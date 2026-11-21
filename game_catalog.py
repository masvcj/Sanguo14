"""Trait (个性) and Tactic (战法/战技) catalog for SAN14PK Heritage Edition (SAN14CE)."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import struct
from typing import Dict, List, Optional, Sequence

from lwc_codec import unpack_s14_file

DEFAULT_CATALOG_PATH = Path(__file__).parent / "profiles" / "san14ce_catalog.json"

TRAIT_COLOR_NAMES = {
    3: "金色",
    1: "蓝色",
    2: "红色",
    0: "特殊",
}

TACTIC_CATEGORY_NAMES = {
    0: "无效",
    1: "战斗",
    2: "射击",
    3: "强化",
    4: "削弱",
    5: "计略",
    6: "辅助",
    7: "攻城",
    8: "水战",
}

# Verified reference signatures from SAN14CE Simplified Chinese (fixdataexce / scedaexce01).
# Used for majority-vote verification of live officer struct offsets in memory.
REFERENCE_OFFICERS: tuple[dict, ...] = (
    {"surname": "曹", "given": "操", "zi": "孟德", "traits": (14, 59, 27, 32, 49), "tactics": (94, 3, 10, 15, 18, 23)},
    {"surname": "吕", "given": "布", "zi": "奉先", "traits": (67, 62, 1, 60, 41), "tactics": (93, 3, 4, 11, 17)},
    {"surname": "关", "given": "羽", "zi": "云长", "traits": (150, 62, 59, 21, 139), "tactics": (91, 2, 9, 14, 16, 24)},
    {"surname": "诸葛", "given": "亮", "zi": "孔明", "traits": (151, 81, 9, 138, 120), "tactics": (97, 6, 13, 19, 21, 23)},
    {"surname": "周", "given": "瑜", "zi": "公瑾", "traits": (53, 59, 23, 68, 46), "tactics": (99, 5, 7, 18, 25, 26)},
    {"surname": "刘", "given": "备", "zi": "玄德", "traits": (97, 59, 88, 21, 66), "tactics": (95, 4, 5, 7)},
    {"surname": "张", "given": "飞", "zi": "翼德", "traits": (108, 62, 145, 1, 35), "tactics": (92, 2, 11, 16, 17)},
    {"surname": "赵", "given": "云", "zi": "子龙", "traits": (37, 62, 92, 98, 86), "tactics": (96, 6, 8, 10, 14)},
    {"surname": "司马", "given": "懿", "zi": "仲达", "traits": (52, 4, 91, 36, 56), "tactics": (83, 4, 8, 17, 20, 23)},
    {"surname": "孙", "given": "策", "zi": "伯符", "traits": (11, 59, 61, 39, 42), "tactics": (46, 2, 11, 18, 24, 25)},
    {"surname": "荀", "given": "彧", "zi": "文若", "traits": (46, 138, 109, 119, 120), "tactics": (39, 14, 15, 18, 23)},
    {"surname": "郭", "given": "嘉", "zi": "奉孝", "traits": (48, 4, 189, 55, 122), "tactics": (40, 13, 14, 15, 17)},
    {"surname": "黄", "given": "忠", "zi": "汉升", "traits": (144, 11, 6, 3, 44), "tactics": (60, 5, 6, 10, 17)},
    {"surname": "马", "given": "超", "zi": "孟起", "traits": (106, 62, 4, 1, 42), "tactics": (59, 2, 11, 12, 17)},
)


def _read_utf16z(buf: bytes, offset: int, max_bytes: int) -> str:
    raw = buf[offset : offset + max_bytes]
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw.decode("utf-16le", errors="replace")


@dataclass(frozen=True)
class TraitInfo:
    id: int
    name: str
    pinyin: str
    color: int  # 3=金色, 1=蓝色, 2=红色, 0=特殊
    description: str

    @property
    def color_label(self) -> str:
        return TRAIT_COLOR_NAMES.get(self.color, "特殊")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "pinyin": self.pinyin,
            "color": self.color,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TraitInfo":
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            pinyin=str(data.get("pinyin", "")),
            color=int(data.get("color", 0)),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class TacticInfo:
    id: int
    name: str
    pinyin: str
    category: int
    gauge: int
    tier: str  # "通用" | "固有" | "DLC"

    @property
    def category_label(self) -> str:
        return TACTIC_CATEGORY_NAMES.get(self.category, "其他")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "pinyin": self.pinyin,
            "category": self.category,
            "gauge": self.gauge,
            "tier": self.tier,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TacticInfo":
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            pinyin=str(data.get("pinyin", "")),
            category=int(data.get("category", 0)),
            gauge=int(data.get("gauge", 0)),
            tier=str(data.get("tier", "固有")),
        )


class GameCatalog:
    """In-memory lookup and search catalog for SAN14PK Heritage traits and tactics."""

    def __init__(
        self,
        traits: Sequence[TraitInfo],
        tactics: Sequence[TacticInfo],
        *,
        source: str = "bundled",
    ) -> None:
        self.source = source
        self.traits: List[TraitInfo] = sorted(traits, key=lambda t: t.id)
        self.tactics: List[TacticInfo] = sorted(tactics, key=lambda t: t.id)
        self.trait_by_id: Dict[int, TraitInfo] = {t.id: t for t in self.traits}
        self.tactic_by_id: Dict[int, TacticInfo] = {t.id: t for t in self.tactics}
        self.max_trait_id = max((t.id for t in self.traits), default=255)
        self.max_tactic_id = max((t.id for t in self.tactics), default=200)

    def upsert_trait(self, info: TraitInfo) -> None:
        """Add or update a custom/runtime trait in the catalog."""
        self.trait_by_id[info.id] = info
        self.traits = sorted(self.trait_by_id.values(), key=lambda t: t.id)
        self.max_trait_id = max((t.id for t in self.traits), default=255)

    def remove_trait(self, trait_id: int) -> None:
        """Remove a custom trait from the catalog."""
        if trait_id in self.trait_by_id:
            del self.trait_by_id[trait_id]
            self.traits = sorted(self.trait_by_id.values(), key=lambda t: t.id)
            self.max_trait_id = max((t.id for t in self.traits), default=255)

    def trait_name(self, trait_id: int) -> str:
        if trait_id == 0:
            return "无"
        info = self.trait_by_id.get(trait_id)
        return info.name if info else f"个性#{trait_id}"

    def trait_summary(self, trait_id: int) -> str:
        if trait_id == 0:
            return "（空槽位）"
        info = self.trait_by_id.get(trait_id)
        if info is None:
            return f"未知个性 #{trait_id}"
        return f"[{info.id:03d}] {info.name}（{info.color_label}）— {info.description}"

    def tactic_name(self, tactic_id: int) -> str:
        if tactic_id == 0:
            return "无"
        info = self.tactic_by_id.get(tactic_id)
        return info.name if info else f"战法#{tactic_id}"

    def tactic_summary(self, tactic_id: int) -> str:
        if tactic_id == 0:
            return "（空槽位）"
        info = self.tactic_by_id.get(tactic_id)
        if info is None:
            return f"未知战法 #{tactic_id}"
        return f"[{info.id:03d}] {info.name}（{info.tier} · {info.category_label}）"

    def search_traits(self, query: str = "", color_filter: Optional[int] = None) -> List[TraitInfo]:
        q = query.strip().lower()
        results: List[TraitInfo] = []
        for item in self.traits:
            if color_filter is not None and item.color != color_filter:
                continue
            if q:
                if (
                    q not in item.name.lower()
                    and q not in item.pinyin.lower()
                    and q not in item.description.lower()
                    and q != str(item.id)
                ):
                    continue
            results.append(item)
        return results

    def search_tactics(
        self,
        query: str = "",
        tier_filter: Optional[str] = None,
        category_filter: Optional[int] = None,
    ) -> List[TacticInfo]:
        q = query.strip().lower()
        results: List[TacticInfo] = []
        for item in self.tactics:
            if tier_filter and item.tier != tier_filter:
                continue
            if category_filter is not None and item.category != category_filter:
                continue
            if q:
                if (
                    q not in item.name.lower()
                    and q not in item.pinyin.lower()
                    and q not in item.category_label.lower()
                    and q != str(item.id)
                ):
                    continue
            results.append(item)
        return results

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "game_version": "SAN14CE_SC",
            "source": self.source,
            "traits": [t.to_dict() for t in self.traits],
            "tactics": [t.to_dict() for t in self.tactics],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "GameCatalog":
        data = json.loads(path.read_text(encoding="utf-8"))
        traits = [TraitInfo.from_dict(x) for x in data.get("traits", [])]
        tactics = [TacticInfo.from_dict(x) for x in data.get("tactics", [])]
        return cls(traits, tactics, source=str(data.get("source", path.name)))

    @classmethod
    def parse_fixdata_raw(cls, raw: bytes, source: str = "fixdata") -> "GameCatalog":
        """Parse trait (section 0x16, stride 0xAA) and tactic (section 0x0C, stride 0x5E) tables."""
        # Locate tactic table by section header (0x0C, count) followed by '无效' + 'Wuxiao'
        tactic_marker = "无效".encode("utf-16le") + b"\x00\x00\x00\x00\x00\x00" + "Wuxiao".encode("utf-16le")
        tactic_pos = -1
        pos = 0
        while True:
            pos = raw.find(tactic_marker, pos)
            if pos < 0:
                break
            if pos >= 8 and struct.unpack_from("<I", raw, pos - 8)[0] == 0x0C:
                tactic_pos = pos
                break
            pos += 2
        if tactic_pos < 0:
            raise ValueError("未能在 fixdata 中找到战法数据表 (section 0x0C)。")
        tactic_count = struct.unpack_from("<I", raw, tactic_pos - 4)[0]

        tactics: List[TacticInfo] = []
        for idx in range(1, tactic_count):
            base = tactic_pos + idx * 0x5E
            if base + 0x5E > len(raw):
                break
            name = _read_utf16z(raw, base, 10)
            pinyin = _read_utf16z(raw, base + 10, 38)
            category = raw[base + 48]
            gauge = raw[base + 49]
            if not name or category == 0:
                continue
            if idx <= 26:
                tier = "通用"
            elif idx >= 170:
                tier = "DLC"
            else:
                tier = "固有"
            tactics.append(
                TacticInfo(
                    id=idx,
                    name=name,
                    pinyin=pinyin,
                    category=category,
                    gauge=gauge,
                    tier=tier,
                )
            )

        # Locate trait table by section header (0x16, count) followed by '无效' + 'Wuxiao'
        trait_marker = "无效".encode("utf-16le") + b"\x00\x00\x00\x00\x00\x00\x00\x00" + "Wuxiao".encode("utf-16le")
        trait_pos = -1
        pos = 0
        while True:
            pos = raw.find(trait_marker, pos)
            if pos < 0:
                break
            if pos >= 8 and struct.unpack_from("<I", raw, pos - 8)[0] == 0x16:
                trait_pos = pos
                break
            pos += 2
        if trait_pos < 0:
            raise ValueError("未能在 fixdata 中找到个性数据表 (section 0x16)。")
        trait_count = struct.unpack_from("<I", raw, trait_pos - 4)[0]

        traits: List[TraitInfo] = []
        for idx in range(1, trait_count):
            base = trait_pos + idx * 0xAA
            if base + 0xAA > len(raw):
                break
            name = _read_utf16z(raw, base, 12)
            pinyin = _read_utf16z(raw, base + 12, 32)
            description = _read_utf16z(raw, base + 44, 104)
            color = raw[base + 158]
            if not name or name.startswith("个性") or description == "说明文":
                continue
            traits.append(
                TraitInfo(
                    id=idx,
                    name=name,
                    pinyin=pinyin,
                    color=color,
                    description=description,
                )
            )

        return cls(traits, tactics, source=source)

    @classmethod
    def load_from_fixdata(cls, path: Path) -> "GameCatalog":
        _, raw = unpack_s14_file(path)
        return cls.parse_fixdata_raw(raw, source=path.name)

    @classmethod
    def load_default(cls, path: Optional[Path] = None) -> "GameCatalog":
        target = path or DEFAULT_CATALOG_PATH
        return cls.load_json(target)
