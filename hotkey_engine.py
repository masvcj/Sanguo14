"""Windows 全局热键引擎（基于 user32.RegisterHotKey，支持小键盘与组合键）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
from typing import Callable, Dict, Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

VK_MAP: Dict[str, int] = {
    "NUM0": 0x60,
    "NUM1": 0x61,
    "NUM2": 0x62,
    "NUM3": 0x63,
    "NUM4": 0x64,
    "NUM5": 0x65,
    "NUM6": 0x66,
    "NUM7": 0x67,
    "NUM8": 0x68,
    "NUM9": 0x69,
    "NUM*": 0x6A,
    "NUMMULTIPLY": 0x6A,
    "NUM+": 0x6B,
    "NUMADD": 0x6B,
    "NUM-": 0x6D,
    "NUMSUBTRACT": 0x6D,
    "NUM.": 0x6E,
    "NUMDECIMAL": 0x6E,
    "NUM/": 0x6F,
    "NUMDIVIDE": 0x6F,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
    **{str(d): 0x30 + d for d in range(10)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    "INSERT": 0x2D,
    "INS": 0x2D,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PGUP": 0x21,
    "PAGEDOWN": 0x22,
    "PGDN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "SPACE": 0x20,
    "TAB": 0x09,
    "-": 0xBD,
    "MINUS": 0xBD,
    "=": 0xBB,
    "EQUAL": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "\\": 0xDC,
}

VK_TO_NAME: Dict[int, str] = {
    **{0x60 + d: f"Num {d}" for d in range(10)},
    0x6A: "Num *",
    0x6B: "Num +",
    0x6D: "Num -",
    0x6E: "Num .",
    0x6F: "Num /",
    **{0x70 + i: f"F{i + 1}" for i in range(12)},
    **{0x30 + d: str(d) for d in range(10)},
    **{c: chr(c) for c in range(ord("A"), ord("Z") + 1)},
    0x2D: "Insert",
    0x2E: "Delete",
    0x24: "Home",
    0x23: "End",
    0x21: "PageUp",
    0x22: "PageDown",
    0x26: "Up",
    0x28: "Down",
    0x25: "Left",
    0x27: "Right",
    0x20: "Space",
    0x09: "Tab",
    0xBD: "-",
    0xBB: "=",
    0xDB: "[",
    0xDD: "]",
    0xBA: ";",
    0xDE: "'",
    0xBC: ",",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDC: "\\",
}

_KEYSYM_TO_VK: Dict[str, int] = {
    **{f"KP_{d}": 0x60 + d for d in range(10)},
    "KP_Multiply": 0x6A,
    "KP_Add": 0x6B,
    "KP_Subtract": 0x6D,
    "KP_Decimal": 0x6E,
    "KP_Divide": 0x6F,
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    "Insert": 0x2D,
    "Delete": 0x2E,
    "Home": 0x24,
    "End": 0x23,
    "Prior": 0x21,
    "Next": 0x22,
    "Up": 0x26,
    "Down": 0x28,
    "Left": 0x25,
    "Right": 0x27,
    "space": 0x20,
    "Tab": 0x09,
    "minus": 0xBD,
    "equal": 0xBB,
    "bracketleft": 0xDB,
    "bracketright": 0xDD,
    "semicolon": 0xBA,
    "apostrophe": 0xDE,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "grave": 0xC0,
    "backslash": 0xDC,
}

_MODIFIER_KEYSYMS = frozenset({
    "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
    "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
})
_MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("lPrivate", wintypes.DWORD),
    ]


user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short


def _split_hotkey_tokens(combo: str) -> list[str]:
    text = combo.replace("小键盘", "Num").strip()
    if not text:
        return []
    tokens: list[str] = []
    for raw in text.split("+"):
        token = raw.strip().upper().replace(" ", "")
        if token:
            tokens.append(token)
        elif tokens and tokens[-1] == "NUM":
            tokens[-1] = "NUM+"
    return tokens


def parse_hotkey(combo: str) -> Tuple[int, int]:
    """将 'Ctrl + Num 1' / 'Alt + F5' / 'Ctrl + Q' 等解析为 (modifiers, vk)。"""
    parts = _split_hotkey_tokens(combo)
    mods = MOD_NOREPEAT
    vk = 0
    for p in parts:
        if p in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif p == "ALT":
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        elif p in VK_MAP:
            if vk != 0:
                raise ValueError(f"快捷键包含多个主键: '{combo}'")
            vk = VK_MAP[p]
        else:
            raise ValueError(f"不支持的快捷键按键: {p} (来自 '{combo}')")
    if vk == 0:
        raise ValueError(f"快捷键缺少主键: '{combo}'")
    return mods, vk


def format_hotkey(mods: int, vk: int) -> str:
    """将 (modifiers, vk) 格式化为标准快捷键文本（例如 'Ctrl + Num 1'）。"""
    key_name = VK_TO_NAME.get(vk)
    if key_name is None:
        raise ValueError(f"不支持的虚拟键码: {hex(vk)}")
    parts: list[str] = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    parts.append(key_name)
    return " + ".join(parts)


def normalize_hotkey(combo: str) -> str:
    """规范化快捷键字符串；空字符串表示取消绑定。"""
    if not combo or not combo.strip():
        return ""
    mods, vk = parse_hotkey(combo)
    return format_hotkey(mods, vk)


def format_hotkey_display(combo: str, *, empty: str = "未设置") -> str:
    """用于界面展示的快捷键文本。"""
    if not combo or not combo.strip():
        return empty
    return combo.replace("Num ", "小键盘 ")


def event_to_hotkey(
    keysym: str,
    keycode: int,
    state: int,
    *,
    ctrl: Optional[bool] = None,
    alt: Optional[bool] = None,
    shift: Optional[bool] = None,
) -> Optional[str]:
    """将 Tkinter KeyPress 事件转换为标准快捷键字符串；纯修饰键或不支持的按键返回 None。"""
    if keysym in _MODIFIER_KEYSYMS or keycode in _MODIFIER_VKS:
        return None
    vk = keycode if keycode in VK_TO_NAME else _KEYSYM_TO_VK.get(keysym)
    if vk is None and len(keysym) == 1:
        upper = keysym.upper()
        if upper in VK_MAP:
            vk = VK_MAP[upper]
    if vk is None:
        return None
    mods = MOD_NOREPEAT
    if (bool(state & 0x0004) if ctrl is None else ctrl):
        mods |= MOD_CONTROL
    if (bool(state & 0x20000) if alt is None else alt):
        mods |= MOD_ALT
    if (bool(state & 0x0001) if shift is None else shift):
        mods |= MOD_SHIFT
    return format_hotkey(mods, vk)


class GlobalHotkeyManager:
    """在独立 Win32 消息循环线程中注册并监听全局快捷键。"""

    def __init__(self) -> None:
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._combos: Dict[int, Tuple[int, int, str]] = {}
        self._next_id = 1
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self.enabled = True
        self.failed_hotkeys: Dict[str, int] = {}

    def register(self, combo: str, callback: Callable[[], None]) -> int:
        mods, vk = parse_hotkey(combo)
        hk_id = self._next_id
        self._next_id += 1
        self._combos[hk_id] = (mods, vk, combo)
        self._callbacks[hk_id] = callback
        return hk_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = None

    def _run_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        # Create the message queue before advertising readiness, even if every key is occupied.
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.failed_hotkeys.clear()
        registered_ids = []
        for hk_id, (mods, vk, combo) in self._combos.items():
            if user32.RegisterHotKey(None, hk_id, mods, vk):
                registered_ids.append(hk_id)
            else:
                self.failed_hotkeys[combo] = ctypes.get_last_error()
        self._ready.set()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and self.enabled:
                cb = self._callbacks.get(int(msg.wParam))
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass

        for hk_id in registered_ids:
            user32.UnregisterHotKey(None, hk_id)
