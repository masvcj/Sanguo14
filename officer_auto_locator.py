"""Resolve open details in the tested Simplified Chinese Heritage executable."""

import ctypes
from ctypes import wintypes
from hashlib import file_digest
import struct

from memory_engine import MemoryErrorWin32, ProcessMemory, SCAN_CHUNK_SIZE
from officer_probe import HERITAGE_STATS_OFFSET


TESTED_IMAGE = "2e49348aa1b9be5cfca651e71c8d77b2e4bd2d11387edc0b0236f4352458a239"
# The UI allocator provides a starting point for finding the open detail objects.
UI_HEAP_POINTER = 28987768
UI_HEAP_RADIUS = 0x380000
DETAIL_SELECTOR = (0x14B04F8, 0x178)
PANEL_SELECTOR = (0x1471ED0, 0x148)
HOVER_SELECTOR = (0x13486B0, 0x168)
DETAIL_SELECTORS = {
    "officer": DETAIL_SELECTOR,
    "city": (0x14AE3E0, 0x178),
    "area": (0x14AD2B0, 0x178),
    "gate": (0x14AFB08, 0x188),
    "unit": (0x14AD970, 0x178),
}
CITY_RECORD_TYPE = 0x13AB6E0
CITY_FIELDS = {
    "city_durability": (0x46, "u16", 60000),
    "city_morale": (0x44, "u8", 200),
    "city_gold": (0x34, "u32", 9999999),
    "city_food": (0x38, "u32", 9999999),
    "city_troops": (0x3C, "u32", 999999),
    "city_wounded": (0x40, "u32", 999999),
    "city_provoked": (0x4B, "u8", 63),
    "city_disorder": (0x49, "u8", 63),
    "city_scale": (0xB8, "u8", 4),
}
OFFICER_RECORD_TYPE = 0x13ABBD8
OFFICER_STAT_CAPS_OFFSET = 0x130
OFFICER_EXTRA_FIELDS = {
    "loyalty": (0x120, "u8", 255),
    "policy_lv": (0x1BA, "u8", 10),
    "birth_year": (0xE4, "u16", 400),
    "illness": (0x199, "u8", 3),
}
AREA_RECORD_TYPE = 0x13AB9D0
AREA_FIELDS = {
    "area_commerce": (0x3C, "u16", 1000),
    "area_agri": (0x3E, "u16", 1000),
    "area_barracks": (0x40, "u16", 1000),
    "area_pop": (0x52, "u16", 65535),
    "area_order": (0x54, "u8", 100),
    "area_build_cap": (0x6E, "u8", 99),
}
GATE_RECORD_TYPE = 0x13ABC40
GATE_FIELDS = {
    "gate_durability": (0x46, "u16", 60000),
    "gate_morale": (0x44, "u8", 200),
    "gate_troops": (0x3C, "u32", 999999),
    "gate_wounded": (0x40, "u32", 999999),
    "gate_provoked": (0x4B, "u8", 63),
    "gate_disorder": (0x49, "u8", 63),
}
UNIT_RECORD_TYPE = 0x1347458
UNIT_FIELDS = {
    "unit_troops": (0x16, "u16", 65535),
    "unit_wounded": (0x18, "u16", 65535),
    "unit_morale": (0x1A, "u8", 200),
    "unit_disorder": (0x23, "u8", 63),
    "unit_restrained": (0x24, "u8", 63),
    "unit_provoked": (0x25, "u8", 63),
    "unit_gold": (0x5C, "u32", 9999999),
    "unit_food": (0x60, "u32", 9999999),
}
# SAN14 stores abnormal-status timers as 4 simulation ticks per in-game day (10 days = 40 ticks).
LIVE_FIELD_SCALES = {
    "city_disorder": 4,
    "city_provoked": 4,
    "gate_disorder": 4,
    "gate_provoked": 4,
    "unit_disorder": 4,
    "unit_restrained": 4,
    "unit_provoked": 4,
}
LIVE_RECORD_FIELDS = {"city": CITY_FIELDS, "area": AREA_FIELDS, "gate": GATE_FIELDS, "unit": UNIT_FIELDS}
LIVE_DETAIL_KINDS = tuple(DETAIL_SELECTORS)

# Master GameData manager offsets for SAN14CE_SC.exe (verified against live process).
MANAGER_PTR_RVA = 0x2783158
PLAYER_FORCE_ID_RVA = 0x27833FC
FORCE_RECORD_TYPE = 0x13AB890
DISTRICT_RECORD_TYPE = 0x13AB900
MGR_FORCE_PTRS_OFFSET = 0xDF30
MGR_DISTRICT_PTRS_OFFSET = 0xE0D0
LIVE_GLOBAL_FIELDS = ("inf_orders", "max_admin")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def supported_executable(mem: ProcessMemory) -> bool:
    if not mem.handle or not mem.base_address or mem.process_name.lower() != "san14ce_sc.exe":
        return False
    image_path = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(image_path))
    if not kernel32.QueryFullProcessImageNameW(mem.handle, 0, image_path, ctypes.byref(size)):
        return False
    try:
        with open(image_path.value, "rb") as executable:
            return file_digest(executable, "sha256").hexdigest() == TESTED_IMAGE
    except OSError:
        return False


def _selector_objects(
    mem: ProcessMemory, heap: int, selectors: tuple[tuple[int, int], ...]
) -> list[list[int]]:
    signatures = [(mem.base_address + rva).to_bytes(8, "little") for rva, _ in selectors]
    found: list[list[int]] = [[] for _ in selectors]
    for base, size in mem.iter_writable_regions(start=heap - UI_HEAP_RADIUS, stop=heap + UI_HEAP_RADIUS):
        for offset in range(0, size, SCAN_CHUNK_SIZE):
            block = min(size - offset, SCAN_CHUNK_SIZE)
            try:
                data = mem.read_bytes(base + offset, min(size - offset, block + 7))
            except MemoryErrorWin32:
                continue
            for signature, objects in zip(signatures, found):
                index = data.find(signature)
                while 0 <= index < block:
                    address = base + offset + index
                    if address % 8 == 0:
                        objects.append(address)
                    index = data.find(signature, index + 1)
    return found


def _read_record(mem: ProcessMemory, address: int, selector: tuple[int, int]) -> int:
    type_rva, offset = selector
    if mem.read_value(address, "u64") != mem.base_address + type_rva:
        return 0
    return int(mem.read_value(address + offset, "u64"))


def read_live_targets(
    mem: ProcessMemory, kinds: tuple[str, ...], *, reject_hover: bool = False
) -> dict[str, tuple[int, bytes]]:
    """Resolve the open details with a matching attribute panel."""
    heap = int(mem.read_value(mem.base_address + UI_HEAP_POINTER, "u64"))
    if heap < 0x10000:
        return {}
    selectors = (PANEL_SELECTOR, *(DETAIL_SELECTORS[kind] for kind in kinds))
    if reject_hover:
        selectors += (HOVER_SELECTOR,)
    panels, *details = _selector_objects(mem, heap, selectors)
    panel_records = {_read_record(mem, obj, PANEL_SELECTOR): obj for obj in panels}
    targets = {}
    for kind, objects in zip(kinds, details):
        selector = DETAIL_SELECTORS[kind]
        detail_records = {_read_record(mem, obj, selector): obj for obj in objects}
        candidates = detail_records.keys() & panel_records.keys()
        verified = []
        for record in candidates:
            if record < 0x10000 or record % 8:
                continue
            try:
                selection = _read_target_fields(mem, record, kind)
            except MemoryErrorWin32:
                continue
            if selection is not None:
                verified.append((record, selection))
        if len(verified) != 1:
            continue
        record, selection = verified[0]
        if reject_hover and kind == "officer" and any(
            _read_record(mem, obj, HOVER_SELECTOR) == record for obj in details[-1]
        ):
            raise MemoryErrorWin32("请移开武将列表上的鼠标，再使用快捷键。")
        if (_read_record(mem, detail_records[record], selector) == record and
                _read_record(mem, panel_records[record], PANEL_SELECTOR) == record):
            targets[kind] = selection
    return targets


def _read_target_fields(
    mem: ProcessMemory, record: int, kind: str
) -> tuple[int, bytes] | None:
    if kind == "officer":
        address = record + HERITAGE_STATS_OFFSET
        values = mem.read_bytes(address, 5)
        if len(values) == 5 and any(values) and all(value <= 120 for value in values):
            return address, values
    elif kind == "city":
        if mem.read_value(record, "u64") != mem.base_address + CITY_RECORD_TYPE:
            return None
        values = mem.read_bytes(record + 0x34, 24)
        scale = mem.read_bytes(record + 0xB8, 1)
        if len(values) == 24 and len(scale) == 1:
            gold, food, troops, wounded, morale, _, durability, _, _, _, _ = struct.unpack("<IIIIBBHBBBB", values)
            if (gold <= 9999999 and food <= 9999999 and troops <= 999999
                    and wounded <= 999999 and morale <= 200 and durability <= 60000):
                return record, values + scale
    elif kind == "area":
        if mem.read_value(record, "u64") != mem.base_address + AREA_RECORD_TYPE:
            return None
        dev = mem.read_bytes(record + 0x3C, 6)
        pop_order = mem.read_bytes(record + 0x52, 3)
        build_cap = mem.read_bytes(record + 0x6E, 1)
        if len(dev) == 6 and len(pop_order) == 3 and len(build_cap) == 1:
            commerce, agri, barracks = struct.unpack("<HHH", dev)
            if commerce <= 1000 and agri <= 1000 and barracks <= 1000 and pop_order[2] <= 100:
                return record, pop_order + dev + build_cap
    elif kind == "gate":
        if mem.read_value(record, "u64") != mem.base_address + GATE_RECORD_TYPE:
            return None
        values = mem.read_bytes(record + 0x3C, 16)
        if len(values) == 16:
            troops, wounded, morale, _, durability, _, disorder, _, provoked = struct.unpack("<IIBBHBBBB", values)
            if troops <= 999999 and wounded <= 999999 and morale <= 200 and durability <= 60000:
                return record, values
    elif kind == "unit":
        if mem.read_value(record, "u64") != mem.base_address + UNIT_RECORD_TYPE:
            return None
        core = mem.read_bytes(record + 0x16, 5)
        if len(core) == 5 and core[4] <= 200:
            status = mem.read_bytes(record + 0x23, 3)
            cargo = mem.read_bytes(record + 0x5C, 8)
            if len(status) == 3 and len(cargo) == 8:
                gold, food = struct.unpack("<II", cargo)
                if gold <= 9999999 and food <= 9999999:
                    return record, core + status + cargo
    return None


def read_selected_officer(mem: ProcessMemory, *, reject_hover: bool = False) -> tuple[int, bytes]:
    target = read_live_targets(mem, ("officer",), reject_hover=reject_hover).get("officer")
    if target is None:
        raise MemoryErrorWin32("请在游戏中打开目标武将的详情页。")
    return target


def resolve_live_global_field(mem: ProcessMemory, field_key: str) -> tuple[int, str, int, int, int] | None:
    """Resolve a live global field to (address, dtype, target_value, min_val, max_val) in SAN14CE_SC.exe."""
    if field_key not in LIVE_GLOBAL_FIELDS:
        return None
    if not mem.is_attached or not mem.base_address:
        raise MemoryErrorWin32("请先连接游戏并进入正在游玩的剧本。")
    base = mem.base_address
    mgr = int(mem.read_value(base + MANAGER_PTR_RVA, "u64"))
    if mgr < 0x10000:
        raise MemoryErrorWin32("尚未进入剧本，无法读取全局势力数据。")
    player_force_id = int(mem.read_value(base + PLAYER_FORCE_ID_RVA, "i32"))
    if not 1 <= player_force_id <= 51:
        raise MemoryErrorWin32("未检测到正在操控的玩家势力，请确认已进入剧本。")

    if field_key == "max_admin":
        force_ptr = int(mem.read_value(mgr + MGR_FORCE_PTRS_OFFSET + player_force_id * 8, "u64"))
        if force_ptr < 0x10000 or int(mem.read_value(force_ptr, "u64")) != base + FORCE_RECORD_TYPE:
            raise MemoryErrorWin32("无法校验玩家势力数据结构。")
        return force_ptr + 0x164, "u32", 9999999, 0, 9999999

    if field_key == "inf_orders":
        fallback_ptr = 0
        for did in range(1, 52):
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
                if dist_num == 1:
                    return dptr + 0x14, "u8", 99, 0, 99
                if fallback_ptr == 0:
                    fallback_ptr = dptr
        if fallback_ptr:
            return fallback_ptr + 0x14, "u8", 99, 0, 99
        raise MemoryErrorWin32("未找到玩家直辖军团，无法定位命令书。")
    return None


