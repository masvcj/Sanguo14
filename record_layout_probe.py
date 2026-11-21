"""Compare candidate field layouts for two distinct in-game records."""

from collections import defaultdict
from itertools import product
from math import prod
import struct

from memory_engine import MemoryErrorWin32, ProcessMemory, TYPE_FORMATS


def _candidate_layouts(mem: ProcessMemory, values: tuple[int, ...], types: tuple[str, ...],
                       radius: int) -> dict[tuple[int, ...], set[int]]:
    patterns = [struct.pack(TYPE_FORMATS[kind][0], value) for value, kind in zip(values, types, strict=True)]
    seed_hits = mem.scan_pattern(patterns[0], max_results=257)
    if len(seed_hits) > 256:
        raise MemoryErrorWin32("首个数值重复太多，请用更有区分度的数值作为首项。")
    layouts: dict[tuple[int, ...], set[int]] = defaultdict(set)
    for anchor in seed_hits:
        start = max(1, anchor - radius)
        try:
            window = mem.read_bytes(start, anchor - start + radius + max(map(len, patterns)))
        except MemoryErrorWin32:
            continue
        ranges = []
        for pattern in patterns[1:]:
            hits = []
            offset = window.find(pattern)
            while offset >= 0 and len(hits) <= 32:
                address = start + offset
                if address + len(pattern) <= anchor or address >= anchor + len(patterns[0]):
                    hits.append(address - anchor)
                offset = window.find(pattern, offset + 1)
            if not hits or len(hits) > 32:
                break
            ranges.append(hits)
        if len(ranges) != len(patterns) - 1:
            continue
        if prod(map(len, ranges)) > 4096:
            raise MemoryErrorWin32("候选字段组合太多，请缩小范围或选择更有区分度的数值。")
        for layout in product(*ranges):
            spans = sorted((position, position + len(pattern))
                           for position, pattern in zip(layout, patterns[1:], strict=True))
            if all(left[1] <= right[0] for left, right in zip(spans, spans[1:])):
                layouts[layout].add(anchor)
    return layouts


def compare_record_layouts(mem: ProcessMemory, first: tuple[int, ...], second: tuple[int, ...],
                           types: tuple[str, ...], radius: int = 192) -> dict[tuple[int, ...], tuple[int, int]]:
    """Find field offsets repeated around different records without writing to the game."""
    if not 3 <= len(types) <= 5 or len(first) != len(types) or len(second) != len(types):
        raise ValueError("Each record needs three to five observed fields.")
    if first[0] == second[0] or not 1 <= radius <= 512:
        raise ValueError("Use different seed values and a radius between 1 and 512 bytes.")
    initial = _candidate_layouts(mem, first, types, radius)
    compared = _candidate_layouts(mem, second, types, radius)
    matched = {}
    for offsets in initial.keys() & compared.keys():
        if len(initial[offsets]) == len(compared[offsets]) == 1:
            left, right = next(iter(initial[offsets])), next(iter(compared[offsets]))
            if left != right:
                matched[(0, *offsets)] = (left, right)
    return matched
