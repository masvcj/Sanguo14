"""Live scenario world editor for SAN14PK Heritage Edition (SAN14CE_SC.exe).

All struct offsets and vtable RVAs in this module are verified directly against the live
Simplified Chinese Heritage executable (SHA-256 2e49348aa1b9be5cfca651e71c8d77b2e4bd2d11387edc0b0236f4352458a239).
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from memory_engine import MemoryErrorWin32, ProcessMemory
from officer_auto_locator import (
    AREA_RECORD_TYPE,
    CITY_RECORD_TYPE,
    DISTRICT_RECORD_TYPE,
    FORCE_RECORD_TYPE,
    GATE_RECORD_TYPE,
    MANAGER_PTR_RVA,
    MGR_DISTRICT_PTRS_OFFSET,
    MGR_FORCE_PTRS_OFFSET,
    OFFICER_RECORD_TYPE,
    OFFICER_STAT_CAPS_OFFSET,
    PLAYER_FORCE_ID_RVA,
    supported_executable,
)

# Master GameData manager table offsets (from mgr = u64[base + MANAGER_PTR_RVA])
MGR_AREA_ARR = 0x6CAF0
MGR_PERSON_ARR = 0x73A50
MGR_ITEM_ARR = 0x77B18
MGR_POLICY_ARR = 0x78D50
MGR_CITY_ARR = 0xDD38
MGR_GATE_ARR = 0xDED8
MGR_FOREIGN_ARR = 0x85A20

ITEM_RECORD_TYPE = 0x13AC170
POLICY_RECORD_TYPE = 0x13AC2A8
FOREIGN_RECORD_TYPE = 0x13AC4B0

PERSON_STRIDE = 0x228
CITY_STRIDE = 0x168
GATE_STRIDE = 0xC0
AREA_STRIDE = 0x98
ITEM_STRIDE = 0x80
POLICY_STRIDE = 0xA8
FOREIGN_STRIDE = 0x30

MAX_FORCES = 52
MAX_DISTRICTS = 52
MAX_CITIES = 52
MAX_AREAS = 400
MAX_PERSONS = 2000
MAX_ITEMS = 500
MAX_POLICIES = 64

# Verified CDoctrineExpData thresholds (Lv1..Lv10)
DOCTRINE_EXP_THRESHOLDS = (0, 0, 6000, 12000, 20000, 30000, 45000, 65000, 90000, 120000, 200000)

# Verified CFormationData bit indices (CPersonData + 0x164, u16 bitmask)
FORMATIONS: Tuple[Tuple[int, str, str], ...] = (
    (1, "鱼鳞", "陆战"),
    (2, "锋矢", "陆战"),
    (3, "雁行", "陆战"),
    (4, "方圆", "陆战"),
    (5, "鹤翼", "陆战"),
    (6, "长蛇", "陆战"),
    (7, "锥行", "陆战"),
    (8, "井阑", "攻城"),
    (9, "冲车", "攻城"),
    (10, "投石", "攻城"),
    (11, "走舸", "水战"),
    (12, "蒙冲", "水战"),
    (13, "楼船", "水战"),
    (14, "四夷", "异族"),
)
FORMATION_MASK_LAND = 0x07FE        # bits 1..10
FORMATION_MASK_LAND_WATER = 0x3FFE  # bits 1..13
FORMATION_MASK_ALL = 0x7FFE         # bits 1..14

DOCTRINE_NAMES: Dict[int, str] = {
    1: "王道",
    2: "霸道",
    3: "我道",
    4: "割据",
    5: "名利",
    6: "礼教",
}

ILLNESS_NAMES: Dict[int, str] = {
    0: "健康",
    1: "轻伤",
    2: "重伤",
    3: "危笃",
}

# Verified Force IDs 46..50 correspond to the 5 Tribes (异民族) in CForceData diplomacy table
TRIBE_NAMES: Dict[int, str] = {
    46: "乌桓",
    47: "鲜卑",
    48: "羌",
    49: "山越",
    50: "南蛮",
}

FOREIGN_NAMES: Dict[int, str] = {
    1: "天竺国",
    2: "贵霜国",
    3: "安息国",
    4: "大秦国",
}

ITEM_STATUS_NAMES: Dict[int, str] = {
    0: "无效",
    1: "武将持有",
    2: "势力宝库",
    3: "未发现",
}


def _read_utf16z(buf: bytes, offset: int, max_bytes: int) -> str:
    if offset < 0 or offset + 2 > len(buf):
        return ""
    raw = buf[offset : min(len(buf), offset + max_bytes)]
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw.decode("utf-16le", errors="replace").strip()


def doctrine_level_from_exp(exp: int) -> int:
    lv = 1
    for level in range(2, len(DOCTRINE_EXP_THRESHOLDS)):
        if exp >= DOCTRINE_EXP_THRESHOLDS[level]:
            lv = level
    return lv


@dataclass
class WorldContext:
    mgr: int
    player_force_id: int
    player_force_ptr: int
    player_ruler_id: int
    player_ruler_name: str
    player_district_ids: Tuple[int, ...]
    player_hq_district_ptr: int
    orders: int
    doctrine_exp: int
    doctrine_level: int
    player_city_count: int
    player_officer_count: int


@dataclass
class OfficerDeepInfo:
    officer_id: int
    address: int
    full_name: str
    stats: Tuple[int, int, int, int, int]
    stat_caps: Tuple[int, int, int, int, int]
    loyalty: int
    illness: int
    birth_year: int
    death_year: int
    formations_mask: int
    ism: int
    policy_id: int
    policy_lv: int
    district_id: int
    status: int


@dataclass
class DiplomacyEntry:
    force_id: int
    address: int
    name: str
    ruler_id: int
    friendliness: int
    is_tribe: bool


@dataclass
class ForeignRelationEntry:
    foreign_id: int
    name: str
    friendliness: int
    trade_level: int


@dataclass
class TreasureEntry:
    item_id: int
    address: int
    name: str
    owner_id: int
    owner_name: str
    area_id: int
    status: int

    @property
    def status_label(self) -> str:
        return ITEM_STATUS_NAMES.get(self.status, f"状态{self.status}")


def _require_supported(mem: ProcessMemory) -> int:
    if not mem.is_attached or not mem.base_address:
        raise MemoryErrorWin32("请先点击右上角「连接游戏」，并进入正在游玩的剧本。")
    if not supported_executable(mem):
        raise MemoryErrorWin32("当前高级功能仅支持简体中文传承版 SAN14CE_SC.exe。")
    mgr = int(mem.read_value(mem.base_address + MANAGER_PTR_RVA, "u64"))
    if mgr < 0x10000:
        raise MemoryErrorWin32("尚未进入剧本，无法读取世界数据。")
    return mgr


def _read_officer_name(mem: ProcessMemory, person_arr: int, oid: int) -> str:
    if oid <= 0 or oid >= MAX_PERSONS or person_arr < 0x10000:
        return ""
    try:
        raw = mem.read_bytes(person_arr + oid * PERSON_STRIDE, 0x40)
    except MemoryErrorWin32:
        return ""
    if struct.unpack_from("<Q", raw, 0)[0] != mem.base_address + OFFICER_RECORD_TYPE:
        return ""
    return _read_utf16z(raw, 0x12, 16) + _read_utf16z(raw, 0x24, 16)


def read_world_context(mem: ProcessMemory) -> WorldContext:
    """Read current player force, districts, orders, doctrine level, and counts."""
    mgr = _require_supported(mem)
    base = mem.base_address
    player_force_id = int(mem.read_value(base + PLAYER_FORCE_ID_RVA, "i32"))
    if not 1 <= player_force_id < MAX_FORCES:
        raise MemoryErrorWin32("当前未处于玩家操控的剧本中。")

    force_ptr = int(mem.read_value(mgr + MGR_FORCE_PTRS_OFFSET + player_force_id * 8, "u64"))
    if force_ptr < 0x10000 or int(mem.read_value(force_ptr, "u64")) != base + FORCE_RECORD_TYPE:
        raise MemoryErrorWin32("无法读取玩家势力数据结构。")

    ruler_id = int(mem.read_value(force_ptr + 0x10, "u16"))
    doctrine_exp = int(mem.read_value(force_ptr + 0x164, "u32"))
    person_arr = int(mem.read_value(mgr + MGR_PERSON_ARR, "u64"))
    ruler_name = _read_officer_name(mem, person_arr, ruler_id) or f"势力#{player_force_id}"

    district_ids: List[int] = []
    hq_ptr = 0
    for did in range(1, MAX_DISTRICTS):
        dptr = int(mem.read_value(mgr + MGR_DISTRICT_PTRS_OFFSET + did * 8, "u64"))
        if dptr < 0x10000:
            continue
        try:
            if int(mem.read_value(dptr, "u64")) != base + DISTRICT_RECORD_TYPE:
                continue
            owner_force = int(mem.read_value(dptr + 0x10, "u8"))
            dist_num = int(mem.read_value(dptr + 0x11, "u8"))
        except MemoryErrorWin32:
            continue
        if owner_force == player_force_id:
            district_ids.append(did)
            if dist_num == 1 or hq_ptr == 0:
                hq_ptr = dptr

    orders = int(mem.read_value(hq_ptr + 0x14, "u8")) if hq_ptr else 0
    dist_set = set(district_ids)

    city_count = 0
    city_arr = int(mem.read_value(mgr + MGR_CITY_ARR, "u64"))
    if city_arr >= 0x10000:
        for cid in range(1, MAX_CITIES):
            addr = city_arr + cid * CITY_STRIDE
            try:
                if int(mem.read_value(addr, "u64")) != base + CITY_RECORD_TYPE:
                    break
                if int(mem.read_value(addr + 0x30, "u8")) in dist_set:
                    city_count += 1
            except MemoryErrorWin32:
                break

    officer_count = 0
    if person_arr >= 0x10000 and dist_set:
        for oid in range(1, MAX_PERSONS):
            addr = person_arr + oid * PERSON_STRIDE
            try:
                if int(mem.read_value(addr, "u64")) != base + OFFICER_RECORD_TYPE:
                    break
                if int(mem.read_value(addr + 0x118, "u8")) in dist_set and int(mem.read_value(addr + 0x11E, "u8")) != 9:
                    officer_count += 1
            except MemoryErrorWin32:
                break

    return WorldContext(
        mgr=mgr,
        player_force_id=player_force_id,
        player_force_ptr=force_ptr,
        player_ruler_id=ruler_id,
        player_ruler_name=ruler_name,
        player_district_ids=tuple(district_ids),
        player_hq_district_ptr=hq_ptr,
        orders=orders,
        doctrine_exp=doctrine_exp,
        doctrine_level=doctrine_level_from_exp(doctrine_exp),
        player_city_count=city_count,
        player_officer_count=officer_count,
    )


def read_policy_catalog(mem: ProcessMemory) -> List[Tuple[int, str]]:
    """Read live CPolicyData names (ID, name) from the running game."""
    mgr = _require_supported(mem)
    base = mem.base_address
    pol_arr = int(mem.read_value(mgr + MGR_POLICY_ARR, "u64"))
    if pol_arr < 0x10000:
        return []
    result: List[Tuple[int, str]] = []
    for pid in range(MAX_POLICIES):
        addr = pol_arr + pid * POLICY_STRIDE
        try:
            raw = mem.read_bytes(addr, 0x38)
        except MemoryErrorWin32:
            break
        if struct.unpack_from("<Q", raw, 0)[0] != base + POLICY_RECORD_TYPE:
            break
        name = _read_utf16z(raw, 0x10, 24)
        if pid > 0 and name and name not in ("无效", "政策"):
            result.append((pid, name))
    return result


def list_live_person_index(mem: ProcessMemory) -> List[Tuple[int, int, str, int]]:
    """Return (officer_id, record_addr, full_name, district_id) for all named active officers."""
    mgr = _require_supported(mem)
    base = mem.base_address
    person_arr = int(mem.read_value(mgr + MGR_PERSON_ARR, "u64"))
    if person_arr < 0x10000:
        return []
    vt = base + OFFICER_RECORD_TYPE
    officers: List[Tuple[int, int, str, int]] = []
    for oid in range(1, MAX_PERSONS):
        addr = person_arr + oid * PERSON_STRIDE
        try:
            raw = mem.read_bytes(addr, 0x130)
        except MemoryErrorWin32:
            break
        if struct.unpack_from("<Q", raw, 0)[0] != vt:
            break
        stats = raw[0x124:0x129]
        if not any(stats) or any(v > 120 for v in stats):
            continue
        name = _read_utf16z(raw, 0x12, 16) + _read_utf16z(raw, 0x24, 16)
        if not name or name == "无效":
            continue
        officers.append((oid, addr, name, raw[0x118]))
    return officers


def resolve_officer_id_from_address(mem: ProcessMemory, record_addr: int) -> int:
    """Compute the canonical CPersonData ID for a live officer record address."""
    mgr = _require_supported(mem)
    person_arr = int(mem.read_value(mgr + MGR_PERSON_ARR, "u64"))
    if person_arr < 0x10000 or record_addr < person_arr:
        raise MemoryErrorWin32("无法将武将地址映射到武将表 ID。")
    delta = record_addr - person_arr
    if delta % PERSON_STRIDE != 0:
        raise MemoryErrorWin32("武将记录地址未按结构步长对齐。")
    return delta // PERSON_STRIDE


def read_officer_deep(mem: ProcessMemory, record_addr: int, officer_id: Optional[int] = None) -> OfficerDeepInfo:
    """Read extended editable attributes of one live CPersonData record."""
    _require_supported(mem)
    raw = mem.read_bytes(record_addr, PERSON_STRIDE)
    if struct.unpack_from("<Q", raw, 0)[0] != mem.base_address + OFFICER_RECORD_TYPE:
        raise MemoryErrorWin32("目标地址不是有效的武将记录。")
    if officer_id is None:
        officer_id = resolve_officer_id_from_address(mem, record_addr)
    full_name = _read_utf16z(raw, 0x12, 16) + _read_utf16z(raw, 0x24, 16)
    stats = tuple(raw[0x124:0x129])
    caps = struct.unpack_from("<5H", raw, OFFICER_STAT_CAPS_OFFSET)
    return OfficerDeepInfo(
        officer_id=officer_id,
        address=record_addr,
        full_name=full_name or f"武将#{officer_id}",
        stats=stats,  # type: ignore[arg-type]
        stat_caps=caps,  # type: ignore[arg-type]
        loyalty=raw[0x120],
        illness=raw[0x199],
        birth_year=struct.unpack_from("<H", raw, 0xE4)[0],
        death_year=struct.unpack_from("<H", raw, 0xE6)[0],
        formations_mask=struct.unpack_from("<H", raw, 0x164)[0],
        ism=raw[0x184],
        policy_id=raw[0x185],
        policy_lv=raw[0x1BA],
        district_id=raw[0x118],
        status=raw[0x11E],
    )


def write_officer_deep(
    mem: ProcessMemory,
    record_addr: int,
    *,
    formations_mask: Optional[int] = None,
    birth_year: Optional[int] = None,
    death_year: Optional[int] = None,
    loyalty: Optional[int] = None,
    illness: Optional[int] = None,
    ism: Optional[int] = None,
    policy_id: Optional[int] = None,
    policy_lv: Optional[int] = None,
    stat_cap_value: Optional[int] = None,
    label_prefix: str = "officer",
) -> OfficerDeepInfo:
    """Safely write selected deep fields on a live CPersonData record and verify read-back."""
    _require_supported(mem)
    if int(mem.read_value(record_addr, "u64")) != mem.base_address + OFFICER_RECORD_TYPE:
        raise MemoryErrorWin32("目标地址校验失败，不是有效的武将记录。")

    writes: List[Tuple[int, str, int, str]] = []
    if formations_mask is not None:
        writes.append((0x164, "u16", int(formations_mask) & 0xFFFF, "阵型"))
    if birth_year is not None:
        writes.append((0xE4, "u16", max(100, min(400, int(birth_year))), "出生年"))
    if death_year is not None:
        writes.append((0xE6, "u16", max(100, min(500, int(death_year))), "卒年"))
    if loyalty is not None:
        writes.append((0x120, "u8", max(0, min(255, int(loyalty))), "忠诚"))
    if illness is not None:
        writes.append((0x199, "u8", max(0, min(3, int(illness))), "伤病"))
    if ism is not None:
        writes.append((0x184, "u8", max(1, min(6, int(ism))), "主义"))
    if policy_id is not None:
        writes.append((0x185, "u8", max(0, min(63, int(policy_id))), "自带政策"))
    if policy_lv is not None:
        writes.append((0x1BA, "u8", max(1, min(10, int(policy_lv))), "政策等级"))

    for off, dtype, val, tag in writes:
        mem.write_value(record_addr + off, dtype, val, record_patch=True, label=f"{label_prefix}.{tag}")
        if int(mem.read_value(record_addr + off, dtype)) != val:
            raise MemoryErrorWin32(f"{tag} 写入校验不一致。")

    if stat_cap_value is not None:
        cap = max(100, min(255, int(stat_cap_value)))
        payload = struct.pack("<5H", cap, cap, cap, cap, cap)
        mem.write_bytes(record_addr + OFFICER_STAT_CAPS_OFFSET, payload, record_patch=True, label=f"{label_prefix}.能力上限")
        if mem.read_bytes(record_addr + OFFICER_STAT_CAPS_OFFSET, 10) != payload:
            raise MemoryErrorWin32("能力上限写入校验不一致。")

    return read_officer_deep(mem, record_addr)


# ---------------- Force-Wide Batch Actions ----------------


def batch_supply_player_cities(
    mem: ProcessMemory,
    *,
    gold: int = 999999,
    food: int = 999999,
    troops: int = 200000,
    durability: int = 60000,
    morale: int = 200,
    clear_wounded: bool = True,
    clear_abnormal: bool = True,
) -> List[str]:
    """Supply all cities owned by the player's force (never touches enemy cities)."""
    ctx = read_world_context(mem)
    base = mem.base_address
    dist_set = set(ctx.player_district_ids)
    city_arr = int(mem.read_value(ctx.mgr + MGR_CITY_ARR, "u64"))
    if city_arr < 0x10000 or not dist_set:
        raise MemoryErrorWin32("未找到玩家势力下辖都市。")

    updated: List[str] = []
    for cid in range(1, MAX_CITIES):
        addr = city_arr + cid * CITY_STRIDE
        try:
            if int(mem.read_value(addr, "u64")) != base + CITY_RECORD_TYPE:
                break
            if int(mem.read_value(addr + 0x30, "u8")) not in dist_set:
                continue
        except MemoryErrorWin32:
            break
        name = _read_utf16z(mem.read_bytes(addr + 0x12, 20), 0, 20) or f"都市#{cid}"
        mem.write_value(addr + 0x34, "u32", min(9999999, max(0, gold)), record_patch=True, label=f"全都市补给.{name}.资金")
        mem.write_value(addr + 0x38, "u32", min(9999999, max(0, food)), record_patch=True, label=f"全都市补给.{name}.兵粮")
        mem.write_value(addr + 0x3C, "u32", min(999999, max(0, troops)), record_patch=True, label=f"全都市补给.{name}.士兵")
        mem.write_value(addr + 0x44, "u8", min(200, max(0, morale)), record_patch=True, label=f"全都市补给.{name}.士气")
        mem.write_value(addr + 0x46, "u16", min(60000, max(0, durability)), record_patch=True, label=f"全都市补给.{name}.耐久")
        if clear_wounded:
            mem.write_value(addr + 0x40, "u32", 0, record_patch=True, label=f"全都市补给.{name}.伤兵")
        if clear_abnormal:
            mem.write_value(addr + 0x49, "u8", 0, record_patch=True, label=f"全都市补给.{name}.混乱")
            mem.write_value(addr + 0x4B, "u8", 0, record_patch=True, label=f"全都市补给.{name}.挑衅")
        updated.append(name)
    return updated


def batch_max_player_areas(
    mem: ProcessMemory,
    *,
    dev: int = 1000,
    order: int = 100,
    build_cap: Optional[int] = None,
) -> int:
    """Max commerce/agri/barracks/order for every area belonging to player's cities."""
    ctx = read_world_context(mem)
    base = mem.base_address
    dist_set = set(ctx.player_district_ids)
    city_arr = int(mem.read_value(ctx.mgr + MGR_CITY_ARR, "u64"))
    area_arr = int(mem.read_value(ctx.mgr + MGR_AREA_ARR, "u64"))
    if city_arr < 0x10000 or area_arr < 0x10000 or not dist_set:
        raise MemoryErrorWin32("未找到玩家势力下辖地区。")

    player_city_ids = set()
    for cid in range(1, MAX_CITIES):
        addr = city_arr + cid * CITY_STRIDE
        try:
            if int(mem.read_value(addr, "u64")) != base + CITY_RECORD_TYPE:
                break
            if int(mem.read_value(addr + 0x30, "u8")) in dist_set:
                player_city_ids.add(cid)
        except MemoryErrorWin32:
            break

    dev_val = max(0, min(1000, int(dev)))
    ord_val = max(0, min(100, int(order)))
    count = 0
    for aid in range(1, MAX_AREAS):
        addr = area_arr + aid * AREA_STRIDE
        try:
            if int(mem.read_value(addr, "u64")) != base + AREA_RECORD_TYPE:
                break
            owner_city = int(mem.read_value(addr + 0x35, "u8"))
        except MemoryErrorWin32:
            break
        if owner_city not in player_city_ids:
            continue
        dev_bytes = struct.pack("<HHH", dev_val, dev_val, dev_val)
        mem.write_bytes(addr + 0x3C, dev_bytes, record_patch=True, label=f"全区域开发.Area#{aid}")
        mem.write_value(addr + 0x54, "u8", ord_val, record_patch=True, label=f"全区域治安.Area#{aid}")
        if build_cap is not None:
            mem.write_value(addr + 0x6E, "u8", max(1, min(99, int(build_cap))), record_patch=True, label=f"全区域建设上限.Area#{aid}")
        count += 1
    return count


def batch_boost_player_officers(
    mem: ProcessMemory,
    *,
    loyalty: Optional[int] = 100,
    cure_illness: bool = True,
    unlock_formations_mask: Optional[int] = None,
    extend_death_year: Optional[int] = None,
) -> int:
    """Batch-update all active officers belonging to the player's districts."""
    ctx = read_world_context(mem)
    base = mem.base_address
    dist_set = set(ctx.player_district_ids)
    person_arr = int(mem.read_value(ctx.mgr + MGR_PERSON_ARR, "u64"))
    if person_arr < 0x10000 or not dist_set:
        raise MemoryErrorWin32("未找到玩家势力下辖武将。")

    count = 0
    for oid in range(1, MAX_PERSONS):
        addr = person_arr + oid * PERSON_STRIDE
        try:
            if int(mem.read_value(addr, "u64")) != base + OFFICER_RECORD_TYPE:
                break
            dist = int(mem.read_value(addr + 0x118, "u8"))
            status = int(mem.read_value(addr + 0x11E, "u8"))
        except MemoryErrorWin32:
            break
        if dist not in dist_set or status == 9:
            continue
        if loyalty is not None:
            cur_loy = int(mem.read_value(addr + 0x120, "u8"))
            target_loy = max(cur_loy, max(0, min(255, int(loyalty))))
            if target_loy != cur_loy:
                mem.write_value(addr + 0x120, "u8", target_loy, record_patch=True, label=f"全武将.#{oid}.忠诚")
        if cure_illness:
            if int(mem.read_value(addr + 0x199, "u8")) != 0:
                mem.write_value(addr + 0x199, "u8", 0, record_patch=True, label=f"全武将.#{oid}.伤病")
        if unlock_formations_mask is not None:
            cur_mask = int(mem.read_value(addr + 0x164, "u16"))
            new_mask = (cur_mask | int(unlock_formations_mask)) & 0xFFFF
            if new_mask != cur_mask:
                mem.write_value(addr + 0x164, "u16", new_mask, record_patch=True, label=f"全武将.#{oid}.阵型")
        if extend_death_year is not None:
            cur_death = int(mem.read_value(addr + 0xE6, "u16"))
            target_death = max(cur_death, max(200, min(500, int(extend_death_year))))
            if target_death != cur_death:
                mem.write_value(addr + 0xE6, "u16", target_death, record_patch=True, label=f"全武将.#{oid}.延寿")
        count += 1
    return count


# ---------------- Diplomacy, Tribes & Foreign Nations ----------------


def list_diplomacy(mem: ProcessMemory) -> Tuple[List[DiplomacyEntry], List[DiplomacyEntry], List[ForeignRelationEntry]]:
    """List active regular forces, 5 tribes, and 4 foreign nations relative to the player force."""
    ctx = read_world_context(mem)
    base = mem.base_address
    person_arr = int(mem.read_value(ctx.mgr + MGR_PERSON_ARR, "u64"))
    player_raw = mem.read_bytes(ctx.player_force_ptr, 0x1A0)

    forces: List[DiplomacyEntry] = []
    tribes: List[DiplomacyEntry] = []
    for fid in range(1, MAX_FORCES):
        if fid == ctx.player_force_id:
            continue
        fptr = int(mem.read_value(ctx.mgr + MGR_FORCE_PTRS_OFFSET + fid * 8, "u64"))
        if fptr < 0x10000:
            continue
        try:
            if int(mem.read_value(fptr, "u64")) != base + FORCE_RECORD_TYPE:
                continue
        except MemoryErrorWin32:
            continue
        friend = player_raw[0x13 + fid]
        if fid in TRIBE_NAMES:
            tribes.append(
                DiplomacyEntry(
                    force_id=fid,
                    address=fptr,
                    name=f"{TRIBE_NAMES[fid]}（异民族）",
                    ruler_id=0,
                    friendliness=friend,
                    is_tribe=True,
                )
            )
            continue
        ruler_id = int(mem.read_value(fptr + 0x10, "u16"))
        if ruler_id == 0:
            continue
        ruler_name = _read_officer_name(mem, person_arr, ruler_id) or f"势力#{fid}"
        forces.append(
            DiplomacyEntry(
                force_id=fid,
                address=fptr,
                name=f"{ruler_name}军",
                ruler_id=ruler_id,
                friendliness=friend,
                is_tribe=False,
            )
        )

    foreigns: List[ForeignRelationEntry] = []
    for fn_id, fn_name in FOREIGN_NAMES.items():
        friend = struct.unpack_from("<H", player_raw, 0x184 + fn_id * 2)[0]
        trade_lv = player_raw[0x18E + fn_id]
        foreigns.append(
            ForeignRelationEntry(
                foreign_id=fn_id,
                name=fn_name,
                friendliness=friend,
                trade_level=trade_lv,
            )
        )

    return forces, tribes, foreigns


def set_force_friendliness(mem: ProcessMemory, other_force_id: int, value: int) -> int:
    """Set mutual friendliness (0..100) between player force and another force or tribe."""
    ctx = read_world_context(mem)
    if not 1 <= other_force_id < MAX_FORCES or other_force_id == ctx.player_force_id:
        raise ValueError("目标势力 ID 无效。")
    clamped = max(0, min(100, int(value)))
    other_ptr = int(mem.read_value(ctx.mgr + MGR_FORCE_PTRS_OFFSET + other_force_id * 8, "u64"))
    if other_ptr < 0x10000 or int(mem.read_value(other_ptr, "u64")) != mem.base_address + FORCE_RECORD_TYPE:
        raise MemoryErrorWin32("无法校验目标势力数据结构。")

    mem.write_value(
        ctx.player_force_ptr + 0x13 + other_force_id,
        "u8",
        clamped,
        record_patch=True,
        label=f"外交.势力{other_force_id}.友好度",
    )
    mem.write_value(
        other_ptr + 0x13 + ctx.player_force_id,
        "u8",
        clamped,
        record_patch=True,
        label=f"外交.势力{other_force_id}.双向友好度",
    )
    return clamped


def set_all_diplomacy_max(mem: ProcessMemory, *, include_tribes: bool = True, value: int = 100) -> int:
    """Set friendliness with all active forces (and optionally all 5 tribes) to `value`."""
    forces, tribes, _ = list_diplomacy(mem)
    targets = list(forces) + (list(tribes) if include_tribes else [])
    for entry in targets:
        set_force_friendliness(mem, entry.force_id, value)
    return len(targets)


def set_foreign_relation(
    mem: ProcessMemory,
    foreign_id: int,
    *,
    friendliness: Optional[int] = None,
    trade_level: Optional[int] = None,
) -> ForeignRelationEntry:
    """Edit player force's friendliness (0..1000) and/or trade level (0..4) with a foreign nation."""
    ctx = read_world_context(mem)
    if foreign_id not in FOREIGN_NAMES:
        raise ValueError(f"无效海外诸国 ID: {foreign_id}")
    name = FOREIGN_NAMES[foreign_id]
    if friendliness is not None:
        val = max(0, min(1000, int(friendliness)))
        mem.write_value(
            ctx.player_force_ptr + 0x184 + foreign_id * 2,
            "u16",
            val,
            record_patch=True,
            label=f"通商.{name}.友好度",
        )
    if trade_level is not None:
        lv = max(0, min(4, int(trade_level)))
        mem.write_value(
            ctx.player_force_ptr + 0x18E + foreign_id,
            "u8",
            lv,
            record_patch=True,
            label=f"通商.{name}.等级",
        )
    cur_f = int(mem.read_value(ctx.player_force_ptr + 0x184 + foreign_id * 2, "u16"))
    cur_lv = int(mem.read_value(ctx.player_force_ptr + 0x18E + foreign_id, "u8"))
    return ForeignRelationEntry(foreign_id=foreign_id, name=name, friendliness=cur_f, trade_level=cur_lv)


# ---------------- Treasures / Items (CItemData) ----------------


def list_items(mem: ProcessMemory) -> List[TreasureEntry]:
    """List all named treasures (CItemData) with their current owner names."""
    mgr = _require_supported(mem)
    base = mem.base_address
    item_arr = int(mem.read_value(mgr + MGR_ITEM_ARR, "u64"))
    person_arr = int(mem.read_value(mgr + MGR_PERSON_ARR, "u64"))
    if item_arr < 0x10000:
        return []

    name_cache: Dict[int, str] = {}
    items: List[TreasureEntry] = []
    vt = base + ITEM_RECORD_TYPE
    for iid in range(1, MAX_ITEMS):
        addr = item_arr + iid * ITEM_STRIDE
        try:
            raw = mem.read_bytes(addr, 0x54)
        except MemoryErrorWin32:
            break
        if struct.unpack_from("<Q", raw, 0)[0] != vt:
            break
        name = _read_utf16z(raw, 0x10, 28)
        if not name or name == "无效":
            continue
        owner_id = struct.unpack_from("<H", raw, 0x4A)[0]
        area_id = struct.unpack_from("<H", raw, 0x4C)[0]
        status = raw[0x50]
        if owner_id:
            owner_name = name_cache.get(owner_id)
            if owner_name is None:
                owner_name = _read_officer_name(mem, person_arr, owner_id) or f"武将#{owner_id}"
                name_cache[owner_id] = owner_name
        else:
            owner_name = "无主"
        items.append(
            TreasureEntry(
                item_id=iid,
                address=addr,
                name=name,
                owner_id=owner_id,
                owner_name=owner_name,
                area_id=area_id,
                status=status,
            )
        )
    return items


def grant_item_to_officer(
    mem: ProcessMemory,
    item_addr: int,
    officer_id: int,
    *,
    item_name: str = "",
    officer_name: str = "",
) -> None:
    """Assign a treasure (CItemData) to an officer (owner_id, area_id=0, status=1)."""
    _require_supported(mem)
    if officer_id <= 0 or officer_id >= MAX_PERSONS:
        raise ValueError(f"无效的目标武将 ID: {officer_id}")
    if int(mem.read_value(item_addr, "u64")) != mem.base_address + ITEM_RECORD_TYPE:
        raise MemoryErrorWin32("名品内存地址校验失败。")

    tag = f"名品.{item_name or hex(item_addr)}->{officer_name or officer_id}"
    mem.write_value(item_addr + 0x4A, "u16", int(officer_id), record_patch=True, label=f"{tag}.持有者")
    mem.write_value(item_addr + 0x4C, "u16", 0, record_patch=True, label=f"{tag}.所在地区")
    mem.write_value(item_addr + 0x50, "u8", 1, record_patch=True, label=f"{tag}.状态")
    if int(mem.read_value(item_addr + 0x4A, "u16")) != int(officer_id):
        raise MemoryErrorWin32("名品归属写入后读回校验失败。")
