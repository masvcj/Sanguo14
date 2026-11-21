"""Live in-memory officer trait (个性) and tactic (战法) locator & editor for SAN14CE."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from game_catalog import REFERENCE_OFFICERS
from memory_engine import MemoryErrorWin32, ProcessMemory, TYPE_FORMATS
from officer_auto_locator import read_live_targets, supported_executable
from officer_probe import HERITAGE_OFFICER_STRIDE, HERITAGE_STATS_OFFSET

TRAIT_SLOTS = 5
TACTIC_SLOTS = 10
MAX_TRAIT_ID = 355
MAX_TACTIC_ID = 200
_MIN_CONSENSUS = 3
_TABLE_SEARCH_RADIUS = 0x180000  # Covers > 2800 officers * 0x228 bytes each side.


def _read_utf16z(buf: bytes, offset: int, max_bytes: int) -> str:
    if offset < 0 or offset + 2 > len(buf):
        return ""
    raw = buf[offset : min(len(buf), offset + max_bytes)]
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw.decode("utf-16le", errors="replace")


@dataclass(frozen=True)
class LiveOfficerLayout:
    """Verified runtime byte layout of an officer record in the attached game process."""

    pid: int
    stride: int
    stats_offset: int
    traits_offset: int
    trait_dtype: str  # "u16" | "u8" | "u32"
    trait_slots: int
    tactics_offset: int
    tactic_dtype: str  # "u8" | "u16" | "u32"
    tactic_slots: int
    surname_offset: Optional[int]
    given_offset: Optional[int]
    zi_offset: Optional[int]
    name_ptr_offset: Optional[int]
    table_start: int
    officer_count: int
    verified_matches: int


@dataclass
class LiveOfficerRecord:
    address: int
    index: int
    surname: str
    given: str
    zi: str
    stats: Tuple[int, int, int, int, int]
    traits: Tuple[int, ...]
    tactics: Tuple[int, ...]

    @property
    def full_name(self) -> str:
        name = f"{self.surname}{self.given}".strip()
        return name if name else f"武将#{self.index:04d}"

    @property
    def display_label(self) -> str:
        zi_part = f"（字{self.zi}）" if self.zi else ""
        return f"#{self.index:04d} {self.full_name}{zi_part}  五维 {'/'.join(map(str, self.stats))}"


def _encode_values(values: Sequence[int], dtype: str) -> bytes:
    fmt, _ = TYPE_FORMATS[dtype]
    return b"".join(struct.pack(fmt, v) for v in values)


def _decode_array(buf: bytes, offset: int, count: int, dtype: str) -> Optional[Tuple[int, ...]]:
    fmt, size = TYPE_FORMATS[dtype]
    end = offset + count * size
    if offset < 0 or end > len(buf):
        return None
    return tuple(struct.unpack_from(fmt, buf, offset + i * size)[0] for i in range(count))


def _read_region_around(mem: ProcessMemory, anchor: int, radius: int = _TABLE_SEARCH_RADIUS) -> Tuple[int, bytes]:
    """Read the writable memory region containing `anchor`, clamped to [anchor-radius, anchor+radius]."""
    for base, size in mem.iter_writable_regions(start=max(0x10000, anchor - radius), stop=anchor + radius):
        if base <= anchor < base + size:
            lo = max(base, anchor - radius)
            hi = min(base + size, anchor + radius)
            return lo, mem.read_bytes(lo, hi - lo)
    raise MemoryErrorWin32(f"无法读取包含地址 {hex(anchor)} 的可写内存区域。")


def _detect_layout_in_buffer(
    mem: ProcessMemory,
    buf_base: int,
    buf: bytes,
    anchor_record: int,
    stride: int = HERITAGE_OFFICER_STRIDE,
    stats_offset: int = HERITAGE_STATS_OFFSET,
) -> LiveOfficerLayout:
    """Derive & consensus-verify trait, tactic, and name offsets from a contiguous officer buffer."""
    align_mod = (anchor_record - buf_base) % stride
    first_slot_off = align_mod
    num_slots = (len(buf) - first_slot_off) // stride
    if num_slots < _MIN_CONSENSUS:
        raise MemoryErrorWin32("武将表内存窗口过小，无法完成多武将交叉校验。")

    # 1. Locate trait offset & dtype via consensus across REFERENCE_OFFICERS.
    trait_votes: Counter[Tuple[int, str]] = Counter()
    trait_hits_by_key: Dict[Tuple[int, str], List[Tuple[dict, int]]] = {}

    for dtype in ("u16", "u8", "u32"):
        _, item_sz = TYPE_FORMATS[dtype]
        for ref in REFERENCE_OFFICERS:
            if dtype == "u8" and any(v > 255 for v in ref["traits"]):
                continue
            pat = _encode_values(ref["traits"], dtype)
            pos = buf.find(pat)
            while pos >= 0:
                if pos >= first_slot_off:
                    slot_idx = (pos - first_slot_off) // stride
                    intra = (pos - first_slot_off) % stride
                    if intra % item_sz == 0 and intra + len(pat) <= stride:
                        key = (intra, dtype)
                        slot_off = first_slot_off + slot_idx * stride
                        # Avoid double-counting same ref in same key
                        existing = trait_hits_by_key.setdefault(key, [])
                        if all(r is not ref for r, _ in existing):
                            existing.append((ref, slot_off))
                            trait_votes[key] += 1
                pos = buf.find(pat, pos + 1)

    if not trait_votes:
        raise MemoryErrorWin32("未能在武将内存中匹配到已知武将的个性签名，请确认已进入剧本。")

    (traits_offset, trait_dtype), trait_match_count = trait_votes.most_common(1)[0]
    if trait_match_count < _MIN_CONSENSUS:
        raise MemoryErrorWin32(
            f"个性结构交叉校验数量不足（仅匹配 {trait_match_count} 名参考武将，需要至少 {_MIN_CONSENSUS} 名）。"
        )

    matched_refs = trait_hits_by_key[(traits_offset, trait_dtype)]

    # 2. Within those exact matched officer records, find tactics_offset & tactic_dtype via consensus.
    tactic_votes: Counter[Tuple[int, str]] = Counter()
    for dtype in ("u8", "u16", "u32"):
        _, item_sz = TYPE_FORMATS[dtype]
        for ref, slot_off in matched_refs:
            rec = buf[slot_off : slot_off + stride]
            base_tactics = ref["tactics"]
            lead = base_tactics[0]
            base_set = set(base_tactics)
            for off in range(0, stride - TACTIC_SLOTS * item_sz + 1, item_sz):
                arr = _decode_array(rec, off, TACTIC_SLOTS, dtype)
                if arr is None:
                    continue
                if arr[0] != lead:
                    continue
                if any(v > MAX_TACTIC_ID for v in arr):
                    continue
                if base_set.issubset(set(arr)):
                    tactic_votes[(off, dtype)] += 1

    if not tactic_votes:
        raise MemoryErrorWin32("未能在武将内存中匹配到已知武将的战法签名。")

    (tactics_offset, tactic_dtype), tactic_match_count = tactic_votes.most_common(1)[0]
    if tactic_match_count < _MIN_CONSENSUS:
        raise MemoryErrorWin32(
            f"战法结构交叉校验数量不足（仅匹配 {tactic_match_count} 名参考武将，需要至少 {_MIN_CONSENSUS} 名）。"
        )

    # 3. Locate inline UTF-16LE name fields (surname, given, zi) or u64 pointer if present.
    surname_votes: Counter[int] = Counter()
    given_votes: Counter[int] = Counter()
    zi_votes: Counter[int] = Counter()
    for ref, slot_off in matched_refs:
        rec = buf[slot_off : slot_off + stride]
        for field_key, counter in (("surname", surname_votes), ("given", given_votes), ("zi", zi_votes)):
            pat = ref[field_key].encode("utf-16le") + b"\x00\x00"
            pos = rec.find(pat)
            while pos >= 0:
                if pos % 2 == 0:
                    counter[pos] += 1
                pos = rec.find(pat, pos + 2)

    surname_offset = surname_votes.most_common(1)[0][0] if surname_votes and surname_votes.most_common(1)[0][1] >= _MIN_CONSENSUS else None
    given_offset = given_votes.most_common(1)[0][0] if given_votes and given_votes.most_common(1)[0][1] >= _MIN_CONSENSUS else None
    zi_offset = zi_votes.most_common(1)[0][0] if zi_votes and zi_votes.most_common(1)[0][1] >= _MIN_CONSENSUS else None
    name_ptr_offset: Optional[int] = None

    # If inline UTF-16LE wasn't found, check whether an 8-byte-aligned pointer inside the record
    # points to a name struct containing surname/given/zi.
    if surname_offset is None:
        ptr_votes: Counter[Tuple[int, int, int, Optional[int]]] = Counter()
        for ref, slot_off in matched_refs[:8]:
            rec = buf[slot_off : slot_off + stride]
            sur_pat = ref["surname"].encode("utf-16le") + b"\x00\x00"
            giv_pat = ref["given"].encode("utf-16le") + b"\x00\x00"
            zi_pat = ref["zi"].encode("utf-16le") + b"\x00\x00"
            for off in range(0, stride - 8 + 1, 8):
                ptr = struct.unpack_from("<Q", rec, off)[0]
                if not 0x10000 <= ptr < 0x7FFFFFFFFFFF or ptr % 8:
                    continue
                try:
                    target = mem.read_bytes(ptr, 0x60)
                except MemoryErrorWin32:
                    continue
                sp = target.find(sur_pat)
                gp = target.find(giv_pat)
                if sp >= 0 and gp >= 0 and sp != gp:
                    zp = target.find(zi_pat)
                    ptr_votes[(off, sp, gp, zp if zp >= 0 else None)] += 1
        if ptr_votes and ptr_votes.most_common(1)[0][1] >= _MIN_CONSENSUS:
            name_ptr_offset, surname_offset, given_offset, zi_offset = ptr_votes.most_common(1)[0][0]

    # 4. Determine contiguous officer table boundaries (table_start .. table_end) within `buf`.
    min_matched_slot = min(slot_off for _, slot_off in matched_refs)
    max_matched_slot = max(slot_off for _, slot_off in matched_refs)

    def _looks_like_officer_slot(slot_off: int) -> bool:
        if slot_off < 0 or slot_off + stride > len(buf):
            return False
        rec = buf[slot_off : slot_off + stride]
        stats = rec[stats_offset : stats_offset + 5]
        if len(stats) != 5 or any(v > 120 for v in stats):
            return False
        trs = _decode_array(rec, traits_offset, TRAIT_SLOTS, trait_dtype)
        tacs = _decode_array(rec, tactics_offset, TACTIC_SLOTS, tactic_dtype)
        if trs is None or tacs is None:
            return False
        if any(v > MAX_TRAIT_ID for v in trs) or any(v > MAX_TACTIC_ID for v in tacs):
            return False
        return True

    start_slot = min_matched_slot
    while start_slot - stride >= 0 and _looks_like_officer_slot(start_slot - stride):
        if (max_matched_slot - (start_slot - stride)) // stride >= 2048:
            break
        start_slot -= stride

    end_slot = max_matched_slot
    while end_slot + stride * 2 <= len(buf) and _looks_like_officer_slot(end_slot + stride):
        if (end_slot + stride - start_slot) // stride >= 2048:
            break
        end_slot += stride

    officer_count = (end_slot - start_slot) // stride + 1

    return LiveOfficerLayout(
        pid=mem.pid or 0,
        stride=stride,
        stats_offset=stats_offset,
        traits_offset=traits_offset,
        trait_dtype=trait_dtype,
        trait_slots=TRAIT_SLOTS,
        tactics_offset=tactics_offset,
        tactic_dtype=tactic_dtype,
        tactic_slots=TACTIC_SLOTS,
        surname_offset=surname_offset,
        given_offset=given_offset,
        zi_offset=zi_offset,
        name_ptr_offset=name_ptr_offset,
        table_start=buf_base + start_slot,
        officer_count=officer_count,
        verified_matches=min(trait_match_count, tactic_match_count),
    )


def calibrate_live_layout_from_record(
    mem: ProcessMemory,
    record_address: int,
    *,
    stride: int = HERITAGE_OFFICER_STRIDE,
    stats_offset: int = HERITAGE_STATS_OFFSET,
) -> LiveOfficerLayout:
    """Calibrate and verify the live officer layout given one live officer record pointer."""
    buf_base, buf = _read_region_around(mem, record_address)
    return _detect_layout_in_buffer(mem, buf_base, buf, record_address, stride=stride, stats_offset=stats_offset)


def scan_live_officer_layout(
    mem: ProcessMemory,
    *,
    stride: int = HERITAGE_OFFICER_STRIDE,
    stats_offset: int = HERITAGE_STATS_OFFSET,
) -> LiveOfficerLayout:
    """Locate the live officer table directly in writable memory without requiring an open detail panel."""
    # First try fast path if an officer detail panel is currently open in SAN14CE_SC.exe.
    if supported_executable(mem):
        try:
            target = read_live_targets(mem, ("officer",)).get("officer")
            if target is not None:
                record_addr = target[0] - stats_offset
                return calibrate_live_layout_from_record(mem, record_addr, stride=stride, stats_offset=stats_offset)
        except MemoryErrorWin32:
            pass

    # Fallback: scan writable memory for distinctive trait signatures of Guan Yu, Zhuge Liang, Lu Bu, Cao Cao.
    seed_refs = REFERENCE_OFFICERS[2], REFERENCE_OFFICERS[3], REFERENCE_OFFICERS[1], REFERENCE_OFFICERS[0]
    for dtype in ("u16", "u8"):
        for ref in seed_refs:
            if dtype == "u8" and any(v > 255 for v in ref["traits"]):
                continue
            pat = _encode_values(ref["traits"], dtype)
            hits = mem.scan_pattern(pat, max_results=32, align=2 if dtype == "u16" else 1)
            for hit in hits:
                # Candidate record start: try aligning via stats_offset or via common trait offset.
                # Note: inside _detect_layout_in_buffer, we only need `anchor_record % stride` to match
                # the true record start! We can find exact record start by checking where `stats_offset`
                # lands relative to Guan Yu/Zhuge Liang/Lu Bu/Cao Cao's stats, or scanning intra-record.
                try:
                    buf_base, buf = _read_region_around(mem, hit)
                except MemoryErrorWin32:
                    continue
                # Try finding the true record start by locating the officer's 5 stats (within +/- 0x100 of hit).
                # Specifically, look for `stats_offset` alignment where multiple reference officers have valid stats.
                candidate_starts = _guess_record_starts_from_trait_hit(buf_base, buf, hit, ref, stride, stats_offset)
                for rec_addr in candidate_starts:
                    try:
                        return _detect_layout_in_buffer(
                            mem, buf_base, buf, rec_addr, stride=stride, stats_offset=stats_offset
                        )
                    except MemoryErrorWin32:
                        continue
    raise MemoryErrorWin32(
        "未能自动定位当前剧本的武将数据表。请确认已进入剧本（非主菜单），或先在游戏中打开任意武将详情页后再试。"
    )


def _guess_record_starts_from_trait_hit(
    buf_base: int,
    buf: bytes,
    trait_hit: int,
    ref: dict,
    stride: int,
    stats_offset: int,
) -> List[int]:
    """Estimate the exact record base address containing `trait_hit`."""
    candidates: List[int] = []
    # Most likely: traits are +0x0A after stats (like save file), or nearby within +0x04..+0x40 after stats.
    for delta in (0x0A, 0x0C, 0x08, 0x0E, 0x10, 0x12, 0x14, 0x06):
        rec = trait_hit - (stats_offset + delta)
        if buf_base <= rec < buf_base + len(buf) - stride:
            candidates.append(rec)
    # Also check courtesy name (zi) match near trait_hit (in save file zi is 0xC4 bytes before traits).
    zi_pat = ref["zi"].encode("utf-16le") + b"\x00\x00"
    search_lo = max(0, trait_hit - buf_base - stride)
    search_hi = min(len(buf), trait_hit - buf_base + stride)
    zp = buf.find(zi_pat, search_lo, search_hi)
    if zp >= 0:
        for zi_off in (0x24, 0x2C, 0x30, 0x34, 0x6A):
            rec = buf_base + zp - zi_off
            if rec not in candidates and buf_base <= rec < buf_base + len(buf) - stride:
                candidates.append(rec)
    return candidates


def read_live_officer(mem: ProcessMemory, layout: LiveOfficerLayout, record_address: int) -> LiveOfficerRecord:
    """Read one officer's live name, stats, traits, and tactics from game memory."""
    rec = mem.read_bytes(record_address, layout.stride)
    stats = tuple(rec[layout.stats_offset : layout.stats_offset + 5])
    traits = _decode_array(rec, layout.traits_offset, layout.trait_slots, layout.trait_dtype)
    tactics = _decode_array(rec, layout.tactics_offset, layout.tactic_slots, layout.tactic_dtype)
    if len(stats) != 5 or traits is None or tactics is None:
        raise MemoryErrorWin32("读取武将内存数据失败。")
    if any(v > MAX_TRAIT_ID for v in traits) or any(v > MAX_TACTIC_ID for v in tactics):
        raise MemoryErrorWin32("当前地址的个性/战法数值未通过范围校验，已阻止读取。")

    surname = ""
    given = ""
    zi = ""
    if layout.name_ptr_offset is not None and layout.surname_offset is not None:
        ptr = struct.unpack_from("<Q", rec, layout.name_ptr_offset)[0]
        if 0x10000 <= ptr < 0x7FFFFFFFFFFF:
            try:
                name_buf = mem.read_bytes(ptr, 0x60)
                surname = _read_utf16z(name_buf, layout.surname_offset, 18)
                if layout.given_offset is not None:
                    given = _read_utf16z(name_buf, layout.given_offset, 18)
                if layout.zi_offset is not None:
                    zi = _read_utf16z(name_buf, layout.zi_offset, 18)
            except MemoryErrorWin32:
                pass
    elif layout.surname_offset is not None:
        surname = _read_utf16z(rec, layout.surname_offset, 18)
        if layout.given_offset is not None:
            given = _read_utf16z(rec, layout.given_offset, 18)
        if layout.zi_offset is not None:
            zi = _read_utf16z(rec, layout.zi_offset, 18)

    index = (record_address - layout.table_start) // layout.stride
    return LiveOfficerRecord(
        address=record_address,
        index=index,
        surname=surname,
        given=given,
        zi=zi,
        stats=stats,  # type: ignore[arg-type]
        traits=traits,
        tactics=tactics,
    )


def list_live_officers(mem: ProcessMemory, layout: LiveOfficerLayout) -> List[LiveOfficerRecord]:
    """Read all active/named officers from the calibrated live officer table in one bulk read."""
    total_bytes = layout.officer_count * layout.stride
    raw = mem.read_bytes(layout.table_start, total_bytes)
    officers: List[LiveOfficerRecord] = []
    for idx in range(layout.officer_count):
        off = idx * layout.stride
        rec = raw[off : off + layout.stride]
        stats = tuple(rec[layout.stats_offset : layout.stats_offset + 5])
        if len(stats) != 5 or any(v > 120 for v in stats) or not any(stats):
            continue
        traits = _decode_array(rec, layout.traits_offset, layout.trait_slots, layout.trait_dtype)
        tactics = _decode_array(rec, layout.tactics_offset, layout.tactic_slots, layout.tactic_dtype)
        if traits is None or tactics is None:
            continue
        if any(v > MAX_TRAIT_ID for v in traits) or any(v > MAX_TACTIC_ID for v in tactics):
            continue
        surname = ""
        given = ""
        zi = ""
        if layout.name_ptr_offset is not None and layout.surname_offset is not None:
            ptr = struct.unpack_from("<Q", rec, layout.name_ptr_offset)[0]
            if 0x10000 <= ptr < 0x7FFFFFFFFFFF:
                try:
                    nbuf = mem.read_bytes(ptr, 0x60)
                    surname = _read_utf16z(nbuf, layout.surname_offset, 18)
                    if layout.given_offset is not None:
                        given = _read_utf16z(nbuf, layout.given_offset, 18)
                    if layout.zi_offset is not None:
                        zi = _read_utf16z(nbuf, layout.zi_offset, 18)
                except MemoryErrorWin32:
                    pass
        elif layout.surname_offset is not None:
            surname = _read_utf16z(rec, layout.surname_offset, 18)
            if layout.given_offset is not None:
                given = _read_utf16z(rec, layout.given_offset, 18)
            if layout.zi_offset is not None:
                zi = _read_utf16z(rec, layout.zi_offset, 18)
        if f"{surname}{given}" == "无效":
            continue
        # Skip empty placeholder slots when names are available.
        if (layout.surname_offset is not None) and not (surname or given):
            continue
        officers.append(
            LiveOfficerRecord(
                address=layout.table_start + off,
                index=idx,
                surname=surname,
                given=given,
                zi=zi,
                stats=stats,  # type: ignore[arg-type]
                traits=traits,
                tactics=tactics,
            )
        )
    return officers


def write_live_traits(
    mem: ProcessMemory,
    layout: LiveOfficerLayout,
    record_address: int,
    traits: Sequence[int],
    *,
    label: str = "",
) -> Tuple[int, ...]:
    """Safely write 5 trait IDs to a live officer record and verify exact read-back."""
    values = [int(v) for v in traits[: layout.trait_slots]]
    while len(values) < layout.trait_slots:
        values.append(0)
    if any(not 0 <= v <= MAX_TRAIT_ID for v in values):
        raise ValueError(f"个性 ID 超出允许范围 (0~{MAX_TRAIT_ID}): {values}")
    if layout.trait_dtype == "u8" and any(v > 255 for v in values):
        raise ValueError(f"当前内存个性字段为 u8，无法写入大于 255 的 ID: {values}")

    # Pre-write sanity check on current record
    read_live_officer(mem, layout, record_address)

    _, item_sz = TYPE_FORMATS[layout.trait_dtype]
    base = record_address + layout.traits_offset
    payload = _encode_values(values, layout.trait_dtype)
    mem.write_bytes(base, payload, record_patch=True, label=label or f"officer@{hex(record_address)}.个性")
    readback = mem.read_bytes(base, len(payload))
    if readback != payload:
        raise MemoryErrorWin32("个性写入后读回校验不一致，请核对游戏内状态。")
    return tuple(values)


def write_live_tactics(
    mem: ProcessMemory,
    layout: LiveOfficerLayout,
    record_address: int,
    tactics: Sequence[int],
    *,
    label: str = "",
) -> Tuple[int, ...]:
    """Safely write 10 tactic IDs to a live officer record and verify exact read-back."""
    values = [int(v) for v in tactics[: layout.tactic_slots]]
    while len(values) < layout.tactic_slots:
        values.append(0)
    if any(not 0 <= v <= MAX_TACTIC_ID for v in values):
        raise ValueError(f"战法 ID 超出允许范围 (0~{MAX_TACTIC_ID}): {values}")

    # Pre-write sanity check on current record
    read_live_officer(mem, layout, record_address)

    base = record_address + layout.tactics_offset
    payload = _encode_values(values, layout.tactic_dtype)
    mem.write_bytes(base, payload, record_patch=True, label=label or f"officer@{hex(record_address)}.战法")
    readback = mem.read_bytes(base, len(payload))
    if readback != payload:
        raise MemoryErrorWin32("战法写入后读回校验不一致，请核对游戏内状态。")
    return tuple(values)
