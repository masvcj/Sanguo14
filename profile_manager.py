"""结构体偏移方案管理与通用结构体读写。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory_engine import ProcessMemory, TYPE_FORMATS


def parse_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    if isinstance(val, int):
        return val
    s = str(val).strip().lower()
    if s.startswith("-0x"):
        return -int(s[3:], 16)
    if s.startswith("0x"):
        return int(s[2:], 16)
    return int(s, 10)


@dataclass
class FieldDef:
    key: str
    label: str
    offset: int
    dtype: str = "u8"
    min_val: int = 0
    max_val: int = 255
    category: str = "基础"
    note: str = ""
    hotkey: str = ""
    mode: str = "edit"  # "toggle" | "edit"
    default_value: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FieldDef":
        return cls(
            key=str(data["key"]),
            label=str(data.get("label", data["key"])),
            offset=parse_int(data.get("offset", 0)),
            dtype=str(data.get("dtype", "u8")),
            min_val=int(data.get("min_val", 0)),
            max_val=int(data.get("max_val", 255)),
            category=str(data.get("category", "基础")),
            note=str(data.get("note", "")),
            hotkey=str(data.get("hotkey", "")),
            mode=str(data.get("mode", "edit")),
            default_value=int(data.get("default_value", 0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "offset": hex(self.offset) if self.offset >= 0 else f"-{hex(-self.offset)}",
            "dtype": self.dtype,
            "min_val": self.min_val,
            "max_val": self.max_val,
            "category": self.category,
            "note": self.note,
            "hotkey": self.hotkey,
            "mode": self.mode,
            "default_value": self.default_value,
        }


@dataclass
class StructTableDef:
    key: str
    label: str
    base_address: int = 0
    stride: int = 0
    count: int = 1
    fields: List[FieldDef] = field(default_factory=list)

    @classmethod
    def from_dict(cls, key: str, data: Dict[str, Any]) -> "StructTableDef":
        return cls(
            key=key,
            label=str(data.get("label", key)),
            base_address=parse_int(data.get("base_address", 0)),
            stride=parse_int(data.get("stride", 0)),
            count=int(data.get("count", 1)),
            fields=[FieldDef.from_dict(f) for f in data.get("fields", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "base_address": hex(self.base_address),
            "stride": hex(self.stride),
            "count": self.count,
            "fields": [f.to_dict() for f in self.fields],
        }

    def record_address(self, index: int) -> int:
        return self.base_address + index * self.stride

    def read_record(self, mem: ProcessMemory, record_addr: int) -> Dict[str, int | float]:
        values: Dict[str, int | float] = {}
        for f in self.fields:
            if f.dtype not in TYPE_FORMATS:
                continue
            values[f.key] = mem.read_value(record_addr + f.offset, f.dtype)
        return values

    def write_record(
        self,
        mem: ProcessMemory,
        record_addr: int,
        updates: Dict[str, int | float],
        record_label: str = "",
    ) -> int:
        written = 0
        field_map = {f.key: f for f in self.fields}
        for key, raw_val in updates.items():
            f = field_map.get(key)
            if f is None or f.dtype not in TYPE_FORMATS:
                continue
            val = raw_val
            if f.dtype != "f32":
                val = max(f.min_val, min(f.max_val, int(raw_val)))
            mem.write_value(
                record_addr + f.offset,
                f.dtype,
                val,
                record_patch=True,
                label=f"{record_label}.{f.label}" if record_label else f.label,
            )
            written += 1
        return written


@dataclass
class TrainerProfile:
    name: str
    game_version: str
    process_names: List[str]
    stat_dtype: str
    tables: Dict[str, StructTableDef]

    @classmethod
    def load(cls, path: Path) -> "TrainerProfile":
        data = json.loads(path.read_text(encoding="utf-8"))
        tables = {
            k: StructTableDef.from_dict(k, v)
            for k, v in data.get("tables", {}).items()
        }
        return cls(
            name=str(data.get("name", path.stem)),
            game_version=str(data.get("game_version", "三国志14 威力加强版 传承版")),
            process_names=list(data.get("process_names", ["SAN14PK.exe", "SAN14.exe"])),
            stat_dtype=str(data.get("stat_dtype", "u8")),
            tables=tables,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": self.name,
            "game_version": self.game_version,
            "process_names": self.process_names,
            "stat_dtype": self.stat_dtype,
            "tables": {k: v.to_dict() for k, v in self.tables.items()},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_table(self, key: str) -> Optional[StructTableDef]:
        return self.tables.get(key)
