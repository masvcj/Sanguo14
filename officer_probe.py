"""Read-only A/B/A probe for the selected officer in one game session."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
from pathlib import Path
import re
from time import monotonic

from memory_engine import MemoryErrorWin32, ProcessMemory, SCAN_CHUNK_SIZE

RECORD_NEIGHBORHOOD = 0x1000
POINTER_SIZE = 8
HERITAGE_OFFICER_STRIDE = 0x228
HERITAGE_STATS_OFFSET = 0x124

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
kernel32.GetProcessTimes.restype = wintypes.BOOL


def process_start_time(mem: ProcessMemory) -> int:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not mem.handle or not kernel32.GetProcessTimes(mem.handle, *(ctypes.byref(value) for value in times)):
        raise MemoryErrorWin32("无法确认当前游戏进程的启动时间。")
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def foreground_process_id() -> int:
    window = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    if window:
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return pid.value


def find_record_references(mem: ProcessMemory, stats_address: int) -> dict[int, int]:
    """Return pointer slots and their signed distance from the five stats."""
    earliest = max(1, stats_address - RECORD_NEIGHBORHOOD)
    latest = stats_address + RECORD_NEIGHBORHOOD
    # Pointers may refer to any field of the record. Search their common high bytes.
    suffixes = {n.to_bytes(POINTER_SIZE, "little")[2:] for n in (earliest, latest)}
    suffix = re.compile(b"|".join(re.escape(s) for s in sorted(suffixes)))
    slots: dict[int, int] = {}
    deadline = monotonic() + 45
    for base, size in mem.iter_writable_regions():
        for offset in range(0, size, SCAN_CHUNK_SIZE):
            if monotonic() >= deadline:
                raise MemoryErrorWin32("查找武将引用超时")
            address = base + offset
            try:
                size_to_read = min(SCAN_CHUNK_SIZE + POINTER_SIZE - 1, size - offset)
                data = mem.read_bytes(address, size_to_read)
            except MemoryErrorWin32:
                continue
            cursor = 2
            while match := suffix.search(data, cursor):
                start = match.start() - 2
                cursor = match.start() + 1
                if start >= SCAN_CHUNK_SIZE:
                    continue
                ptr = int.from_bytes(data[start:start + POINTER_SIZE], "little")
                if earliest <= ptr <= latest:
                    slot = address + start
                    slots[slot] = stats_address - ptr
                    if len(slots) > 10000:
                        raise MemoryErrorWin32("武将引用候选过多")
    return slots


def match_record_references(mem: ProcessMemory, slots: dict[int, int], stats_address: int) -> dict[int, int]:
    matched = {}
    for slot, offset in slots.items():
        try:
            if mem.read_value(slot, "u64") == stats_address - offset:
                matched[slot] = offset
        except MemoryErrorWin32:
            continue
    return matched


@dataclass(frozen=True)
class OfficerFollowTarget:
    pid: int
    handles: tuple[tuple[int, int], ...]
    anchor: int

    def save(self, mem: ProcessMemory, file: Path) -> None:
        payload = {"pid": self.pid, "started": process_start_time(mem),
                   "anchor": self.anchor, "references": self.handles}
        temporary = file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(file)

    @classmethod
    def load(cls, mem: ProcessMemory, file: Path) -> "OfficerFollowTarget | None":
        if not mem.pid or mem.process_name.lower() != "san14ce_sc.exe" or not file.is_file():
            return None
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            if payload["pid"] != mem.pid or payload["started"] != process_start_time(mem):
                return None
            anchor = payload["anchor"]
            references = tuple((address, offset) for address, offset in payload["references"])
            if (type(anchor) is not int or anchor <= 0 or not 2 <= len(references) <= 10000
                or len(set(references)) != len(references)
                or any(type(address) is not int or address <= 0 or address % 8 or offset != HERITAGE_STATS_OFFSET
                       for address, offset in references)):
                return None
            return cls(mem.pid, references, anchor)
        except (OSError, ValueError, TypeError, KeyError, MemoryErrorWin32):
            return None

    def read_current(self, mem: ProcessMemory, required_foreground: int) -> tuple[int, bytes]:
        if mem.pid != self.pid or foreground_process_id() != required_foreground:
            raise MemoryErrorWin32("请先打开游戏中的目标武将详情。")
        if len(self.handles) < 2:
            raise MemoryErrorWin32("自动跟随需要至少两条一致的引用。")
        targets = []
        for slot, offset in self.handles:
            pointer = int(mem.read_value(slot, "u64"))
            if pointer <= 0:
                raise MemoryErrorWin32("当前没有可用的武将引用。")
            targets.append(pointer + offset)
        address = targets[0]
        if any(target != address for target in targets) or (address - self.anchor) % HERITAGE_OFFICER_STRIDE:
            raise MemoryErrorWin32("武将引用不一致，未能确认当前目标。")
        stats = mem.read_bytes(address, 5)
        if len(stats) != 5 or any(value > 120 for value in stats):
            raise MemoryErrorWin32("当前武将五维未通过校验。")
        if foreground_process_id() != required_foreground:
            raise MemoryErrorWin32("前台窗口已切换，请核对武将后再试。")
        return address, stats

    def resolve(self, mem: ProcessMemory) -> int:
        return self.read_current(mem, self.pid)[0]
