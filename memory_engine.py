"""Windows 内存读写与扫描引擎（基于 ctypes + kernel32/psapi，无需第三方依赖）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import struct
from time import monotonic
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_ALL_ACCESS_MIN = (
    PROCESS_QUERY_INFORMATION
    | PROCESS_VM_OPERATION
    | PROCESS_VM_READ
    | PROCESS_VM_WRITE
)

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE_PROTECT = {
    0x02,  # PAGE_READONLY
    0x04,  # PAGE_READWRITE
    0x08,  # PAGE_WRITECOPY
    0x20,  # PAGE_EXECUTE_READ
    0x40,  # PAGE_EXECUTE_READWRITE
    0x80,  # PAGE_EXECUTE_WRITECOPY
}
WRITABLE_PROTECT = {
    0x04,  # PAGE_READWRITE
    0x08,  # PAGE_WRITECOPY
    0x40,  # PAGE_EXECUTE_READWRITE
    0x80,  # PAGE_EXECUTE_WRITECOPY
}
SCAN_CHUNK_SIZE = 1024 * 1024

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class SYSTEM_INFO(ctypes.Structure):
    _fields_ = [
        ("wProcessorArchitecture", wintypes.WORD),
        ("wReserved", wintypes.WORD),
        ("dwPageSize", wintypes.DWORD),
        ("lpMinimumApplicationAddress", ctypes.c_void_p),
        ("lpMaximumApplicationAddress", ctypes.c_void_p),
        ("dwActiveProcessorMask", ctypes.c_size_t),
        ("dwNumberOfProcessors", wintypes.DWORD),
        ("dwProcessorType", wintypes.DWORD),
        ("dwAllocationGranularity", wintypes.DWORD),
        ("wProcessorLevel", wintypes.WORD),
        ("wProcessorRevision", wintypes.WORD),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.GetSystemInfo.argtypes = [ctypes.POINTER(SYSTEM_INFO)]
kernel32.GetSystemInfo.restype = None
kernel32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.VirtualProtectEx.restype = wintypes.BOOL
kernel32.FlushInstructionCache.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_size_t,
]
kernel32.FlushInstructionCache.restype = wintypes.BOOL

PAGE_EXECUTE_READWRITE = 0x40


TYPE_FORMATS = {
    "u8": ("<B", 1),
    "i8": ("<b", 1),
    "u16": ("<H", 2),
    "i16": ("<h", 2),
    "u32": ("<I", 4),
    "i32": ("<i", 4),
    "u64": ("<Q", 8),
    "i64": ("<q", 8),
    "f32": ("<f", 4),
}


@dataclass
class ProcessInfo:
    pid: int
    name: str


@dataclass
class MemoryPatch:
    address: int
    original_bytes: bytes
    new_bytes: bytes
    label: str
    is_code: bool = False


class MemoryErrorWin32(RuntimeError):
    """Windows 内存操作异常。"""


def list_processes() -> List[ProcessInfo]:
    """枚举当前系统运行中的进程。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise MemoryErrorWin32(f"CreateToolhelp32Snapshot 失败 (err={ctypes.get_last_error()})")
    results: List[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                results.append(ProcessInfo(pid=entry.th32ProcessID, name=entry.szExeFile))
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return results


def find_game_processes(
    candidates: Sequence[str] = (
        "SAN14CE_SC.exe", "SAN14CE_TC.exe", "SAN14CE.exe", "SAN14CE_KO.exe",
        "SAN14PK.exe", "SAN14.exe", "San14PK_TC.exe", "San14PK_SC.exe",
    )
) -> List[ProcessInfo]:
    """按常见《三国志14》进程名查找匹配进程。"""
    priority = {c.lower(): i for i, c in enumerate(candidates)}
    procs = list_processes()
    exact = [p for p in procs if p.name.lower() in priority]
    if exact:
        return sorted(exact, key=lambda p: priority[p.name.lower()])
    # A trainer EXE also contains "SAN14" in its name; never attach to it as the game.
    return []


def get_main_module_base(pid: int) -> Tuple[int, int]:
    """获取目标进程主模块的基址与大小 (base_addr, size)。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        return (0, 0)
    try:
        mod = MODULEENTRY32W()
        mod.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if kernel32.Module32FirstW(snap, ctypes.byref(mod)):
            base = ctypes.cast(mod.modBaseAddr, ctypes.c_void_p).value or 0
            return (base, int(mod.modBaseSize))
    finally:
        kernel32.CloseHandle(snap)
    return (0, 0)


class ProcessMemory:
    """封装对目标进程的内存读写、指针解析、扫描与修改撤销。"""

    def __init__(self) -> None:
        self.handle: Optional[int] = None
        self.pid: Optional[int] = None
        self.process_name: str = ""
        self.base_address: int = 0
        self.module_size: int = 0
        self.patch_history: List[MemoryPatch] = []

    @property
    def is_attached(self) -> bool:
        return self.handle is not None

    def attach(self, pid: int, process_name: str = "", *, read_only: bool = False) -> None:
        self.detach()
        access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ if read_only else PROCESS_ALL_ACCESS_MIN
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            raise MemoryErrorWin32(
                f"无法打开进程 PID={pid} (Win32 错误码 {err})。请尝试以管理员身份运行修改器。"
            )
        self.handle = handle
        self.pid = pid
        self.process_name = process_name
        self.base_address, self.module_size = get_main_module_base(pid)

    def detach(self) -> None:
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
        self.handle = None
        self.pid = None
        self.process_name = ""
        self.base_address = 0
        self.module_size = 0

    def _ensure_attached(self) -> int:
        if self.handle is None:
            raise MemoryErrorWin32("尚未附加到任何游戏进程。")
        return self.handle

    def read_bytes(self, address: int, size: int) -> bytes:
        handle = self._ensure_attached()
        if address <= 0 or size <= 0:
            raise MemoryErrorWin32(f"无效的读取参数: addr={hex(address)}, size={size}")
        buf = (ctypes.c_ubyte * size)()
        bytes_read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            handle, ctypes.c_void_p(address), buf, size, ctypes.byref(bytes_read)
        )
        if not ok or bytes_read.value == 0:
            err = ctypes.get_last_error()
            raise MemoryErrorWin32(f"读取内存 {hex(address)} 失败 (err={err})")
        return ctypes.string_at(ctypes.addressof(buf), bytes_read.value)

    def write_bytes(self, address: int, data: bytes, record_patch: bool = True, label: str = "") -> None:
        handle = self._ensure_attached()
        if address <= 0 or not data:
            raise MemoryErrorWin32(f"无效的写入参数: addr={hex(address)}")
        original = b""
        if record_patch:
            try:
                original = self.read_bytes(address, len(data))
            except MemoryErrorWin32:
                original = b""
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        bytes_written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            handle, ctypes.c_void_p(address), buf, len(data), ctypes.byref(bytes_written)
        )
        if not ok or bytes_written.value != len(data):
            err = ctypes.get_last_error()
            raise MemoryErrorWin32(f"写入内存 {hex(address)} 失败 (err={err})")
        if record_patch and original:
            self.patch_history.append(
                MemoryPatch(
                    address=address,
                    original_bytes=original,
                    new_bytes=bytes(data),
                    label=label or hex(address),
                )
            )

    def write_code_bytes(
        self, address: int, data: bytes, record_patch: bool = True, label: str = ""
    ) -> None:
        """向只读可执行代码段写入字节（自动切换页保护并刷新指令缓存）。"""
        handle = self._ensure_attached()
        if address <= 0 or not data:
            raise MemoryErrorWin32(f"无效的代码写入参数: addr={hex(address)}")
        original = b""
        if record_patch:
            original = self.read_bytes(address, len(data))
        old_protect = wintypes.DWORD(0)
        if not kernel32.VirtualProtectEx(
            handle,
            ctypes.c_void_p(address),
            len(data),
            PAGE_EXECUTE_READWRITE,
            ctypes.byref(old_protect),
        ):
            err = ctypes.get_last_error()
            raise MemoryErrorWin32(f"修改代码页保护 {hex(address)} 失败 (err={err})")
        try:
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            bytes_written = ctypes.c_size_t(0)
            ok = kernel32.WriteProcessMemory(
                handle, ctypes.c_void_p(address), buf, len(data), ctypes.byref(bytes_written)
            )
            if not ok or bytes_written.value != len(data):
                err = ctypes.get_last_error()
                raise MemoryErrorWin32(f"写入代码内存 {hex(address)} 失败 (err={err})")
        finally:
            tmp_protect = wintypes.DWORD(0)
            kernel32.VirtualProtectEx(
                handle,
                ctypes.c_void_p(address),
                len(data),
                old_protect.value,
                ctypes.byref(tmp_protect),
            )
            kernel32.FlushInstructionCache(handle, ctypes.c_void_p(address), len(data))
        if record_patch and original:
            self.patch_history.append(
                MemoryPatch(
                    address=address,
                    original_bytes=original,
                    new_bytes=bytes(data),
                    label=label or hex(address),
                    is_code=True,
                )
            )

    def read_value(self, address: int, dtype: str) -> int | float:
        fmt, size = TYPE_FORMATS[dtype]
        raw = self.read_bytes(address, size)
        return struct.unpack(fmt, raw)[0]

    def write_value(
        self, address: int, dtype: str, value: int | float, record_patch: bool = True, label: str = ""
    ) -> None:
        fmt, _ = TYPE_FORMATS[dtype]
        data = struct.pack(fmt, value)
        self.write_bytes(address, data, record_patch=record_patch, label=label)

    def read_text(self, address: int, max_bytes: int = 32) -> str:
        """尝试以 UTF-8 / GBK / Shift-JIS 解码以 \\x00 结尾的字符串。"""
        raw = self.read_bytes(address, max_bytes)
        raw = raw.split(b"\x00", 1)[0]
        if not raw:
            return ""
        for enc in ("utf-8", "gbk", "big5", "cp932"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace")

    def resolve_pointer(self, base_offset: int, offsets: Sequence[int], ptr_size: int = 8) -> int:
        """从主模块基址 + base_offset 出发，逐级解引用指针链。"""
        addr = self.base_address + base_offset
        ptr_fmt = "u64" if ptr_size == 8 else "u32"
        for idx, off in enumerate(offsets):
            addr = int(self.read_value(addr, ptr_fmt))
            if addr == 0:
                raise MemoryErrorWin32(f"指针链在第 {idx} 级为空 (0x0)")
            addr += off
        return addr

    def undo_patch(self, index: int) -> None:
        if index < 0 or index >= len(self.patch_history):
            return
        patch = self.patch_history.pop(index)
        if patch.is_code:
            self.write_code_bytes(patch.address, patch.original_bytes, record_patch=False)
        else:
            self.write_bytes(patch.address, patch.original_bytes, record_patch=False)

    def undo_all(self) -> int:
        count = 0
        while self.patch_history:
            patch = self.patch_history.pop()
            if patch.is_code:
                self.write_code_bytes(patch.address, patch.original_bytes, record_patch=False)
            else:
                self.write_bytes(patch.address, patch.original_bytes, record_patch=False)
            count += 1
        return count

    def iter_writable_regions(
        self, *, start: int | None = None, stop: int | None = None
    ) -> Iterable[Tuple[int, int]]:
        """遍历进程中所有已提交、可读写且非保护页的内存区域。"""
        handle = self._ensure_attached()
        sys_info = SYSTEM_INFO()
        kernel32.GetSystemInfo(ctypes.byref(sys_info))
        addr = sys_info.lpMinimumApplicationAddress or 0x10000
        max_addr = sys_info.lpMaximumApplicationAddress or 0x7FFFFFFFFFFF
        if start is not None:
            addr = max(addr, start)
        if stop is not None:
            max_addr = min(max_addr, stop)

        mbi = MEMORY_BASIC_INFORMATION()
        mbi_size = ctypes.sizeof(mbi)
        while addr < max_addr:
            ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), mbi_size)
            if ret == 0:
                break
            region_base = mbi.BaseAddress or addr
            region_size = mbi.RegionSize
            if (
                mbi.State == MEM_COMMIT
                and not (mbi.Protect & PAGE_GUARD)
                and not (mbi.Protect & PAGE_NOACCESS)
                and (mbi.Protect & 0xFF) in WRITABLE_PROTECT
                and region_size > 0
            ):
                lower = max(addr, region_base)
                upper = min(max_addr, region_base + region_size)
                if lower < upper:
                    yield (lower, upper - lower)
            next_addr = region_base + region_size
            if next_addr <= addr:
                break
            addr = next_addr

    def scan_pattern(
        self, pattern: bytes, max_results: int = 200, align: int = 1
    ) -> List[int]:
        """在可写内存中搜索精确字节序列。"""
        if not pattern or max_results <= 0:
            return []
        matches: List[int] = []
        deadline = monotonic() + 45
        read_any = False
        for base, size in self.iter_writable_regions():
            tail = b""
            for offset in range(0, size, SCAN_CHUNK_SIZE):
                if monotonic() >= deadline:
                    raise MemoryErrorWin32("扫描超时，未完成搜索。请确认游戏仍在运行后重试。")
                requested = min(SCAN_CHUNK_SIZE, size - offset)
                try:
                    chunk = self.read_bytes(base + offset, requested)
                except MemoryErrorWin32:
                    tail = b""
                    continue
                read_any = True
                data_base = base + offset - len(tail)
                data = tail + chunk
                idx = data.find(pattern)
                while idx != -1:
                    addr = data_base + idx
                    if align <= 1 or addr % align == 0:
                        matches.append(addr)
                        if len(matches) >= max_results:
                            return matches
                    idx = data.find(pattern, idx + 1)
                # A partial read leaves a gap before the next chunk.
                tail = data[-(len(pattern) - 1):] if len(pattern) > 1 and len(chunk) == requested else b""
        if not read_any:
            raise MemoryErrorWin32("未读取到游戏内存。请确认游戏仍在运行并重新连接。")
        return matches

    def scan_value(self, value: int | float, dtype: str = "u32", max_results: int = 500) -> List[int]:
        """首次扫描：在可写内存中搜索指定类型和数值。"""
        fmt, size = TYPE_FORMATS[dtype]
        pattern = struct.pack(fmt, value)
        return self.scan_pattern(pattern, max_results=max_results, align=min(size, 4))

    def filter_value(self, addresses: Sequence[int], value: int | float, dtype: str = "u32") -> List[int]:
        """再次筛选：在已有候选地址中保留当前等于指定数值的地址。"""
        kept: List[int] = []
        for addr in addresses:
            try:
                if self.read_value(addr, dtype) == value:
                    kept.append(addr)
            except MemoryErrorWin32:
                continue
        return kept

    def scan_officer_stats(
        self,
        stats: Tuple[int, int, int, int, int],
        stat_dtype: str = "u8",
        max_results: int = 100,
    ) -> List[int]:
        """按连续五维属性（统率、武力、智力、政治、魅力）搜索武将结构体位置。"""
        fmt, size = TYPE_FORMATS[stat_dtype]
        pattern = b"".join(struct.pack(fmt, s) for s in stats)
        return self.scan_pattern(pattern, max_results=max_results, align=min(size, 4))
