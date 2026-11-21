"""Comprehensive tests for Tier 1-3 trainer enhancements (no live game process required)."""

from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import San14TrainerApp
from custom_trait_editor import (
    CUSTOM_TRAIT_MIN_ID,
    EDIT_MGR_PERSONALITY_ARR,
    EDIT_MGR_PTR_RVA,
    EDIT_PERSONALITY_FILE_SIZE,
    EDIT_PERSONALITY_RECORD_TYPE,
    EDIT_PERSONALITY_SLOT_COUNT,
    EDIT_PERSONALITY_STRIDE,
    MGR_PERSONALITY_ARR,
    PERSONALITY_RECORD_TYPE,
    PERSONALITY_STRIDE,
    TOTAL_PERSONALITY_SLOTS,
    CustomTraitDef,
    auto_generate_description,
    clear_live_custom_trait,
    clear_live_edit_personality,
    find_next_free_custom_slot,
    list_live_custom_traits,
    load_custom_traits_file,
    load_edit_personality_file,
    read_live_trait,
    save_custom_traits_file,
    save_edit_personality_file,
    write_live_custom_trait,
    write_live_edit_personality,
)
from game_world_editor import (
    AREA_STRIDE,
    CITY_STRIDE,
    FORMATION_MASK_ALL,
    FORMATION_MASK_LAND,
    ITEM_RECORD_TYPE,
    ITEM_STRIDE,
    MGR_AREA_ARR,
    MGR_CITY_ARR,
    MGR_ITEM_ARR,
    MGR_PERSON_ARR,
    MGR_POLICY_ARR,
    PERSON_STRIDE,
    POLICY_RECORD_TYPE,
    POLICY_STRIDE,
    batch_boost_player_officers,
    batch_max_player_areas,
    batch_supply_player_cities,
    doctrine_level_from_exp,
    grant_item_to_officer,
    list_diplomacy,
    list_items,
    read_officer_deep,
    read_policy_catalog,
    read_world_context,
    set_all_diplomacy_max,
    set_force_friendliness,
    set_foreign_relation,
    write_officer_deep,
)
from memory_engine import MemoryErrorWin32, TYPE_FORMATS
from officer_auto_locator import (
    AREA_RECORD_TYPE,
    CITY_RECORD_TYPE,
    DISTRICT_RECORD_TYPE,
    FORCE_RECORD_TYPE,
    MANAGER_PTR_RVA,
    MGR_DISTRICT_PTRS_OFFSET,
    MGR_FORCE_PTRS_OFFSET,
    OFFICER_RECORD_TYPE,
    PLAYER_FORCE_ID_RVA,
    resolve_live_global_field,
)
from officer_probe import HERITAGE_STATS_OFFSET


class FakeMemory:
    """Sparse byte-addressable mock ProcessMemory for deterministic struct-offset tests."""

    def __init__(self, base_address: int = 0x7FF600000000) -> None:
        self.is_attached = True
        self.handle = 1
        self.pid = 9999
        self.process_name = "SAN14CE_SC.exe"
        self.base_address = base_address
        self._bufs: list[tuple[int, bytearray]] = []
        self.patch_history: list[tuple[int, bytes, str]] = []

    def alloc(self, addr: int, size: int) -> bytearray:
        buf = bytearray(size)
        self._bufs.append((addr, buf))
        return buf

    def _locate(self, addr: int, size: int) -> tuple[bytearray, int]:
        for start, buf in self._bufs:
            if start <= addr and addr + size <= start + len(buf):
                return buf, addr - start
        raise MemoryErrorWin32(f"unmapped {hex(addr)}+{size}")

    def read_bytes(self, addr: int, size: int) -> bytes:
        buf, off = self._locate(addr, size)
        return bytes(buf[off : off + size])

    def write_bytes(self, addr: int, data: bytes, *, record_patch: bool = True, label: str = "") -> None:
        buf, off = self._locate(addr, len(data))
        if record_patch:
            self.patch_history.append((addr, bytes(buf[off : off + len(data)]), label))
        buf[off : off + len(data)] = data

    def read_value(self, addr: int, dtype: str) -> int:
        fmt, sz = TYPE_FORMATS[dtype]
        return struct.unpack(fmt, self.read_bytes(addr, sz))[0]

    def write_value(self, addr: int, dtype: str, value: int, *, record_patch: bool = True, label: str = "") -> None:
        fmt, _ = TYPE_FORMATS[dtype]
        self.write_bytes(addr, struct.pack(fmt, value), record_patch=record_patch, label=label)

    def detach(self) -> None:
        self.is_attached = False


def _put_wstr(buf: bytearray, off: int, text: str) -> None:
    enc = (text + "\x00").encode("utf-16-le")
    buf[off : off + len(enc)] = enc


def build_fake_world() -> tuple[FakeMemory, dict[str, int]]:
    """Create a realistic miniature SAN14CE_SC.exe world in FakeMemory (Player=Liu Bei Force 2, Enemy=Liu Zhang Force 11)."""
    mem = FakeMemory(base_address=0x7FF600000000)
    base = mem.base_address

    # Globals near base + 0x2783158..0x2783400
    gbuf = mem.alloc(base + 0x2783100, 0x400)
    mgr = 0x20000000
    struct.pack_into("<Q", gbuf, (base + MANAGER_PTR_RVA) - (base + 0x2783100), mgr)
    struct.pack_into("<i", gbuf, (base + PLAYER_FORCE_ID_RVA) - (base + 0x2783100), 2)

    mbuf = mem.alloc(mgr, 0x90000)
    person_arr = 0x21000000
    city_arr = 0x22000000
    area_arr = 0x23000000
    item_arr = 0x24000000
    policy_arr = 0x25000000
    forces_base = 0x26000000
    districts_base = 0x27000000
    personality_arr = 0x28000000

    struct.pack_into("<Q", mbuf, MGR_PERSON_ARR, person_arr)
    struct.pack_into("<Q", mbuf, MGR_CITY_ARR, city_arr)
    struct.pack_into("<Q", mbuf, MGR_AREA_ARR, area_arr)
    struct.pack_into("<Q", mbuf, MGR_ITEM_ARR, item_arr)
    struct.pack_into("<Q", mbuf, MGR_POLICY_ARR, policy_arr)
    struct.pack_into("<Q", mbuf, MGR_PERSONALITY_ARR, personality_arr)

    # Personalities (0..355, preserving trailing vector pointers at +0xC8..+0xE0)
    perbuf = mem.alloc(personality_arr, PERSONALITY_STRIDE * TOTAL_PERSONALITY_SLOTS)
    for pid in range(TOTAL_PERSONALITY_SLOTS):
        off = pid * PERSONALITY_STRIDE
        struct.pack_into("<Q", perbuf, off, base + PERSONALITY_RECORD_TYPE)
        struct.pack_into("<QQQ", perbuf, off + 0xC8, 0x30000000 + pid * 16, 0x30000004 + pid * 16, 0x30000004 + pid * 16)

    # Districts (1 -> Force 2 HQ, 2 -> Force 11 HQ)
    dbuf = mem.alloc(districts_base, 0x30 * 52)
    for did in range(52):
        struct.pack_into("<Q", mbuf, MGR_DISTRICT_PTRS_OFFSET + did * 8, districts_base + did * 0x30)
        struct.pack_into("<Q", dbuf, did * 0x30, base + DISTRICT_RECORD_TYPE)
    # District 1: Liu Bei (Force 2, num=1, orders=10)
    dbuf[0x30 + 0x10] = 2
    dbuf[0x30 + 0x11] = 1
    dbuf[0x30 + 0x14] = 10
    # District 2: Liu Zhang (Force 11, num=1, orders=5)
    dbuf[2 * 0x30 + 0x10] = 11
    dbuf[2 * 0x30 + 0x11] = 1
    dbuf[2 * 0x30 + 0x14] = 5

    # Forces (2=Liu Bei, 11=Liu Zhang, 46..50=Tribes)
    fbuf = mem.alloc(forces_base, 0x268 * 52)
    for fid in range(52):
        struct.pack_into("<Q", mbuf, MGR_FORCE_PTRS_OFFSET + fid * 8, forces_base + fid * 0x268)
        struct.pack_into("<Q", fbuf, fid * 0x268, base + FORCE_RECORD_TYPE)
    struct.pack_into("<H", fbuf, 2 * 0x268 + 0x10, 1)    # ruler = officer 1 (刘备)
    struct.pack_into("<I", fbuf, 2 * 0x268 + 0x164, 15000)  # doctrine exp -> Lv3
    fbuf[2 * 0x268 + 0x13 + 11] = 50
    fbuf[11 * 0x268 + 0x13 + 2] = 50
    struct.pack_into("<H", fbuf, 11 * 0x268 + 0x10, 3)   # ruler = officer 3 (刘璋)
    for tid in range(46, 51):
        fbuf[2 * 0x268 + 0x13 + tid] = 60
        fbuf[tid * 0x268 + 0x13 + 2] = 60
    struct.pack_into("<H", fbuf, 2 * 0x268 + 0x184 + 1 * 2, 250)  # 天竺 friendliness
    fbuf[2 * 0x268 + 0x18E + 1] = 1  # 天竺 trade lv 1

    # Officers (1=刘备 dist 1, 2=张飞 dist 1 ill=2, 3=刘璋 dist 2 enemy, 4=已死 dist 1 status=9)
    pbuf = mem.alloc(person_arr, PERSON_STRIDE * 10)
    for oid in range(1, 5):
        off = oid * PERSON_STRIDE
        struct.pack_into("<Q", pbuf, off, base + OFFICER_RECORD_TYPE)
        struct.pack_into("<5H", pbuf, off + 0x130, 100, 100, 100, 100, 100)
    # 1: 刘备
    o1 = 1 * PERSON_STRIDE
    _put_wstr(pbuf, o1 + 0x12, "刘")
    _put_wstr(pbuf, o1 + 0x24, "备")
    struct.pack_into("<HH", pbuf, o1 + 0xE4, 161, 223)
    pbuf[o1 + 0x118] = 1
    pbuf[o1 + 0x11E] = 1
    pbuf[o1 + 0x120] = 95
    pbuf[o1 + 0x124 : o1 + 0x129] = bytes((76, 73, 74, 78, 99))
    struct.pack_into("<H", pbuf, o1 + 0x164, 0x00A8)
    pbuf[o1 + 0x184] = 1
    pbuf[o1 + 0x185] = 2
    pbuf[o1 + 0x199] = 0
    pbuf[o1 + 0x1BA] = 3
    # 2: 张飞 (sick, low loyalty)
    o2 = 2 * PERSON_STRIDE
    _put_wstr(pbuf, o2 + 0x12, "张")
    _put_wstr(pbuf, o2 + 0x24, "飞")
    struct.pack_into("<HH", pbuf, o2 + 0xE4, 167, 221)
    pbuf[o2 + 0x118] = 1
    pbuf[o2 + 0x11E] = 4
    pbuf[o2 + 0x120] = 80
    pbuf[o2 + 0x124 : o2 + 0x129] = bytes((86, 98, 33, 22, 44))
    pbuf[o2 + 0x199] = 2
    # 3: 刘璋 (enemy)
    o3 = 3 * PERSON_STRIDE
    _put_wstr(pbuf, o3 + 0x12, "刘")
    _put_wstr(pbuf, o3 + 0x24, "璋")
    struct.pack_into("<HH", pbuf, o3 + 0xE4, 162, 219)
    pbuf[o3 + 0x118] = 2
    pbuf[o3 + 0x11E] = 1
    pbuf[o3 + 0x120] = 70
    pbuf[o3 + 0x124 : o3 + 0x129] = bytes((40, 30, 50, 60, 70))
    pbuf[o3 + 0x199] = 1
    # 4: Dead officer in dist 1
    o4 = 4 * PERSON_STRIDE
    _put_wstr(pbuf, o4 + 0x12, "庞")
    _put_wstr(pbuf, o4 + 0x24, "统")
    pbuf[o4 + 0x118] = 1
    pbuf[o4 + 0x11E] = 9
    pbuf[o4 + 0x120] = 50
    pbuf[o4 + 0x124 : o4 + 0x129] = bytes((78, 34, 97, 85, 69))

    # Cities (1=永安 dist 1, 2=成都 dist 2 enemy)
    cbuf = mem.alloc(city_arr, CITY_STRIDE * 6)
    for cid in range(1, 3):
        off = cid * CITY_STRIDE
        struct.pack_into("<Q", cbuf, off, base + CITY_RECORD_TYPE)
    _put_wstr(cbuf, 1 * CITY_STRIDE + 0x12, "永安")
    cbuf[1 * CITY_STRIDE + 0x30] = 1
    struct.pack_into("<IIIIBBH", cbuf, 1 * CITY_STRIDE + 0x34, 1000, 2000, 5000, 300, 80, 0, 4000)
    _put_wstr(cbuf, 2 * CITY_STRIDE + 0x12, "成都")
    cbuf[2 * CITY_STRIDE + 0x30] = 2
    struct.pack_into("<IIIIBBH", cbuf, 2 * CITY_STRIDE + 0x34, 3000, 4000, 8000, 0, 90, 0, 6000)

    # Areas (1=永安郊区 city 1, 2=成都郊区 city 2 enemy)
    abuf = mem.alloc(area_arr, AREA_STRIDE * 6)
    for aid in range(1, 3):
        off = aid * AREA_STRIDE
        struct.pack_into("<Q", abuf, off, base + AREA_RECORD_TYPE)
    abuf[1 * AREA_STRIDE + 0x35] = 1
    struct.pack_into("<HHH", abuf, 1 * AREA_STRIDE + 0x3C, 100, 120, 140)
    abuf[1 * AREA_STRIDE + 0x54] = 50
    abuf[1 * AREA_STRIDE + 0x6E] = 5
    abuf[2 * AREA_STRIDE + 0x35] = 2
    struct.pack_into("<HHH", abuf, 2 * AREA_STRIDE + 0x3C, 200, 220, 240)
    abuf[2 * AREA_STRIDE + 0x54] = 60

    # Policies
    polbuf = mem.alloc(policy_arr, POLICY_STRIDE * 5)
    for pid, name in ((0, "无效"), (1, "强化鱼鳞"), (2, "强化锋矢"), (3, "强化雁行")):
        off = pid * POLICY_STRIDE
        struct.pack_into("<Q", polbuf, off, base + POLICY_RECORD_TYPE)
        _put_wstr(polbuf, off + 0x10, name)

    # Items (1=赤兔马 owned by 3, 2=玉玺 unowned)
    ibuf = mem.alloc(item_arr, ITEM_STRIDE * 5)
    for iid, name, owner, area, st in ((1, "赤兔马", 3, 0, 1), (2, "玉玺", 0, 12, 3)):
        off = iid * ITEM_STRIDE
        struct.pack_into("<Q", ibuf, off, base + ITEM_RECORD_TYPE)
        _put_wstr(ibuf, off + 0x10, name)
        struct.pack_into("<HH", ibuf, off + 0x4A, owner, area)
        ibuf[off + 0x50] = st

    return mem, {
        "person_arr": person_arr,
        "city_arr": city_arr,
        "area_arr": area_arr,
        "item_arr": item_arr,
        "forces_base": forces_base,
        "districts_base": districts_base,
        "personality_arr": personality_arr,
    }


class WorldBackendTests(unittest.TestCase):
    def test_doctrine_level_thresholds(self):
        self.assertEqual(doctrine_level_from_exp(0), 1)
        self.assertEqual(doctrine_level_from_exp(5999), 1)
        self.assertEqual(doctrine_level_from_exp(6000), 2)
        self.assertEqual(doctrine_level_from_exp(200000), 10)
        self.assertEqual(doctrine_level_from_exp(9999999), 10)

    def test_world_context_and_global_resolution(self):
        mem, addrs = build_fake_world()
        with patch("game_world_editor.supported_executable", return_value=True):
            ctx = read_world_context(mem)
            self.assertEqual(ctx.player_force_id, 2)
            self.assertEqual(ctx.player_ruler_name, "刘备")
            self.assertEqual(ctx.player_district_ids, (1,))
            self.assertEqual(ctx.orders, 10)
            self.assertEqual(ctx.doctrine_level, 3)
            self.assertEqual(ctx.player_city_count, 1)
            self.assertEqual(ctx.player_officer_count, 2)  # excludes dead officer #4 and enemy #3

            pols = read_policy_catalog(mem)
            self.assertEqual(pols, [(1, "强化鱼鳞"), (2, "强化锋矢"), (3, "强化雁行")])

        orders_res = resolve_live_global_field(mem, "inf_orders")
        admin_res = resolve_live_global_field(mem, "max_admin")
        self.assertEqual(orders_res, (addrs["districts_base"] + 0x30 + 0x14, "u8", 99, 0, 99))
        self.assertEqual(admin_res, (addrs["forces_base"] + 2 * 0x268 + 0x164, "u32", 9999999, 0, 9999999))
        self.assertIsNone(resolve_live_global_field(mem, "inf_advocation"))

    def test_officer_deep_read_and_write(self):
        mem, addrs = build_fake_world()
        rec1 = addrs["person_arr"] + 1 * PERSON_STRIDE
        with patch("game_world_editor.supported_executable", return_value=True):
            info = read_officer_deep(mem, rec1, 1)
            self.assertEqual(info.full_name, "刘备")
            self.assertEqual(info.birth_year, 161)
            self.assertEqual(info.death_year, 223)
            self.assertEqual(info.formations_mask, 0x00A8)

            updated = write_officer_deep(
                mem,
                rec1,
                formations_mask=FORMATION_MASK_ALL,
                death_year=350,
                loyalty=100,
                illness=0,
                ism=2,
                policy_id=3,
                policy_lv=10,
                stat_cap_value=120,
            )
            self.assertEqual(updated.formations_mask, FORMATION_MASK_ALL)
            self.assertEqual(updated.death_year, 350)
            self.assertEqual(updated.loyalty, 100)
            self.assertEqual(updated.ism, 2)
            self.assertEqual(updated.policy_id, 3)
            self.assertEqual(updated.policy_lv, 10)
            self.assertEqual(updated.stat_caps, (120, 120, 120, 120, 120))

    def test_batch_ops_only_modify_player_assets(self):
        mem, addrs = build_fake_world()
        with patch("game_world_editor.supported_executable", return_value=True):
            cities = batch_supply_player_cities(mem, gold=500000, food=600000, troops=150000, durability=50000, morale=180)
            self.assertEqual(cities, ["永安"])
            # Player city updated
            self.assertEqual(mem.read_value(addrs["city_arr"] + CITY_STRIDE + 0x34, "u32"), 500000)
            self.assertEqual(mem.read_value(addrs["city_arr"] + CITY_STRIDE + 0x40, "u32"), 0)
            # Enemy city untouched
            self.assertEqual(mem.read_value(addrs["city_arr"] + 2 * CITY_STRIDE + 0x34, "u32"), 3000)

            area_cnt = batch_max_player_areas(mem, dev=1000, order=100, build_cap=25)
            self.assertEqual(area_cnt, 1)
            self.assertEqual(mem.read_value(addrs["area_arr"] + AREA_STRIDE + 0x3C, "u16"), 1000)
            self.assertEqual(mem.read_value(addrs["area_arr"] + AREA_STRIDE + 0x54, "u8"), 100)
            self.assertEqual(mem.read_value(addrs["area_arr"] + AREA_STRIDE + 0x6E, "u8"), 25)
            # Enemy area untouched
            self.assertEqual(mem.read_value(addrs["area_arr"] + 2 * AREA_STRIDE + 0x3C, "u16"), 200)

            off_cnt = batch_boost_player_officers(
                mem, loyalty=100, cure_illness=True, unlock_formations_mask=FORMATION_MASK_LAND, extend_death_year=350
            )
            self.assertEqual(off_cnt, 2)
            # Zhang Fei cured, maxed loyalty, formations unlocked, lifespan extended
            o2 = addrs["person_arr"] + 2 * PERSON_STRIDE
            self.assertEqual(mem.read_value(o2 + 0x120, "u8"), 100)
            self.assertEqual(mem.read_value(o2 + 0x199, "u8"), 0)
            self.assertEqual(mem.read_value(o2 + 0x164, "u16"), FORMATION_MASK_LAND)
            self.assertEqual(mem.read_value(o2 + 0xE6, "u16"), 350)
            # Enemy Liu Zhang & dead Pang Tong untouched
            self.assertEqual(mem.read_value(addrs["person_arr"] + 3 * PERSON_STRIDE + 0x120, "u8"), 70)
            self.assertEqual(mem.read_value(addrs["person_arr"] + 4 * PERSON_STRIDE + 0x120, "u8"), 50)

    def test_diplomacy_tribes_foreign_and_items(self):
        mem, addrs = build_fake_world()
        with patch("game_world_editor.supported_executable", return_value=True):
            forces, tribes, foreigns = list_diplomacy(mem)
            self.assertEqual(len(forces), 1)
            self.assertEqual(forces[0].name, "刘璋军")
            self.assertEqual(len(tribes), 5)
            self.assertEqual(len(foreigns), 4)
            self.assertEqual(foreigns[0].friendliness, 250)

            set_force_friendliness(mem, 11, 100)
            self.assertEqual(mem.read_value(addrs["forces_base"] + 2 * 0x268 + 0x13 + 11, "u8"), 100)
            self.assertEqual(mem.read_value(addrs["forces_base"] + 11 * 0x268 + 0x13 + 2, "u8"), 100)

            cnt = set_all_diplomacy_max(mem, include_tribes=True, value=100)
            self.assertEqual(cnt, 6)
            self.assertEqual(mem.read_value(addrs["forces_base"] + 2 * 0x268 + 0x13 + 50, "u8"), 100)

            updated_fn = set_foreign_relation(mem, 1, friendliness=1000, trade_level=4)
            self.assertEqual(updated_fn.friendliness, 1000)
            self.assertEqual(updated_fn.trade_level, 4)

            items = list_items(mem)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].name, "赤兔马")
            self.assertEqual(items[0].owner_name, "刘璋")
            self.assertEqual(items[1].owner_name, "无主")

            grant_item_to_officer(mem, items[1].address, 1, item_name="玉玺", officer_name="刘备")
            items_after = list_items(mem)
            self.assertEqual(items_after[1].owner_id, 1)
            self.assertEqual(items_after[1].owner_name, "刘备")
            self.assertEqual(items_after[1].area_id, 0)
            self.assertEqual(items_after[1].status, 1)


class AppEnhancementTests(unittest.TestCase):
    def setUp(self):
        with patch.object(San14TrainerApp, "_register_all_hotkeys"), \
             patch.object(San14TrainerApp, "_auto_find_game"), \
             patch.object(San14TrainerApp, "_schedule_freeze_loop"):
            self.app = San14TrainerApp()
        self.app.sound_enabled.set(False)
        self.app.officer_auto_enabled = True

    def tearDown(self):
        self.app._on_close()

    def test_city_all_nine_fields_and_status_format(self):
        mem = FakeMemory()
        city_addr = 0x50000
        buf = mem.alloc(city_addr, 0x168)
        struct.pack_into("<Q", buf, 0, mem.base_address + CITY_RECORD_TYPE)
        struct.pack_into("<IIIIBBHBBBB", buf, 0x34, 10000, 20000, 30000, 500, 90, 0, 8000, 0, 12, 0, 8)
        buf[0xB8] = 2
        self.app.mem = mem
        snap = (city_addr, bytes(buf[0x34:0x4C]) + bytes((buf[0xB8],)))
        self.app.location_snapshots["city"] = snap
        self.assertIn("耐8000/士90/兵30000", self.app._format_location_status("city", snap[1]))
        self.assertIn("混乱3天", self.app._format_location_status("city", snap[1]))
        self.assertIn("挑衅2天", self.app._format_location_status("city", snap[1]))

        city_fields = {f.key: f for f in self.app.profile.tables["city"].fields}
        cases = [
            ("city_durability", "15000", 0x46, "u16", 15000),
            ("city_morale", "180", 0x44, "u8", 180),
            ("city_gold", "88888", 0x34, "u32", 88888),
            ("city_food", "99999", 0x38, "u32", 99999),
            ("city_troops", "123456", 0x3C, "u32", 123456),
            ("city_wounded", "0", 0x40, "u32", 0),
            ("city_provoked", "5", 0x4B, "u8", 20),
            ("city_disorder", "10", 0x49, "u8", 40),
            ("city_scale", "4", 0xB8, "u8", 4),
        ]
        for key, text, off, dtype, expected in cases:
            cur_snap = (city_addr, bytes(buf[0x34:0x4C]) + bytes((buf[0xB8],)))
            self.app.location_snapshots["city"] = cur_snap
            self.app.trainer_inputs[("city", key)].set(text)
            with (
                patch("app.foreground_process_id", return_value=812),
                patch("app.os.getpid", return_value=812),
                patch("app.read_live_targets", return_value={"city": cur_snap}),
            ):
                self.app._trigger_field_action("city", city_fields[key])
            self.assertEqual(mem.read_value(city_addr + off, dtype), expected)

    def test_officer_extra_five_fields_and_max_cap_toggle(self):
        mem = FakeMemory()
        rec_addr = 0x60000
        buf = mem.alloc(rec_addr, PERSON_STRIDE)
        struct.pack_into("<Q", buf, 0, mem.base_address + OFFICER_RECORD_TYPE)
        buf[HERITAGE_STATS_OFFSET : HERITAGE_STATS_OFFSET + 5] = bytes((80, 85, 70, 65, 90))
        struct.pack_into("<5H", buf, 0x130, 100, 100, 100, 100, 100)
        self.app.mem = mem

        snap = (rec_addr + HERITAGE_STATS_OFFSET, bytes((80, 85, 70, 65, 90)))
        self.app.officer_auto_snapshot = snap
        off_fields = {f.key: f for f in self.app.profile.tables["officer"].fields}

        for key, text, off, dtype, expected in (
            ("loyalty", "100", 0x120, "u8", 100),
            ("policy_lv", "10", 0x1BA, "u8", 10),
            ("birth_year", "175", 0xE4, "u16", 175),
            ("illness", "0", 0x199, "u8", 0),
        ):
            self.app.trainer_inputs[("officer", key)].set(text)
            with (
                patch("app.foreground_process_id", return_value=812),
                patch("app.os.getpid", return_value=812),
                patch("app.read_selected_officer", return_value=snap),
            ):
                self.app._trigger_field_action("officer", off_fields[key])
            self.assertEqual(mem.read_value(rec_addr + off, dtype), expected)

        # Toggle max_cap ON -> 120, OFF -> 100
        with (
            patch("app.foreground_process_id", return_value=812),
            patch("app.os.getpid", return_value=812),
            patch("app.read_selected_officer", return_value=snap),
        ):
            self.app._trigger_field_action("officer", off_fields["max_cap"])
        self.assertEqual(struct.unpack_from("<5H", buf, 0x130), (120, 120, 120, 120, 120))
        with (
            patch("app.foreground_process_id", return_value=812),
            patch("app.os.getpid", return_value=812),
            patch("app.read_selected_officer", return_value=snap),
        ):
            self.app._trigger_field_action("officer", off_fields["max_cap"])
        self.assertEqual(struct.unpack_from("<5H", buf, 0x130), (100, 100, 100, 100, 100))

    def test_global_live_toggles_and_group_quick_actions(self):
        mem, addrs = build_fake_world()
        self.app.mem = mem
        gfields = {f.key: f for f in self.app.profile.tables["global"].fields}
        self.app._trigger_field_action("global", gfields["inf_orders"])
        self.assertTrue(self.app.active_toggles[("global", "inf_orders")])
        self.assertEqual(mem.read_value(addrs["districts_base"] + 0x30 + 0x14, "u8"), 99)

        self.app._trigger_field_action("global", gfields["max_admin"])
        self.assertTrue(self.app.active_toggles[("global", "max_admin")])
        self.assertEqual(mem.read_value(addrs["forces_base"] + 2 * 0x268 + 0x164, "u32"), 9999999)

        # Group quick actions: read current values & weaken & purify on open city
        city1 = addrs["city_arr"] + CITY_STRIDE
        snap = (city1, mem.read_bytes(city1 + 0x34, 24) + mem.read_bytes(city1 + 0xB8, 1))
        with patch("app.read_live_targets", return_value={"city": snap}):
            self.app._read_group_values("city")
            self.assertEqual(self.app.trainer_inputs[("city", "city_gold")].get(), "1000")
            self.app._apply_group_quick_action("city", "weaken")
            self.assertEqual(mem.read_value(city1 + 0x46, "u16"), 1)
            self.assertEqual(mem.read_value(city1 + 0x44, "u8"), 1)
            self.assertEqual(mem.read_value(city1 + 0x3C, "u32"), 1)
            self.app._apply_group_quick_action("city", "purify")
            self.assertEqual(mem.read_value(city1 + 0x40, "u32"), 0)
            self.assertEqual(mem.read_value(city1 + 0x49, "u8"), 0)

    def test_topmost_toggle_and_skill_presets_persistence(self):
        self.app.topmost_enabled.set(True)
        self.app._toggle_topmost()
        self.assertTrue(bool(self.app.attributes("-topmost")))
        self.app.topmost_enabled.set(False)
        self.app._toggle_topmost()
        self.assertFalse(bool(self.app.attributes("-topmost")))

        ctrl = self.app.skill_controller
        with tempfile.TemporaryDirectory() as tmp:
            ctrl.presets_path = Path(tmp) / "skill_presets.json"
            ctrl.apply_skill_preset("内置 · 野战神将")
            self.assertEqual(ctrl.draft_traits, [67, 150, 37, 4, 106])
            self.assertEqual(ctrl.draft_tactics[0], 93)

            ctrl.draft_traits = [151, 37, 0, 0, 0]
            ctrl.draft_tactics = [97, 15, 0, 0, 0, 0, 0, 0, 0, 0]
            ctrl.save_current_as_preset("测试自定义方案")
            self.assertTrue(ctrl.presets_path.is_file())

            # Reload from disk
            ctrl.custom_presets = {}
            ctrl._load_custom_presets()
            self.assertIn("测试自定义方案", ctrl.custom_presets)
            self.assertEqual(ctrl.custom_presets["测试自定义方案"]["traits"], [151, 37, 0, 0, 0])

            # Built-in preset cannot be deleted; custom preset can
            self.assertFalse(ctrl.delete_selected_preset("内置 · 野战神将"))
            self.assertTrue(ctrl.delete_selected_preset("测试自定义方案"))
            self.assertFalse(ctrl.presets_path.exists())

    def test_unrestricted_custom_trait_write_read_clear_and_catalog_sync(self):
        mem, addrs = build_fake_world()
        with patch("custom_trait_editor.supported_executable", return_value=True):
            self.assertEqual(find_next_free_custom_slot(mem), CUSTOM_TRAIT_MIN_ID)
            trait = CustomTraitDef(
                slot_id=256,
                name="战神无双",
                description="",
                color=3,
                eff1_id=19,
                eff1_val=50,
                eff2_id=51,
                eff2_val=1,
                cond1=0,
                cond2=0,
            )
            written = write_live_custom_trait(mem, trait)
            self.assertEqual(written.name, "战神无双")
            self.assertEqual(written.eff1_val, 50)
            self.assertEqual(written.eff2_id, 51)
            self.assertTrue(len(written.description) > 0)

            # Verify trailing std::vector pointers (+0xC8) were NEVER clobbered!
            slot_addr = addrs["personality_arr"] + 256 * PERSONALITY_STRIDE
            vec_begin = mem.read_value(slot_addr + 0xC8, "u64")
            self.assertEqual(vec_begin, 0x30000000 + 256 * 16)
            # Verify flags (+0xBF) = 0x1A and eff1 (+0xB6) = 19, val1 (+0xB8) = 50
            self.assertEqual(mem.read_value(slot_addr + 0xBF, "u8"), 0x1A)
            self.assertEqual(mem.read_value(slot_addr + 0xB6, "u16"), 19)
            self.assertEqual(mem.read_value(slot_addr + 0xB8, "i16"), 50)

            readback = read_live_trait(mem, 256)
            self.assertEqual(readback.name, "战神无双")
            self.assertEqual(readback.eff1_id, 19)
            self.assertEqual(readback.eff1_val, 50)
            self.assertEqual(readback.eff2_id, 51)
            self.assertEqual(len(list_live_custom_traits(mem)), 1)
            self.assertEqual(find_next_free_custom_slot(mem), 257)

            # Test JSON save/load & catalog upsert + equip hook
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "custom_traits.json"
                save_custom_traits_file(p, {256: written})
                loaded = load_custom_traits_file(p)
                self.assertIn(256, loaded)
                self.assertEqual(loaded[256].name, "战神无双")

            ctrl = self.app.skill_controller
            ctrl.catalog.upsert_trait(written.to_trait_info())
            self.assertEqual(ctrl.catalog.trait_name(256), "战神无双")
            ctrl.draft_traits = [14, 0, 0, 0, 0]
            ctrl._on_custom_trait_changed(256, equip=True)
            self.assertEqual(ctrl.draft_traits[1], 256)

            clear_live_custom_trait(mem, 256)
            self.assertEqual(list_live_custom_traits(mem), [])
            self.assertEqual(auto_generate_description(trait)[:4], "全能力上")

        from custom_trait_dialog import CustomTraitWorkshopDialog
        dlg = CustomTraitWorkshopDialog(self.app.skill_controller, on_changed=lambda _s, _e: None)
        try:
            self.assertIn("正常值: 2", dlg.eff1_norm_var.get())
            self.assertIn("开关", dlg.eff2_norm_var.get())
            dlg._fill_normal_value(1)
            self.assertEqual(dlg.eff1_val_var.get(), "2")
        finally:
            dlg.destroy()

    def test_global_edit_personality_file_and_title_menu_live_sync(self):
        mem, _ = build_fake_world()
        base = mem.base_address
        # Map g_edit_mgr (base + 0x2148880) -> CEditPersonalityData[0..99]
        gptr_buf = mem.alloc(base + EDIT_MGR_PTR_RVA, 16)
        edit_mgr = 0x29000000
        struct.pack_into("<Q", gptr_buf, 0, edit_mgr)
        em_buf = mem.alloc(edit_mgr, EDIT_MGR_PERSONALITY_ARR + 16)
        ep_arr = 0x2A000000
        struct.pack_into("<Q", em_buf, EDIT_MGR_PERSONALITY_ARR, ep_arr)
        ep_buf = mem.alloc(ep_arr, EDIT_PERSONALITY_STRIDE * EDIT_PERSONALITY_SLOT_COUNT)
        for i in range(EDIT_PERSONALITY_SLOT_COUNT):
            struct.pack_into("<Q", ep_buf, i * EDIT_PERSONALITY_STRIDE, base + EDIT_PERSONALITY_RECORD_TYPE)
            ep_buf[i * EDIT_PERSONALITY_STRIDE + 8] = 2

        trait = CustomTraitDef(
            slot_id=256,
            name="霸王无双",
            description="全能力+200且无视ZOC",
            color=3,
            eff1_id=19,
            eff1_val=200,
            eff2_id=51,
            eff2_val=1,
            cond1=9,
            cond2=1,
            cond1_param=5,
        )

        # 1. File roundtrip (exact 6300 bytes + .bak backup)
        with tempfile.TemporaryDirectory() as tmp:
            ep_file = Path(tmp) / "editpersonalityexceSC.s14"
            save_edit_personality_file(ep_file, {256: trait}, create_backup=True)
            self.assertEqual(ep_file.stat().st_size, EDIT_PERSONALITY_FILE_SIZE)
            loaded = load_edit_personality_file(ep_file)
            self.assertIn(256, loaded)
            self.assertEqual(loaded[256].name, "霸王无双")
            self.assertEqual(loaded[256].eff1_id, 19)
            self.assertEqual(loaded[256].eff1_val, 200)
            self.assertEqual(loaded[256].eff2_id, 51)
            self.assertEqual(loaded[256].cond1, 9)
            self.assertEqual(loaded[256].cond1_param, 5)

            # Second write creates .bak backup
            save_edit_personality_file(ep_file, {256: trait}, create_backup=True)
            self.assertTrue(ep_file.with_suffix(".s14.bak").is_file())

        # 2. Live title-menu CEditPersonalityData write & clear
        with patch("custom_trait_editor.supported_executable", return_value=True):
            write_live_edit_personality(mem, trait)
            self.assertEqual(mem.read_value(ep_arr + 0x0A, "u16"), 256)
            self.assertEqual(mem.read_value(ep_arr + 0x38, "u8"), 9)
            self.assertEqual(mem.read_value(ep_arr + 0x3C, "u32"), 5)
            self.assertEqual(mem.read_value(ep_arr + 0x48, "u8"), 19)
            self.assertEqual(mem.read_value(ep_arr + 0x4A, "i16"), 200)
            self.assertEqual(mem.read_value(ep_arr + 0x4C, "u8"), 51)
            self.assertEqual(mem.read_value(ep_arr + 0x4E, "i16"), 1)

            clear_live_edit_personality(mem, 256)
            self.assertEqual(mem.read_value(ep_arr + 0x0A, "u16"), 0)
            self.assertEqual(mem.read_value(ep_arr + 0x08, "u8"), 2)


if __name__ == "__main__":
    unittest.main()
