"""SAN14PK / Heritage Edition (.s14) save-file parser and officer trait/tactic editor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import struct
from typing import List, Optional, Sequence
import winreg

from lwc_codec import lwc_compress, lwc_decompress

SAVE_MAGIC = b"SN14SVEXVER0000\x00"
OFF_SURNAME = 0x00
OFF_GIVEN = 0x12
OFF_ZI = 0x24
OFF_PINYIN = 0x36
OFF_STATS = 0xDE
OFF_STATS_COMP = 0xE3
OFF_TRAITS = 0xE8
OFF_TACTICS = 0xFE
TRAIT_SLOT_COUNT = 5
TACTIC_SLOT_COUNT = 10
KNOWN_STRIDES = (0x1CA, 0x1BC)


def _read_utf16z(buf: bytes | bytearray, offset: int, max_bytes: int) -> str:
    raw = bytes(buf[offset : offset + max_bytes])
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw.decode("utf-16le", errors="replace")


@dataclass
class SaveMetadata:
    version: int
    ruler: str
    scenario: str
    force: str
    timestamp: str
    edition_label: str


@dataclass
class SaveOfficer:
    index: int
    offset: int
    surname: str
    given: str
    zi: str
    pinyin: str
    stats: tuple[int, int, int, int, int]
    traits: tuple[int, ...]
    tactics: tuple[int, ...]

    @property
    def full_name(self) -> str:
        return f"{self.surname}{self.given}"

    @property
    def display_label(self) -> str:
        zi_part = f"（字{self.zi}）" if self.zi else ""
        return f"#{self.index:04d} {self.full_name}{zi_part}  五维 {'/'.join(map(str, self.stats))}"


def discover_save_directories() -> List[Path]:
    """Locate Steam userdata save directories for SAN14 (AppID 872410)."""
    steam_roots: List[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            val, _ = winreg.QueryValueEx(key, "SteamPath")
            if val:
                steam_roots.append(Path(val))
    except OSError:
        pass
    for fallback in (
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
        Path(r"D:\Steam"),
        Path(r"E:\Steam"),
    ):
        if fallback not in steam_roots:
            steam_roots.append(fallback)

    found: List[Path] = []
    for root in steam_roots:
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for user_dir in userdata.iterdir():
            remote = user_dir / "872410" / "remote"
            if remote.is_dir() and remote not in found:
                found.append(remote)
    return found


def list_recent_save_files(directory: Path) -> List[Path]:
    """List playable SAN14 save files sorted by newest first."""
    if not directory.is_dir():
        return []
    saves = [
        p
        for p in directory.glob("*.s14")
        if p.name.lower().startswith(("svdex", "autosdex", "svd", "autosd"))
    ]
    return sorted(saves, key=lambda p: p.stat().st_mtime, reverse=True)


class SaveDocument:
    """In-memory representation of a decompressed SAN14 .s14 save file."""

    def __init__(
        self,
        path: Path,
        header: bytes,
        raw: bytearray,
        metadata: SaveMetadata,
        table_offset: int,
        officer_count: int,
        stride: int,
    ) -> None:
        self.path = path
        self.header = header
        self.raw = raw
        self.metadata = metadata
        self.table_offset = table_offset
        self.officer_count = officer_count
        self.stride = stride
        self.dirty = False
        self._officers: List[SaveOfficer] = self._parse_all_officers()

    @classmethod
    def load(cls, path: Path) -> "SaveDocument":
        blob = path.read_bytes()
        if len(blob) < 0x100 or blob[4:20] != SAVE_MAGIC:
            raise ValueError("该文件不是受支持的三国志14 威力加强版 / 传承版存档文件 (.s14)。")
        header, payload = lwc_decompress(blob)
        raw = bytearray(payload)
        metadata = cls._parse_metadata(header)
        table_offset, officer_count, stride = cls._locate_officer_table(raw)
        return cls(path, header, raw, metadata, table_offset, officer_count, stride)

    @staticmethod
    def _parse_metadata(header: bytes) -> SaveMetadata:
        version = struct.unpack_from("<I", header, 0)[0]
        ruler = _read_utf16z(header, 0x14, 36)
        scenario = _read_utf16z(header, 0x38, 60)
        force = _read_utf16z(header, 0x9A, 34) if len(header) >= 0xBC else ""
        if len(header) >= 0x84:
            yr, mo, _dow, day, hr, mn, sec, _ms = struct.unpack_from("<8H", header, 0x74)
            ts = f"{yr:04d}-{mo:02d}-{day:02d} {hr:02d}:{mn:02d}:{sec:02d}"
        else:
            ts = ""
        edition = "威力加强传承版 (CE)" if version == 0x66 else f"威力加强版 (v{hex(version)})"
        return SaveMetadata(
            version=version,
            ruler=ruler or "未知君主",
            scenario=scenario or "未知剧本",
            force=force or "",
            timestamp=ts,
            edition_label=edition,
        )

    @staticmethod
    def _locate_officer_table(raw: bytes | bytearray) -> tuple[int, int, int]:
        """Locate section 0x0A (officer records) and verify its stride against section 0x0B."""
        marker = "无效".encode("utf-16le") + b"\x00\x00"
        pos = 0
        while True:
            pos = raw.find(marker, pos)
            if pos < 0:
                break
            if pos >= 8:
                sid, count = struct.unpack_from("<II", raw, pos - 8)
                if sid == 0x0A and 500 <= count <= 4000:
                    # Try known strides first, then search for section 0x0B (next section ID).
                    for stride in KNOWN_STRIDES:
                        end = pos + count * stride
                        if end + 8 <= len(raw):
                            next_sid, next_cnt = struct.unpack_from("<II", raw, end)
                            if next_sid == 0x0B and 1 <= next_cnt <= 256:
                                return pos, count, stride
                    # Dynamic stride fallback (range 0x180..0x240)
                    for stride in range(0x180, 0x240, 2):
                        end = pos + count * stride
                        if end + 8 <= len(raw):
                            next_sid, next_cnt = struct.unpack_from("<II", raw, end)
                            if next_sid == 0x0B and next_cnt == 0x15:
                                return pos, count, stride
            pos += 2
        raise ValueError("未能定位存档中的武将数据表 (section 0x0A)。")

    def _read_officer(self, index: int) -> SaveOfficer:
        base = self.table_offset + index * self.stride
        surname = _read_utf16z(self.raw, base + OFF_SURNAME, 18)
        given = _read_utf16z(self.raw, base + OFF_GIVEN, 18)
        zi = _read_utf16z(self.raw, base + OFF_ZI, 18)
        pinyin = _read_utf16z(self.raw, base + OFF_PINYIN, 36)
        stats = tuple(self.raw[base + OFF_STATS : base + OFF_STATS + 5])
        traits = struct.unpack_from("<5H", self.raw, base + OFF_TRAITS)
        tactics = tuple(self.raw[base + OFF_TACTICS : base + OFF_TACTICS + TACTIC_SLOT_COUNT])
        return SaveOfficer(
            index=index,
            offset=base,
            surname=surname,
            given=given,
            zi=zi,
            pinyin=pinyin,
            stats=stats,  # type: ignore[arg-type]
            traits=traits,
            tactics=tactics,
        )

    def _parse_all_officers(self) -> List[SaveOfficer]:
        officers: List[SaveOfficer] = []
        for idx in range(1, self.officer_count):
            off = self._read_officer(idx)
            if off.full_name and off.full_name != "无效":
                officers.append(off)
        return officers

    @property
    def officers(self) -> List[SaveOfficer]:
        return self._officers

    def get_officer(self, index: int) -> Optional[SaveOfficer]:
        for off in self._officers:
            if off.index == index:
                return off
        return None

    def search_officers(self, query: str = "") -> List[SaveOfficer]:
        q = query.strip().lower()
        if not q:
            return list(self._officers)
        return [
            off
            for off in self._officers
            if q in off.full_name.lower()
            or q in off.zi.lower()
            or q in off.pinyin.lower()
            or q == str(off.index)
        ]

    def set_officer_traits(self, officer_index: int, traits: Sequence[int]) -> SaveOfficer:
        if not 1 <= officer_index < self.officer_count:
            raise IndexError(f"无效武将索引: {officer_index}")
        values = [int(v) for v in traits[:TRAIT_SLOT_COUNT]]
        while len(values) < TRAIT_SLOT_COUNT:
            values.append(0)
        if any(not 0 <= v <= 355 for v in values):
            raise ValueError(f"个性 ID 超出范围 (0~355): {values}")
        base = self.table_offset + officer_index * self.stride
        struct.pack_into("<5H", self.raw, base + OFF_TRAITS, *values)
        self.dirty = True
        return self._refresh_officer(officer_index)

    def set_officer_tactics(self, officer_index: int, tactics: Sequence[int]) -> SaveOfficer:
        if not 1 <= officer_index < self.officer_count:
            raise IndexError(f"无效武将索引: {officer_index}")
        values = [int(v) for v in tactics[:TACTIC_SLOT_COUNT]]
        while len(values) < TACTIC_SLOT_COUNT:
            values.append(0)
        if any(not 0 <= v <= 200 for v in values):
            raise ValueError(f"战法 ID 超出范围 (0~200): {values}")
        base = self.table_offset + officer_index * self.stride
        self.raw[base + OFF_TACTICS : base + OFF_TACTICS + TACTIC_SLOT_COUNT] = bytes(values)
        self.dirty = True
        return self._refresh_officer(officer_index)

    def set_officer_stats(self, officer_index: int, stats: Sequence[int]) -> SaveOfficer:
        if not 1 <= officer_index < self.officer_count:
            raise IndexError(f"无效武将索引: {officer_index}")
        if len(stats) != 5:
            raise ValueError("五维必须为 5 项。")
        values = [max(1, min(120, int(v))) for v in stats]
        comps = [max(0, 100 - v) for v in values]
        base = self.table_offset + officer_index * self.stride
        self.raw[base + OFF_STATS : base + OFF_STATS + 5] = bytes(values)
        self.raw[base + OFF_STATS_COMP : base + OFF_STATS_COMP + 5] = bytes(comps)
        self.dirty = True
        return self._refresh_officer(officer_index)

    def _refresh_officer(self, officer_index: int) -> SaveOfficer:
        updated = self._read_officer(officer_index)
        for pos, existing in enumerate(self._officers):
            if existing.index == officer_index:
                self._officers[pos] = updated
                break
        return updated

    def save(self, target_path: Optional[Path] = None, *, create_backup: bool = True) -> Path:
        """Compress, verify round-trip, optionally back up, and atomically write the .s14 file."""
        dest = target_path or self.path
        payload_bytes = bytes(self.raw)
        packed = self.header + lwc_compress(payload_bytes)
        # Round-trip verification before touching disk!
        verify_hdr, verify_raw = lwc_decompress(packed)
        if verify_hdr != self.header or verify_raw != payload_bytes:
            raise RuntimeError("存档重压缩校验失败，已取消写入以保护原存档。")

        if create_backup and dest.exists():
            backup = dest.with_suffix(dest.suffix + ".bak")
            if not backup.exists():
                shutil.copy2(dest, backup)

        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(packed)
        tmp.replace(dest)
        self.path = dest
        self.dirty = False
        return dest
