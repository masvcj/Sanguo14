"""《三国志14 威力加强版（传承版）》极简风实时内存修改器 - 桌面 GUI。"""

from __future__ import annotations

import json
import struct
import threading
from dataclasses import replace
import queue
import os
from time import monotonic, sleep
import shutil
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import winsound

from hotkey_engine import (
    GlobalHotkeyManager,
    event_to_hotkey,
    format_hotkey_display,
    normalize_hotkey,
    user32,
)
from memory_engine import (
    MemoryErrorWin32,
    ProcessInfo,
    ProcessMemory,
    find_game_processes,
    list_processes,
)
from profile_manager import FieldDef, TrainerProfile, parse_int
from officer_auto_locator import (
    LIVE_DETAIL_KINDS,
    LIVE_FIELD_SCALES,
    LIVE_GLOBAL_FIELDS,
    LIVE_RECORD_FIELDS,
    OFFICER_EXTRA_FIELDS,
    OFFICER_STAT_CAPS_OFFSET,
    read_live_targets,
    read_selected_officer,
    resolve_live_global_field,
    supported_executable,
)
from officer_probe import (
    HERITAGE_OFFICER_STRIDE, HERITAGE_STATS_OFFSET, OfficerFollowTarget, find_record_references,
    foreground_process_id, match_record_references,
)
from custom_trait_editor import (
    is_custom_aura_self_patch_enabled,
    set_custom_aura_self_patch,
)
from trait_tactic_panel import OfficerSkillController
from world_panel import WorldEditorController

DEFAULT_PROFILE_PATH = Path(__file__).parent / "profiles" / "san14pk_heritage.json"


def default_profile_path() -> Path:
    """Keep packaged configuration beside the EXE, outside its temporary bundle."""
    if not getattr(sys, "frozen", False):
        return DEFAULT_PROFILE_PATH
    path = Path(sys.executable).parent / "profiles" / DEFAULT_PROFILE_PATH.name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_PROFILE_PATH, path)
    return path


class San14TrainerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("三国志14 PK 修改器")
        self.geometry("1240x920")
        self.minsize(1080, 760)

        self.mem = ProcessMemory()
        self.profile_path = default_profile_path()
        self.profile = TrainerProfile.load(self.profile_path)
        self.default_hotkeys: Dict[Tuple[str, str], str] = {
            (tbl_key, f.key): f.hotkey
            for tbl_key, tbl in self.profile.tables.items()
            for f in tbl.fields
        }
        self.hotkey_config_path = self.profile_path.with_name("custom_hotkeys.json")
        self._load_custom_hotkeys()
        self.process_list: List[ProcessInfo] = []
        self.scan_results: List[int] = []
        self.scan_running = False
        self.officer_probe = None
        self.officer_follow: Optional[OfficerFollowTarget] = None
        self.officer_auto_enabled = False
        self.officer_auto_poll_at = 0.0
        self.officer_auto_snapshot: Optional[tuple[int, bytes]] = None
        self.location_snapshots: dict[str, tuple[int, bytes]] = {}
        self.location_status = {kind: tk.StringVar(value="自动识别 · 等待详情") for kind in LIVE_RECORD_FIELDS}
        self.officer_snapshot: Optional[tuple[OfficerFollowTarget, tuple[int, bytes]]] = None
        self.custom_aura_self_var = tk.BooleanVar(value=False)
        self.follow_cache_path = self.profile_path.with_name("officer_follow_session.json")
        self.follow_status = tk.StringVar(value="跟随已校准 · 请打开武将详情")
        self.selected_record_addrs: Dict[str, int] = {}

        self.sound_enabled = tk.BooleanVar(value=True)
        self.hotkeys_enabled = tk.BooleanVar(value=True)
        self.topmost_enabled = tk.BooleanVar(value=False)
        self.active_toggles: Dict[Tuple[str, str], bool] = {}
        self.trainer_inputs: Dict[Tuple[str, str], tk.StringVar] = {}
        self.trainer_buttons: Dict[Tuple[str, str], ttk.Button] = {}
        self.hotkey_buttons: Dict[Tuple[str, str], ttk.Button] = {}
        self.cat_buttons: Dict[str, ttk.Button] = {}
        self.current_category = "officer"

        self.hotkey_mgr = GlobalHotkeyManager()
        self._hotkey_events = queue.SimpleQueue()

        self._configure_style()
        self._build_header()
        self._build_body()
        self._register_all_hotkeys()
        self.after(50, self._drain_hotkey_events)
        self._auto_find_game(silent=True)
        self._schedule_freeze_loop()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        self.colors = dict(bg="#202223", card="#202223", line="#414548",
                           fg="#eeeeee", muted="#a2a5a8", accent="#ff9800")
        c = self.colors
        self.configure(bg=c["bg"])
        self.option_add("*TCombobox*Listbox.background", c["card"])
        self.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", "#65421d")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["fg"],
                        font=("Microsoft YaHei UI", 10), borderwidth=0,
                        lightcolor=c["line"], darkcolor=c["line"], bordercolor=c["line"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["card"])
        for name, bg, fg, size, weight in [
            ("TLabel", c["bg"], c["fg"], 10, "normal"),
            ("Card.TLabel", c["card"], c["fg"], 10, "normal"),
            ("Title.TLabel", c["bg"], c["accent"], 16, "bold"),
            ("Section.TLabel", c["card"], c["fg"], 14, "bold"),
            ("Muted.TLabel", c["bg"], c["muted"], 9, "normal"),
            ("CardMuted.TLabel", c["card"], c["muted"], 9, "normal"),
            ("Group.TLabel", c["card"], c["accent"], 9, "bold"),
            ("Badge.TLabel", "#3b3021", c["accent"], 9, "normal"),
            ("Connected.TLabel", c["bg"], c["accent"], 9, "normal"),
        ]:
            style.configure(name, background=bg, foreground=fg,
                            font=("Microsoft YaHei UI", size, weight))
        style.configure("Badge.TLabel", padding=(10, 4))
        style.configure("Hotkey.TLabel", background="#202223", foreground="#e3e3e3",
                        font=("Consolas", 9), padding=(8, 5))
        for name, bg, fg, hover in [
            ("TButton", "#33383b", c["fg"], "#454b4f"),
            ("Accent.TButton", c["accent"], "#211b11", "#ffb34a"),
            ("TabActive.TButton", "#594021", c["accent"], "#715027"),
            ("TabIdle.TButton", c["bg"], c["muted"], "#303335"),
            ("On.TButton", "#8c5109", "#ffb34a", "#a56313"),
            ("Off.TButton", "#33383b", c["muted"], "#454b4f"),
            ("Quiet.TButton", c["bg"], c["muted"], "#33383b"),
            ("Hotkey.TButton", "#26292b", "#e3e3e3", "#3b3021"),
        ]:
            style.configure(name, background=bg, foreground=fg, borderwidth=0,
                            padding=(14, 9), anchor=tk.CENTER, focusthickness=0)
            style.map(name, background=[("disabled", "#202733"), ("active", hover)],
                      foreground=[("disabled", "#59687d")])
        style.configure("TabIdle.TButton", anchor=tk.W, padding=(16, 12))
        style.configure("TabActive.TButton", anchor=tk.W, padding=(16, 12))
        style.configure("Hotkey.TButton", anchor=tk.W, padding=(6, 4),
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Hotkey.TButton", foreground=[("active", c["accent"])])
        style.configure("TEntry", fieldbackground="#272a2c", foreground=c["fg"],
                        insertcolor=c["fg"], bordercolor=c["line"], lightcolor=c["line"],
                        darkcolor=c["line"], padding=(9, 7))
        style.map("TEntry", bordercolor=[("focus", c["accent"])],
                  lightcolor=[("focus", c["accent"])], darkcolor=[("focus", c["accent"])])
        style.configure("TCombobox", fieldbackground="#272a2c", background="#33383b",
                        foreground=c["fg"], arrowcolor=c["muted"], padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", "#272a2c")],
                  foreground=[("readonly", c["fg"])], selectbackground=[("readonly", "#272a2c")],
                  selectforeground=[("readonly", c["fg"])])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["muted"],
                        indicatorbackground="#33383b", indicatorforeground=c["accent"], padding=3)
        style.map("TCheckbutton", background=[("active", c["bg"])],
                  foreground=[("active", c["fg"])])
        style.configure("TRadiobutton", background=c["card"], foreground=c["fg"],
                        indicatorbackground="#33383b", indicatorforeground=c["accent"], padding=3)
        style.map("TRadiobutton", background=[("active", c["card"])],
                  foreground=[("selected", c["accent"]), ("active", c["fg"])])
        style.configure("TNotebook", background=c["bg"], borderwidth=0,
                        lightcolor=c["bg"], darkcolor=c["bg"], bordercolor=c["bg"],
                        tabmargins=(0, 0, 0, 12))
        style.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"],
                        lightcolor=c["bg"], darkcolor=c["bg"], bordercolor=c["bg"],
                        padding=(20, 10), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#3e3323"), ("active", "#303335")],
                  foreground=[("selected", c["accent"])])
        style.configure("TSeparator", background=c["line"])
        style.configure("Vertical.TScrollbar", background="#454b4f", troughcolor=c["card"],
                        lightcolor=c["card"], darkcolor=c["card"], bordercolor=c["card"],
                        arrowcolor=c["muted"], borderwidth=0, arrowsize=12)
        style.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                        foreground=c["fg"], rowheight=34, borderwidth=0)
        style.configure("Treeview.Heading", background="#2a2d2f", foreground=c["muted"],
                        font=("Microsoft YaHei UI", 9), padding=(10, 10), relief="flat")
        style.map("Treeview", background=[("selected", "#594021")],
                  foreground=[("selected", c["accent"])])

    def _build_header(self) -> None:
        hdr = ttk.Frame(self, padding=(20, 14, 20, 12))
        hdr.pack(fill=tk.X)
        left = ttk.Frame(hdr)
        left.pack(side=tk.LEFT)
        brand = ttk.Frame(left)
        brand.pack(anchor=tk.W)
        ttk.Label(brand, text="《三国志14：威力加强传承版》", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(brand, text="实时修改器", style="Badge.TLabel").pack(side=tk.LEFT, padx=14)
        self.conn_var = tk.StringVar(value="● 未连接游戏 · 功能地址尚未定位")
        self.conn_label = ttk.Label(left, textvariable=self.conn_var, style="Muted.TLabel")
        self.conn_label.pack(anchor=tk.W, pady=(7, 0))
        right = ttk.Frame(hdr)
        right.pack(side=tk.RIGHT)
        self.attach_btn = ttk.Button(right, text="连接游戏", style="Accent.TButton",
                                     command=self._quick_attach_or_pick)
        self.attach_btn.pack(anchor=tk.E)
        settings = ttk.Frame(right)
        settings.pack(anchor=tk.E, pady=(8, 0))
        ttk.Checkbutton(settings, text="快捷键", variable=self.hotkeys_enabled,
                        command=self._toggle_hotkeys).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(settings, text="提示音", variable=self.sound_enabled).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(settings, text="窗口置顶", variable=self.topmost_enabled,
                        command=self._toggle_topmost).pack(side=tk.LEFT)
        self.hotkey_status_var = tk.StringVar(value="快捷键检测中")
        ttk.Label(right, textvariable=self.hotkey_status_var, style="Muted.TLabel").pack(anchor=tk.E, pady=(4, 0))

    def _build_body(self) -> None:
        # Footer is packed first so that it remains visible when the window shrinks.
        self._build_footer()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))
        self.main_tab = ttk.Frame(self.notebook)
        self.calib_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.main_tab, text="基础修改")
        self.tactics_tab = ttk.Frame(self.notebook)
        self.traits_tab = ttk.Frame(self.notebook)
        self.custom_trait_tab = ttk.Frame(self.notebook)
        self.officer_deep_tab = ttk.Frame(self.notebook)
        self.world_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.tactics_tab, text="战法 / 战技")
        self.notebook.add(self.traits_tab, text="个性")
        self.notebook.add(self.custom_trait_tab, text="自创个性（无限制）")
        self.notebook.add(self.officer_deep_tab, text="武将进阶")
        self.notebook.add(self.world_tab, text="势力 / 外交 / 名品")
        self.notebook.add(self.calib_tab, text="地址扫描")
        self._build_main_panel()
        self._build_calib_panel()
        self.skill_controller = OfficerSkillController(self)
        self.skill_controller.build_tactics_tab(self.tactics_tab)
        self.skill_controller.build_traits_tab(self.traits_tab)
        self.skill_controller.build_custom_traits_tab(self.custom_trait_tab)
        self.world_controller = WorldEditorController(self)
        self.world_controller.build_officer_deep_tab(self.officer_deep_tab)
        self.world_controller.build_world_tab(self.world_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _on_notebook_tab_changed(self, _event: tk.Event) -> None:
        try:
            cur = self.notebook.select()
            if cur in (str(self.traits_tab), str(self.tactics_tab)):
                self.skill_controller.refresh_page_data(silent=True, reload_officer=False)
        except Exception:
            pass

    def _build_main_panel(self) -> None:
        self.category_names = {"officer": "武将", "city": "都市", "global": "全局",
                               "area": "地区", "gate": "关隘", "unit": "部队"}
        style = ttk.Style(self)
        style.configure("Compact.TButton", padding=(6, 5), font=("Microsoft YaHei UI", 10))
        style.configure("Mini.TButton", padding=(6, 2), font=("Microsoft YaHei UI", 8))
        style.configure("On.TButton", padding=(3, 0))
        style.configure("Off.TButton", padding=(3, 0))
        style.configure("Hotkey.TLabel", padding=0, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Group.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        side = ttk.Frame(self.main_tab, width=176, padding=(0, 12, 18, 0))
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        emblem = tk.Canvas(side, height=172, bg="#282b2d", highlightthickness=0)
        emblem.pack(fill=tk.X, pady=(0, 18))
        emblem.create_text(78, 45, text="三国志", fill="#eeeeee", font=("Microsoft YaHei UI", 23, "bold"))
        emblem.create_text(78, 102, text="14", fill="#ff9800", font=("Georgia", 52, "bold"))
        emblem.create_text(78, 151, text="威力加强 · 传承版", fill="#b1b1b1", font=("Microsoft YaHei UI", 9))
        ttk.Label(side, text="当前功能", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 8))
        self.feature_count_var = tk.StringVar()
        ttk.Label(side, textvariable=self.feature_count_var, style="Muted.TLabel").pack(anchor=tk.W)
        ttk.Label(side, text="点击左侧快捷键可修改\n点击「写入」应用数值\n开关用于持续锁定\n输入后可按回车应用", style="Muted.TLabel",
                  justify=tk.LEFT).pack(anchor=tk.W, pady=14)
        ttk.Button(side, text="地址定位 / 扫描", command=lambda: self.notebook.select(self.calib_tab)).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="快捷键诊断", command=self._show_hotkey_status).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="恢复默认快捷键", command=self._reset_all_hotkeys).pack(fill=tk.X, pady=4)
        ttk.Button(side, text="关闭全部开关", command=self._disable_all_toggles).pack(fill=tk.X, pady=4)
        ttk.Separator(side).pack(fill=tk.X, pady=14)
        ttk.Label(side, text="战法 / 个性 / 进阶", style="Muted.TLabel").pack(anchor=tk.W, pady=(0, 6))
        ttk.Button(side, text="战法 / 战技编辑", command=lambda: self.notebook.select(self.tactics_tab)).pack(fill=tk.X, pady=3)
        ttk.Button(side, text="个性编辑", command=lambda: self.notebook.select(self.traits_tab)).pack(fill=tk.X, pady=3)
        ttk.Button(side, text="自创个性（无限制）", command=lambda: self.notebook.select(self.custom_trait_tab)).pack(fill=tk.X, pady=3)
        ttk.Button(side, text="武将进阶 / 阵型", command=lambda: self.notebook.select(self.officer_deep_tab)).pack(fill=tk.X, pady=3)
        ttk.Button(side, text="势力 / 外交 / 名品", command=lambda: self.notebook.select(self.world_tab)).pack(fill=tk.X, pady=3)
        self.rows_card = ttk.Frame(self.main_tab, padding=(8, 8, 0, 0))
        self.rows_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._switch_category("officer")

    def _switch_category(self, key: str) -> None:
        # All categories stay visible together, including after a profile reload or bind.
        self.current_category = key
        for w in self.rows_card.winfo_children():
            w.destroy()
        self.trainer_buttons.clear()
        self.hotkey_buttons.clear()
        total = sum(len(t.fields) for t in self.profile.tables.values())
        self.feature_count_var.set(f"{total} 项基础修改")
        heading = ttk.Frame(self.rows_card)
        heading.pack(fill=tk.X)
        ttk.Label(heading, text="快捷键（点击修改）/ 操作 / 功能", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Label(heading, text="橙色数值可编辑 · 写入后生效", style="Muted.TLabel").pack(side=tk.RIGHT)
        ttk.Separator(self.rows_card).pack(fill=tk.X, pady=(6, 8))
        viewport = ttk.Frame(self.rows_card)
        viewport.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(viewport, bg=self.colors["bg"], highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(viewport, orient=tk.VERTICAL, command=canvas.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        content = ttk.Frame(canvas)
        item = canvas.create_window(0, 0, window=content, anchor=tk.NW)
        content.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        self.options_canvas = canvas
        content.columnconfigure((0, 1), weight=1, uniform="options")
        for col, keys in enumerate((("global", "city", "area", "gate"), ("officer", "unit"))):
            column = ttk.Frame(content, padding=(0 if col == 0 else 16, 0, 12, 6))
            column.grid(row=0, column=col, sticky="nsew")
            for table_key in keys:
                table = self.profile.tables.get(table_key)
                if table is None:
                    continue
                title = ttk.Frame(column)
                title.pack(fill=tk.X, pady=(6, 2))
                ttk.Label(title, text=f"{self.category_names[table_key]}编辑", style="Group.TLabel").pack(side=tk.LEFT)
                addr = self.selected_record_addrs.get(table_key) or table.base_address
                if table_key == "officer" and (self.officer_follow or self.officer_auto_enabled):
                    ttk.Label(title, textvariable=self.follow_status, style="Muted.TLabel").pack(side=tk.RIGHT)
                elif (self.officer_auto_enabled and table_key in self.location_status
                      and table_key not in self.selected_record_addrs):
                    ttk.Label(title, textvariable=self.location_status[table_key], style="Muted.TLabel").pack(side=tk.RIGHT)
                elif table_key == "global" and self.officer_auto_enabled and table_key not in self.selected_record_addrs:
                    ttk.Label(title, text="自动识别 · 命令书/施政", style="Muted.TLabel").pack(side=tk.RIGHT)
                else:
                    ttk.Label(title, text="已定位" if addr > 0 else "未定位", style="Muted.TLabel").pack(side=tk.RIGHT)
                if table_key in ("officer", "city", "area", "gate", "unit"):
                    qbar = ttk.Frame(column)
                    qbar.pack(fill=tk.X, pady=(0, 3))
                    ttk.Button(
                        qbar,
                        text="读取当前值",
                        style="Mini.TButton",
                        command=lambda k=table_key: self._read_group_values(k),
                    ).pack(side=tk.LEFT, padx=(0, 5))
                    ttk.Button(
                        qbar,
                        text="一键全满",
                        style="Mini.TButton",
                        command=lambda k=table_key: self._apply_group_quick_action(k, "max"),
                    ).pack(side=tk.LEFT, padx=(0, 5))
                    if table_key in ("city", "gate", "unit"):
                        ttk.Button(
                            qbar,
                            text="削弱(归1)",
                            style="Mini.TButton",
                            command=lambda k=table_key: self._apply_group_quick_action(k, "weaken"),
                        ).pack(side=tk.LEFT, padx=(0, 5))
                        ttk.Button(
                            qbar,
                            text="一键净化",
                            style="Mini.TButton",
                            command=lambda k=table_key: self._apply_group_quick_action(k, "purify"),
                        ).pack(side=tk.LEFT)
                for f in table.fields:
                    self._render_option_row(column, table_key, f)
                if table_key == "global":
                    aura_row = ttk.Frame(column, padding=(0, 4, 0, 2))
                    aura_row.pack(fill=tk.X)
                    ttk.Checkbutton(
                        aura_row,
                        text="自创友军光环单槽双吃（仅 256~355：1槽同时生效自身+友军）",
                        variable=self.custom_aura_self_var,
                        command=self._toggle_custom_aura_self_patch,
                    ).pack(side=tk.LEFT)
        if not getattr(self, "_wheel_bound", False):
            self.bind_all("<MouseWheel>", self._scroll_options, add="+")
            self._wheel_bound = True

    def _scroll_options(self, event: tk.Event) -> None:
        canvas = self.options_canvas
        if not canvas.winfo_exists() or self.notebook.select() != str(self.main_tab):
            return
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget and str(widget).startswith(str(self.rows_card)):
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def _render_option_row(self, parent: ttk.Widget, tbl_key: str, f: FieldDef) -> None:
        row = ttk.Frame(parent, padding=(0, 3))
        row.pack(fill=tk.X)
        row.columnconfigure(2, weight=1)
        pair = (tbl_key, f.key)
        shortcut = format_hotkey_display(f.hotkey, empty="点击设置")
        hk_btn = ttk.Button(
            row,
            text=shortcut,
            style="Hotkey.TButton",
            width=15,
            command=lambda t=tbl_key, fd=f: self._open_hotkey_editor(t, fd),
        )
        hk_btn.grid(row=0, column=0, padx=(0, 6), sticky=tk.W)
        self.hotkey_buttons[pair] = hk_btn
        active = self.active_toggles.get(pair, False)
        btn = ttk.Button(row, text=("开" if active else "关") if f.mode == "toggle" else "写入",
                         width=4 if f.mode == "toggle" else 5, style=("On.TButton" if active else "Off.TButton") if f.mode == "toggle" else "Compact.TButton",
                         command=lambda t=tbl_key, fd=f: self._trigger_field_action(t, fd))
        btn.grid(row=0, column=1, padx=(0, 6))
        ttk.Label(row, text=f.label, style="Card.TLabel").grid(row=0, column=2, sticky=tk.W)
        if f.mode != "toggle":
            if pair not in self.trainer_inputs:
                self.trainer_inputs[pair] = tk.StringVar(value=str(f.default_value))
            underline = tk.Frame(row, bg=self.colors["accent"])
            underline.grid(row=0, column=3, padx=(6, 0), sticky=tk.E)
            entry = tk.Entry(underline, textvariable=self.trainer_inputs[pair], width=12,
                             justify=tk.CENTER, relief=tk.FLAT, bd=0, highlightthickness=0,
                             bg="#2c3033", fg=self.colors["accent"],
                             insertbackground=self.colors["accent"], selectbackground="#65421d",
                             selectforeground="#ffffff", font=("Consolas", 12))
            entry.pack(pady=(0, 2), ipady=6)
            entry.bind("<Return>", lambda e, t=tbl_key, fd=f: self._trigger_field_action(t, fd))
        self.trainer_buttons[pair] = btn

    def _build_calib_panel(self) -> None:
        scan_box = ttk.Frame(self.calib_tab, style="Card.TFrame", padding=20)
        scan_box.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(scan_box, text="定位游戏数据", style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(scan_box, text="先连接游戏，再输入当前数值进行扫描。选中结果后，在右侧绑定分类。",
                  style="CardMuted.TLabel").pack(anchor=tk.W, pady=(6, 18))
        r1 = ttk.Frame(scan_box, style="Card.TFrame")
        r1.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(r1, text="武将五维", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        self.scan_stat_vars = [tk.StringVar(value=v) for v in ("96", "72", "91", "94", "96")]
        for label, var in zip(("统", "武", "智", "政", "魅"), self.scan_stat_vars):
            ttk.Label(r1, text=label, style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(4, 3))
            ttk.Entry(r1, textvariable=var, width=3, justify=tk.CENTER).pack(side=tk.LEFT)
        self.scan_stat_dtype = tk.StringVar(value="u8")
        ttk.Combobox(r1, textvariable=self.scan_stat_dtype, values=["u8", "u16", "u32"],
                     width=4, state="readonly").pack(side=tk.LEFT, padx=10)
        ttk.Button(r1, text="扫描五维", style="Accent.TButton", command=self._run_stat_scan).pack(side=tk.LEFT)
        ttk.Button(r1, text="跟随测试 · 采样", command=self._run_officer_probe).pack(side=tk.LEFT, padx=8)
        ttk.Button(r1, text="重置采样", command=self._reset_officer_probe).pack(side=tk.LEFT)
        r2 = ttk.Frame(scan_box, style="Card.TFrame")
        r2.pack(fill=tk.X)
        ttk.Label(r2, text="通用数值", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        self.scan_val_var = tk.StringVar(value="10000")
        ttk.Entry(r2, textvariable=self.scan_val_var, width=12).pack(side=tk.LEFT, padx=(4, 10))
        self.scan_val_dtype = tk.StringVar(value="u32")
        ttk.Combobox(r2, textvariable=self.scan_val_dtype, values=["u8", "u16", "u32", "i32"],
                     width=4, state="readonly").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(r2, text="首次扫描", command=self._run_first_value_scan).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(r2, text="再次筛选", command=self._run_next_value_scan).pack(side=tk.LEFT)
        self.scan_count_var = tk.StringVar(value="结果: 0")
        ttk.Label(r2, textvariable=self.scan_count_var, style="CardMuted.TLabel").pack(side=tk.RIGHT)
        mid = ttk.Frame(self.calib_tab)
        mid.pack(fill=tk.BOTH, expand=True)
        results = ttk.Frame(mid)
        results.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 14))
        self.scan_tree = ttk.Treeview(results, columns=("addr", "preview"), show="headings", selectmode="browse")
        self.scan_tree.heading("addr", text="候选地址")
        self.scan_tree.heading("preview", text="内存预览 · HEX")
        self.scan_tree.column("addr", width=170, anchor=tk.CENTER)
        self.scan_tree.column("preview", width=240, anchor=tk.W)
        scroll = ttk.Scrollbar(results, orient=tk.VERTICAL, command=self.scan_tree.yview)
        self.scan_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.scan_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        side = ttk.Frame(mid, style="Card.TFrame", padding=16)
        side.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Label(side, text="绑定到分类", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 10))
        bindings = ttk.Frame(side, style="Card.TFrame")
        bindings.pack(fill=tk.X)
        for index, key in enumerate(("officer", "city", "global", "area", "gate", "unit")):
            ttk.Button(bindings, text=self.category_names[key], width=5,
                       command=lambda k=key: self._bind_selected_addr(k)).grid(
                           row=index // 2, column=index % 2, padx=3, pady=4, sticky="ew")
        ttk.Separator(side).pack(fill=tk.X, pady=10)
        config = ttk.Frame(side, style="Card.TFrame")
        config.pack(fill=tk.X)
        ttk.Button(config, text="保存", width=5, command=self._save_profile).grid(row=0, column=0, padx=3)
        ttk.Button(config, text="加载", width=5, command=self._load_profile).grid(row=0, column=1, padx=3)

    def _build_footer(self) -> None:
        ftr = ttk.Frame(self, padding=(26, 0, 26, 16))
        ftr.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(ftr).pack(fill=tk.X, pady=(0, 10))
        self.status_var = tk.StringVar(value="就绪 · 连接游戏后开始修改")
        ttk.Label(ftr, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=620).pack(side=tk.LEFT)
        ttk.Button(ftr, text="恢复全部修改", style="Quiet.TButton",
                   command=self._undo_all_patches).pack(side=tk.RIGHT)

    # ---------------- 进程连接 ----------------

    def _auto_find_game(self, silent: bool = False) -> bool:
        procs = find_game_processes(self.profile.process_names)
        if procs:
            proc = procs[0]
            try:
                self.mem.attach(proc.pid, proc.name)
                self.officer_probe = None
                self._restore_officer_follow()
                self.conn_var.set(f"● 已连接 {proc.name} (PID {proc.pid})")
                self.conn_label.configure(style="Connected.TLabel")
                self.attach_btn.configure(text="重新连接")
                return True
            except MemoryErrorWin32 as exc:
                if not silent:
                    messagebox.showerror("连接失败", str(exc))
        return False

    def _quick_attach_or_pick(self) -> None:
        if self.scan_running:
            self.status_var.set("请等当前扫描结束后再重新连接")
            return
        if self._auto_find_game(silent=False):
            self._play_beep(True)
            return
        # 未自动找到游戏时弹出简洁进程选择框
        procs = find_game_processes(self.profile.process_names)
        if not procs:
            self.status_var.set("未找到游戏进程：请启动传承版并进入正在游玩的剧本")
            messagebox.showwarning("未找到游戏", "未找到传承版游戏进程。请先启动游戏并进入剧本。", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("选择游戏进程")
        win.geometry("380x420")
        win.configure(bg=self.colors["bg"])
        win.transient(self)
        win.grab_set()

        lb = tk.Listbox(win, bg=self.colors["card"], fg=self.colors["fg"], selectbackground="#594021", selectforeground=self.colors["accent"], font=("Microsoft YaHei UI", 10), highlightthickness=0, relief=tk.FLAT)
        lb.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        for p in procs:
            lb.insert(tk.END, f"{p.name} (PID: {p.pid})")

        def confirm() -> None:
            sel = lb.curselection()
            if not sel:
                return
            p = procs[sel[0]]
            try:
                self.mem.attach(p.pid, p.name)
                self.officer_probe = None
                self._restore_officer_follow()
                self.conn_var.set(f"● 已连接 {p.name} (PID {p.pid})")
                self.conn_label.configure(style="Connected.TLabel")
                self.attach_btn.configure(text="重新连接")
                self._play_beep(True)
                win.destroy()
            except MemoryErrorWin32 as exc:
                messagebox.showerror("连接失败", str(exc), parent=win)

        ttk.Button(win, text="附加选中进程", style="Accent.TButton", command=confirm).pack(pady=(0, 12))

    # ---------------- 热键与修改逻辑 ----------------

    def _restore_officer_follow(self) -> None:
        self.officer_follow = OfficerFollowTarget.load(self.mem, self.follow_cache_path)
        self.officer_auto_enabled = supported_executable(self.mem)
        self._sync_custom_aura_patch_state()
        if hasattr(self, "skill_controller"):
            self.skill_controller.on_process_attached()
        if hasattr(self, "world_controller"):
            self.world_controller.on_process_attached()

    def _sync_custom_aura_patch_state(self) -> None:
        self.custom_aura_self_var.set(is_custom_aura_self_patch_enabled(self.mem))

    def _toggle_custom_aura_self_patch(self, parent: Optional[tk.Widget] = None) -> None:
        want = bool(self.custom_aura_self_var.get())
        if not self.mem.is_attached:
            self.custom_aura_self_var.set(False)
            self.status_var.set("请先连接游戏进程 SAN14CE_SC.exe")
            messagebox.showwarning("提示", "请先连接游戏进程 SAN14CE_SC.exe 后再开启该开关。", parent=parent or self)
            return
        try:
            active = set_custom_aura_self_patch(self.mem, want)
            self.custom_aura_self_var.set(active)
            if active:
                self.status_var.set("已开启：自创友军光环单槽双吃（仅 256~355，自身+友军同步生效，不影响原版神将/奸雄）")
            else:
                self.status_var.set("已关闭：自创友军光环单槽双吃（已还原游戏原始代码）")
            self._play_beep(active)
        except MemoryErrorWin32 as exc:
            self._sync_custom_aura_patch_state()
            self.status_var.set(f"自创光环单槽双吃切换失败：{exc}")
            messagebox.showerror("操作失败", str(exc), parent=parent or self)
        self.officer_auto_poll_at = 0.0
        self.officer_auto_snapshot = None
        self.location_snapshots.clear()
        self.officer_snapshot = None
        self.follow_status.set("跟随已校准 · 请打开武将详情" if self.officer_follow else
                               "自动识别 · 请打开武将详情")
        self._switch_category(self.current_category)

    def _remember_officer(self) -> None:
        if self.officer_auto_enabled:
            self._remember_live_targets()
            if "officer" not in self.selected_record_addrs:
                return
        follow = self.officer_follow
        if follow is None:
            self.officer_snapshot = None
            return
        foreground = foreground_process_id()
        if foreground == follow.pid:
            try:
                snapshot = follow.read_current(self.mem, follow.pid)
            except MemoryErrorWin32:
                self.officer_snapshot = None
                self.follow_status.set("武将目标待确认")
            else:
                self.officer_snapshot = (follow, snapshot)
                self.follow_status.set("跟随五维 · " + "/".join(map(str, snapshot[1])))
        elif foreground != os.getpid():
            self.officer_snapshot = None
            self.follow_status.set("跟随已校准 · 请返回游戏确认武将")

    def _remember_live_targets(self) -> None:
        now = monotonic()
        if now < self.officer_auto_poll_at:
            return
        self.officer_auto_poll_at = now + 0.4
        targets = {}
        if foreground_process_id() in (self.mem.pid, os.getpid()):
            try:
                targets = read_live_targets(self.mem, LIVE_DETAIL_KINDS)
            except MemoryErrorWin32:
                pass
        self.officer_auto_snapshot = targets.get("officer")
        if self.officer_auto_snapshot:
            self.follow_status.set("当前五维 · " + "/".join(map(str, self.officer_auto_snapshot[1])))
        else:
            self.follow_status.set("自动识别 · 等待武将详情")
        self.location_snapshots = {
            kind: value for kind, value in targets.items() if kind in LIVE_RECORD_FIELDS and len(targets) == 1
        }
        for kind, status in self.location_status.items():
            selection = self.location_snapshots.get(kind)
            if selection is None:
                status.set("自动识别 · 等待详情")
            else:
                status.set(self._format_location_status(kind, selection[1]))

    @staticmethod
    def _format_location_status(kind: str, data: bytes) -> str:
        if kind == "city":
            if len(data) >= 25:
                gold, food, troops, wounded, morale, _, durability, _, disorder, _, provoked = struct.unpack(
                    "<IIIIBBHBBBB", data[:24]
                )
                scale = data[24]
                summary = f"耐{durability}/士{morale}/兵{troops} · 资{gold}/粮{food}/规{scale}"
                flags = [
                    f"{name}{(ticks + 3) // 4}天"
                    for name, ticks in (("混乱", disorder), ("挑衅", provoked))
                    if ticks
                ]
                if flags:
                    summary += " · " + " ".join(flags)
                return summary
            gold = int.from_bytes(data[:4], "little")
            food = int.from_bytes(data[4:8], "little")
            return f"已识别 · 资金 {gold} / 兵粮 {food}"
        if kind == "gate":
            troops, wounded, morale, _, durability, _, disorder, _, provoked = struct.unpack("<IIBBHBBBB", data[:16])
            summary = f"耐久 {durability} / 士气 {morale} / 士兵 {troops}"
            if wounded:
                summary += f" / 伤兵 {wounded}"
            flags = [
                f"{name}{(ticks + 3) // 4}天"
                for name, ticks in (("混乱", disorder), ("挑衅", provoked))
                if ticks
            ]
            if flags:
                summary += " · " + " ".join(flags)
            return summary
        if kind == "unit":
            troops = int.from_bytes(data[:2], "little")
            wounded = int.from_bytes(data[2:4], "little")
            summary = f"士气 {data[4]} / 士兵 {troops} / 伤兵 {wounded}"
            if len(data) >= 16:
                disorder, restrained, provoked = data[5], data[6], data[7]
                gold = int.from_bytes(data[8:12], "little")
                food = int.from_bytes(data[12:16], "little")
                if gold or food:
                    summary += f" / 资 {gold} / 粮 {food}"
                flags = [
                    f"{name}{(ticks + 3) // 4}天"
                    for name, ticks in (("混乱", disorder), ("止步", restrained), ("挑衅", provoked))
                    if ticks
                ]
                if flags:
                    summary += " · " + " ".join(flags)
            return summary
        pop = int.from_bytes(data[:2], "little")
        order = data[2]
        if len(data) >= 10:
            commerce, agri, barracks, build_cap = struct.unpack("<HHHB", data[3:10])
            return f"商{commerce}/农{agri}/兵{barracks} · 人{pop}/治{order}/建{build_cap}"
        return f"已识别 · 人口 {pop} / 治安 {order}"

    def _load_custom_hotkeys(self) -> None:
        if not self.hotkey_config_path.is_file():
            return
        try:
            raw = json.loads(self.hotkey_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for tbl_key, table in self.profile.tables.items():
            for f in table.fields:
                entry_key = f"{tbl_key}.{f.key}"
                if entry_key not in raw:
                    continue
                val = raw[entry_key]
                if not isinstance(val, str):
                    continue
                try:
                    f.hotkey = normalize_hotkey(val)
                except ValueError:
                    continue

    def _save_custom_hotkeys(self) -> None:
        overrides: Dict[str, str] = {}
        for tbl_key, table in self.profile.tables.items():
            for f in table.fields:
                pair = (tbl_key, f.key)
                default_val = self.default_hotkeys.get(pair, "")
                if f.hotkey != default_val:
                    overrides[f"{tbl_key}.{f.key}"] = f.hotkey
        if not overrides:
            try:
                self.hotkey_config_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.hotkey_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.hotkey_config_path.write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _find_hotkey_conflict(
        self, normalized: str, exclude_pair: Tuple[str, str]
    ) -> Optional[Tuple[str, FieldDef]]:
        if not normalized:
            return None
        for tbl_key, table in self.profile.tables.items():
            for f in table.fields:
                if (tbl_key, f.key) == exclude_pair or not f.hotkey:
                    continue
                try:
                    other = normalize_hotkey(f.hotkey)
                except ValueError:
                    other = f.hotkey
                if other == normalized:
                    return tbl_key, f
        return None

    def _refresh_hotkey_button(self, tbl_key: str, f: FieldDef) -> None:
        btn = self.hotkey_buttons.get((tbl_key, f.key))
        if btn is not None and btn.winfo_exists():
            btn.configure(text=format_hotkey_display(f.hotkey, empty="点击设置"))

    def _set_field_hotkey(
        self,
        tbl_key: str,
        f: FieldDef,
        new_hotkey: str,
        *,
        clear_conflict: bool = True,
        save: bool = True,
    ) -> Optional[Tuple[str, FieldDef]]:
        normalized = normalize_hotkey(new_hotkey)
        conflict = self._find_hotkey_conflict(normalized, (tbl_key, f.key))
        cleared: Optional[Tuple[str, FieldDef]] = None
        if conflict is not None:
            if not clear_conflict:
                other_tbl, other_field = conflict
                cat_name = self.category_names.get(other_tbl, other_tbl)
                raise ValueError(f"快捷键 {format_hotkey_display(normalized)} 已被「{cat_name} · {other_field.label}」占用")
            other_tbl, other_field = conflict
            other_field.hotkey = ""
            self._refresh_hotkey_button(other_tbl, other_field)
            cleared = conflict
        f.hotkey = normalized
        self._refresh_hotkey_button(tbl_key, f)
        self._register_all_hotkeys()
        if save:
            try:
                self._save_custom_hotkeys()
            except OSError as exc:
                self.status_var.set(f"快捷键已生效，但保存配置失败：{exc}")
                return cleared
        if normalized:
            failed_code = self.hotkey_mgr.failed_hotkeys.get(normalized)
            if failed_code is not None:
                self.status_var.set(
                    f"快捷键「{f.label}」已设为 {format_hotkey_display(normalized)}，但系统注册失败（错误 {failed_code}），可能与其他软件冲突"
                )
            elif cleared is not None:
                other_tbl, other_field = cleared
                cat_name = self.category_names.get(other_tbl, other_tbl)
                self.status_var.set(
                    f"已将「{f.label}」快捷键设为 {format_hotkey_display(normalized)}（已自动清空「{cat_name} · {other_field.label}」的绑定）"
                )
            else:
                self.status_var.set(f"已将「{f.label}」快捷键设为 {format_hotkey_display(normalized)}")
        else:
            self.status_var.set(f"已清除「{f.label}」的快捷键")
        return cleared

    def _reset_all_hotkeys(self, *, confirm: bool = True) -> None:
        if confirm and not messagebox.askyesno(
            "恢复默认快捷键",
            "确定要将全部快捷键恢复为默认方案吗？",
            parent=self,
        ):
            return
        for tbl_key, table in self.profile.tables.items():
            for f in table.fields:
                f.hotkey = self.default_hotkeys.get((tbl_key, f.key), f.hotkey)
                self._refresh_hotkey_button(tbl_key, f)
        self._register_all_hotkeys()
        try:
            self._save_custom_hotkeys()
        except OSError as exc:
            self.status_var.set(f"已恢复默认快捷键，但清理自定义文件失败：{exc}")
            return
        self.status_var.set("已恢复全部默认快捷键")

    def _open_hotkey_editor(self, tbl_key: str, f: FieldDef) -> None:
        prev_enabled = self.hotkey_mgr.enabled
        self.hotkey_mgr.enabled = False

        win = tk.Toplevel(self)
        win.title(f"修改快捷键 - {f.label}")
        win.geometry("460x295")
        win.resizable(False, False)
        win.configure(bg=self.colors["bg"])
        win.transient(self)
        win.grab_set()

        closed = False

        def close_dialog() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            self.hotkey_mgr.enabled = self.hotkeys_enabled.get() if prev_enabled else False
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_dialog)

        body = ttk.Frame(win, padding=20)
        body.pack(fill=tk.BOTH, expand=True)

        cat_name = self.category_names.get(tbl_key, tbl_key)
        ttk.Label(body, text=f"{cat_name} · {f.label}", style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(
            body,
            text="直接按下新的组合键（支持字母、数字、小键盘、F1-F12 及 Ctrl/Alt/Shift），也可手动输入。",
            style="Muted.TLabel",
            wraplength=410,
        ).pack(anchor=tk.W, pady=(6, 12))

        pending_var = tk.StringVar(value=f.hotkey)
        display_var = tk.StringVar(value=format_hotkey_display(f.hotkey, empty="未设置（按任意键录制）"))
        conflict_var = tk.StringVar(value="")

        capture = tk.Label(
            body,
            textvariable=display_var,
            bg="#2b2f33",
            fg=self.colors["accent"],
            font=("Microsoft YaHei UI", 14, "bold"),
            pady=12,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=self.colors["accent"],
            highlightcolor=self.colors["accent"],
            takefocus=1,
            cursor="hand2",
        )
        capture.pack(fill=tk.X)

        manual_row = ttk.Frame(body)
        manual_row.pack(fill=tk.X, pady=(10, 4))
        ttk.Label(manual_row, text="手动输入：", style="Muted.TLabel").pack(side=tk.LEFT)
        manual_entry = ttk.Entry(manual_row, textvariable=pending_var)
        manual_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        conflict_lbl = ttk.Label(body, textvariable=conflict_var, style="Connected.TLabel", wraplength=415)
        conflict_lbl.pack(anchor=tk.W, pady=(6, 0))

        def update_preview(*_: object) -> None:
            raw = pending_var.get().strip()
            if not raw:
                display_var.set("未设置（将清除快捷键）")
                conflict_var.set("")
                return
            try:
                norm = normalize_hotkey(raw)
            except ValueError as exc:
                display_var.set("格式无效")
                conflict_var.set(str(exc))
                return
            display_var.set(format_hotkey_display(norm))
            hit = self._find_hotkey_conflict(norm, (tbl_key, f.key))
            if hit is not None:
                other_tbl, other_field = hit
                other_cat = self.category_names.get(other_tbl, other_tbl)
                conflict_var.set(f"提示：与「{other_cat} · {other_field.label}」冲突，点击确定将自动清空原项绑定。")
            else:
                conflict_var.set("")

        pending_var.trace_add("write", update_preview)
        update_preview()

        def on_capture_key(event: tk.Event) -> str:
            if event.keysym == "Escape":
                close_dialog()
                return "break"
            ctrl = bool(event.state & 0x0004) or bool(user32.GetKeyState(0x11) & 0x8000)
            alt = bool(event.state & 0x20000) or bool(user32.GetKeyState(0x12) & 0x8000)
            shift = bool(event.state & 0x0001) or bool(user32.GetKeyState(0x10) & 0x8000)
            combo = event_to_hotkey(event.keysym, event.keycode, event.state, ctrl=ctrl, alt=alt, shift=shift)
            if combo is not None:
                pending_var.set(combo)
            return "break"

        capture.bind("<KeyPress>", on_capture_key)
        capture.bind("<Button-1>", lambda _e: capture.focus_set())
        win.bind("<Escape>", lambda _e: close_dialog())

        def apply_change() -> None:
            raw = pending_var.get().strip()
            try:
                normalize_hotkey(raw)
            except ValueError as exc:
                messagebox.showerror("快捷键无效", str(exc), parent=win)
                return
            close_dialog()
            self._set_field_hotkey(tbl_key, f, raw, clear_conflict=True, save=True)
            self._play_beep(True)

        def clear_key() -> None:
            pending_var.set("")
            capture.focus_set()

        def restore_default() -> None:
            pending_var.set(self.default_hotkeys.get((tbl_key, f.key), ""))
            capture.focus_set()

        manual_entry.bind("<Return>", lambda _e: apply_change())

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X, side=tk.BOTTOM, pady=(12, 0))
        ttk.Button(btns, text="清除", command=clear_key).pack(side=tk.LEFT)
        ttk.Button(btns, text="恢复默认", command=restore_default).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="取消", command=close_dialog).pack(side=tk.RIGHT)
        ttk.Button(btns, text="确定", style="Accent.TButton", command=apply_change).pack(side=tk.RIGHT, padx=(0, 8))

        win.update_idletasks()
        x = self.winfo_rootx() + max(20, (self.winfo_width() - win.winfo_width()) // 2)
        y = self.winfo_rooty() + max(20, (self.winfo_height() - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        capture.focus_set()

    def _register_all_hotkeys(self) -> None:
        self.hotkey_mgr.stop()
        self.hotkey_mgr = GlobalHotkeyManager()
        self.hotkey_mgr.enabled = self.hotkeys_enabled.get()
        for tbl_key, table in self.profile.tables.items():
            for f in table.fields:
                if f.hotkey:
                    try:
                        self.hotkey_mgr.register(
                            f.hotkey,
                            lambda t=tbl_key, fd=f: self._hotkey_events.put((t, fd)),
                        )
                    except ValueError as exc:
                        self.status_var.set(str(exc))
        self.hotkey_mgr.start()
        total = sum(bool(f.hotkey) for t in self.profile.tables.values() for f in t.fields)
        failed = len(self.hotkey_mgr.failed_hotkeys)
        self.hotkey_status_var.set(f"快捷键 {total - failed}/{total} · 点击左侧可改键")
        if failed:
            self.status_var.set(f"{failed} 个快捷键注册失败，请打开「快捷键诊断」，并关闭其他修改器实例")

    def _drain_hotkey_events(self) -> None:
        for _ in range(100):
            try:
                table_key, field = self._hotkey_events.get_nowait()
            except queue.Empty:
                break
            if self.hotkeys_enabled.get():
                self._trigger_field_action(table_key, field, from_hotkey=True)
        self._remember_officer()
        self.after(50, self._drain_hotkey_events)

    def _show_hotkey_status(self) -> None:
        failures = self.hotkey_mgr.failed_hotkeys
        details = "\n".join(f"{key}: Windows 错误 {code}" for key, code in failures.items())
        messagebox.showinfo("快捷键诊断", self.hotkey_status_var.get() +
                            "\n点击任意功能左侧的快捷键按钮即可自定义组合键，修改后自动保存。\n"
                            "Num / 小键盘表示右侧数字小键盘，使用小键盘数字时请开启 Num Lock。\n"
                            "快捷键与点击写入使用同一功能，仍需先连接游戏并定位地址。\n\n" +
                            (details or "所有快捷键注册成功。"), parent=self)

    def _toggle_hotkeys(self) -> None:
        self.hotkey_mgr.enabled = self.hotkeys_enabled.get()

    def _toggle_topmost(self) -> None:
        topmost = bool(self.topmost_enabled.get())
        self.attributes("-topmost", topmost)
        self.status_var.set("窗口已置顶" if topmost else "已取消窗口置顶")

    def _play_beep(self, enabled: bool) -> None:
        if not self.sound_enabled.get():
            return
        try:
            freq = 880 if enabled else 520
            threading.Thread(target=lambda: winsound.Beep(freq, 75), daemon=True).start()
        except Exception:
            pass

    def _trigger_field_action(self, tbl_key: str, f: FieldDef, *, from_hotkey: bool = False) -> None:
        table = self.profile.tables.get(tbl_key)
        if table is None:
            return
        pair = (tbl_key, f.key)

        if not self.mem.is_attached:
            self.status_var.set(f"收到操作 [{f.hotkey}] {f.label}：尚未连接游戏，请点击右上角「连接游戏」")
            self._play_beep(False)
            return

        target_addr = self.selected_record_addrs.get(tbl_key) or table.base_address
        follow = self.officer_follow if tbl_key == "officer" else None
        automatic = (tbl_key == "officer" and self.officer_auto_enabled and
                     "officer" not in self.selected_record_addrs)
        live_location = (self.officer_auto_enabled and tbl_key in LIVE_RECORD_FIELDS and
                         tbl_key not in self.selected_record_addrs)
        live_global = (tbl_key == "global" and self.officer_auto_enabled and
                       "global" not in self.selected_record_addrs)
        field_scale = 1
        officer_stat_write = False

        if live_global:
            if f.key not in LIVE_GLOBAL_FIELDS:
                self.status_var.set(
                    "《三国志14》的进言与军师指令均消耗命令书，开启「无限命令书」即可无限执行；如需自定义地址可在地址扫描中绑定。"
                )
                self._play_beep(False)
                return
            try:
                resolved = resolve_live_global_field(self.mem, f.key)
            except MemoryErrorWin32 as exc:
                self.status_var.set(str(exc))
                self._play_beep(False)
                return
            if resolved is None:
                self.status_var.set("当前全局项暂不支持自动定位。")
                self._play_beep(False)
                return
            g_addr, g_dtype, g_val, g_min, g_max = resolved
            target_addr = g_addr
            f = replace(f, offset=0, dtype=g_dtype, default_value=g_val, min_val=g_min, max_val=g_max)
            table = replace(table, fields=[f])

        if automatic or follow is not None:
            stats = ("leadership", "war", "intelligence", "politics", "charm")
            is_stat = f.key in stats and f.offset == stats.index(f.key) and f.dtype == "u8" and f.mode == "edit"
            is_extra_officer = automatic and (f.key in OFFICER_EXTRA_FIELDS or f.key == "max_cap")
            if not is_stat and not is_extra_officer:
                self.status_var.set("手动校准跟随目前仅支持统率、武力、智力、政治、魅力。")
                return
            try:
                if automatic:
                    required = self.mem.pid if from_hotkey else os.getpid()
                    if foreground_process_id() != required:
                        raise MemoryErrorWin32("请先打开游戏中的武将详情。")
                    current = read_selected_officer(self.mem, reject_hover=from_hotkey)
                    if current != self.officer_auto_snapshot or foreground_process_id() != required:
                        raise MemoryErrorWin32("武将刚刚切换，请核对当前五维后再写入。")
                    target_addr = current[0]
                elif from_hotkey:
                    target_addr = follow.resolve(self.mem)
                else:
                    current = follow.read_current(self.mem, os.getpid())
                    if self.officer_snapshot != (follow, current):
                        raise MemoryErrorWin32("当前武将与上次在游戏中确认的不一致，请重新打开他的详情页后再写入。")
                    target_addr = current[0]
            except MemoryErrorWin32 as exc:
                self.status_var.set(str(exc))
                self._play_beep(False)
                return
            if is_stat:
                officer_stat_write = True
            elif f.key == "max_cap":
                record_addr = target_addr - HERITAGE_STATS_OFFSET
                new_state = not self.active_toggles.get(pair, False)
                cap_val = 120 if new_state else 100
                payload = struct.pack("<5H", cap_val, cap_val, cap_val, cap_val, cap_val)
                try:
                    self.mem.write_bytes(
                        record_addr + OFFICER_STAT_CAPS_OFFSET,
                        payload,
                        record_patch=True,
                        label=f"officer@{hex(record_addr)}.能力上限",
                    )
                    if self.mem.read_bytes(record_addr + OFFICER_STAT_CAPS_OFFSET, 10) != payload:
                        raise MemoryErrorWin32("能力上限写入后读回校验失败。")
                except Exception as exc:
                    self.status_var.set(f"修改失败: {exc}")
                    self._play_beep(False)
                    return
                self.active_toggles[pair] = new_state
                btn = self.trainer_buttons.get(pair)
                if btn is not None and btn.winfo_exists():
                    btn.configure(text="开" if new_state else "关", style="On.TButton" if new_state else "Off.TButton")
                self.status_var.set(
                    f"已将当前武将五维能力上限{'突破至 120' if new_state else '恢复为默认 100'}（地址 {hex(record_addr + OFFICER_STAT_CAPS_OFFSET)}）"
                )
                self._play_beep(new_state)
                return
            else:
                record_addr = target_addr - HERITAGE_STATS_OFFSET
                extra_off, extra_dtype, extra_max = OFFICER_EXTRA_FIELDS[f.key]
                target_addr = record_addr
                f = replace(f, offset=extra_off, dtype=extra_dtype, max_val=extra_max)
                table = replace(table, fields=[f])
        if live_location:
            layout = LIVE_RECORD_FIELDS[tbl_key].get(f.key)
            if layout is None or f.mode != "edit":
                self.status_var.set("这个项目还没有支持自动定位。")
                return
            required = self.mem.pid if from_hotkey else os.getpid()
            try:
                if foreground_process_id() != required:
                    raise MemoryErrorWin32("请在游戏中打开目标的详情。")
                now = read_live_targets(self.mem, LIVE_DETAIL_KINDS)
                current = now.get(tbl_key)
                if (len(now) != 1 or current is None or current != self.location_snapshots.get(tbl_key)
                        or foreground_process_id() != required):
                    raise MemoryErrorWin32("请先在游戏中打开目标的详情，再核对修改器显示的数值。")
                target_addr = current[0]
            except MemoryErrorWin32 as exc:
                self.status_var.set(str(exc))
                self._play_beep(False)
                return
            offset, dtype, limit = layout
            field_scale = LIVE_FIELD_SCALES.get(f.key, 1)
            user_max = limit
            f = replace(f, offset=offset, dtype=dtype, max_val=user_max * field_scale)
            table = replace(table, fields=[f])
        if target_addr <= 0:
            self.status_var.set(f"收到操作 [{f.hotkey}] {f.label}：地址未定位，未执行修改；请在「地址扫描」中校准")
            self._play_beep(False)
            return

        if f.mode == "toggle":
            new_state = not self.active_toggles.get(pair, False)
            if new_state:
                try:
                    table.write_record(self.mem, target_addr, {f.key: f.default_value},
                                       record_label=f"{tbl_key}@{hex(target_addr)}")
                    actual = self.mem.read_value(target_addr + f.offset, f.dtype)
                    expected = max(f.min_val, min(f.max_val, f.default_value))
                    if actual != expected:
                        raise MemoryErrorWin32(f"写入后校验失败：期望 {expected}，读回 {actual}")
                except Exception as exc:
                    self.active_toggles[pair] = False
                    self.status_var.set(f"锁定未开启：{exc}")
                    self._play_beep(False)
                    return
            self.active_toggles[pair] = new_state
            btn = self.trainer_buttons.get(pair)
            if btn is not None and btn.winfo_exists():
                btn.configure(text="开" if new_state else "关", style="On.TButton" if new_state else "Off.TButton")
            self.status_var.set(f"已{'开启锁定' if new_state else '关闭'}: {f.label}")
            self._play_beep(new_state)
        else:
            try:
                var = self.trainer_inputs.get(pair)
                val = parse_int(var.get(), f.default_value) if var is not None else f.default_value
                raw_val = val * field_scale
                expected = max(f.min_val, min(f.max_val, raw_val))
                table.write_record(self.mem, target_addr, {f.key: raw_val}, record_label=f"{tbl_key}@{hex(target_addr)}")
                actual = self.mem.read_value(target_addr + f.offset, f.dtype)
                if actual != expected:
                    raise MemoryErrorWin32(f"写入后校验失败：期望 {expected}，读回 {actual}")
                if officer_stat_write and (automatic or (follow is not None and not from_hotkey)):
                    updated = bytearray(current[1])
                    updated[f.offset] = actual
                    if automatic:
                        self.officer_auto_snapshot = (target_addr, bytes(updated))
                    else:
                        self.officer_snapshot = (follow, (target_addr, bytes(updated)))
                    self.follow_status.set("跟随五维 · " + "/".join(map(str, updated)))
                elif live_location:
                    try:
                        refreshed = read_live_targets(self.mem, LIVE_DETAIL_KINDS)
                        if len(refreshed) == 1 and tbl_key in refreshed:
                            self.location_snapshots[tbl_key] = refreshed[tbl_key]
                            if tbl_key in self.location_status:
                                self.location_status[tbl_key].set(
                                    self._format_location_status(tbl_key, refreshed[tbl_key][1])
                                )
                    except MemoryErrorWin32:
                        pass
                if field_scale > 1:
                    shown_days = actual // field_scale
                    self.status_var.set(
                        f"地址 {hex(target_addr + f.offset)} 已写入 {f.label} {shown_days} 天（内部值 {actual}）；请核对游戏内效果"
                    )
                else:
                    self.status_var.set(f"地址 {hex(target_addr + f.offset)} 已写入并读回 {actual}；请核对游戏内效果")
                self._play_beep(True)
            except Exception as exc:
                self.status_var.set(f"修改失败: {exc}")
                self._play_beep(False)

    def _resolve_live_group_record(self, tbl_key: str) -> int:
        if not self.mem.is_attached:
            raise MemoryErrorWin32("尚未连接游戏，请点击右上角「连接游戏」。")
        if not self.officer_auto_enabled or tbl_key in self.selected_record_addrs:
            raise MemoryErrorWin32("一键读取/快捷操作需要简体中文传承版自动定位模式。")
        if tbl_key == "officer":
            cur = read_selected_officer(self.mem, reject_hover=False)
            self.officer_auto_snapshot = cur
            self.follow_status.set("当前五维 · " + "/".join(map(str, cur[1])))
            return cur[0] - HERITAGE_STATS_OFFSET
        now = read_live_targets(self.mem, LIVE_DETAIL_KINDS)
        cur = now.get(tbl_key)
        if len(now) != 1 or cur is None:
            raise MemoryErrorWin32(f"请先在游戏中打开目标{self.category_names[tbl_key]}的详情页。")
        self.location_snapshots[tbl_key] = cur
        if tbl_key in self.location_status:
            self.location_status[tbl_key].set(self._format_location_status(tbl_key, cur[1]))
        return cur[0]

    def _read_group_values(self, tbl_key: str) -> None:
        """Read current live values of the open target directly into the group's input boxes."""
        try:
            record_addr = self._resolve_live_group_record(tbl_key)
            loaded = 0
            if tbl_key == "officer":
                stats = self.mem.read_bytes(record_addr + HERITAGE_STATS_OFFSET, 5)
                for idx, key in enumerate(("leadership", "war", "intelligence", "politics", "charm")):
                    var = self.trainer_inputs.get(("officer", key))
                    if var is not None:
                        var.set(str(stats[idx]))
                        loaded += 1
                for key, (off, dtype, _) in OFFICER_EXTRA_FIELDS.items():
                    var = self.trainer_inputs.get(("officer", key))
                    if var is not None:
                        val = int(self.mem.read_value(record_addr + off, dtype))
                        var.set(str(val))
                        loaded += 1
            else:
                for key, (off, dtype, _) in LIVE_RECORD_FIELDS[tbl_key].items():
                    var = self.trainer_inputs.get((tbl_key, key))
                    if var is None:
                        continue
                    raw = int(self.mem.read_value(record_addr + off, dtype))
                    scale = LIVE_FIELD_SCALES.get(key, 1)
                    shown = (raw + scale - 1) // scale if scale > 1 else raw
                    var.set(str(shown))
                    loaded += 1
            self.status_var.set(f"已读取当前{self.category_names[tbl_key]}的 {loaded} 项数值到输入框")
            self._play_beep(True)
        except Exception as exc:
            self.status_var.set(f"读取当前值失败：{exc}")
            self._play_beep(False)

    def _apply_group_quick_action(self, tbl_key: str, preset: str) -> None:
        """Execute one-click max / weaken / purify on the currently open live target."""
        presets: Dict[Tuple[str, str], Dict[str, int]] = {
            ("officer", "max"): {
                "leadership": 100, "war": 100, "intelligence": 100, "politics": 100, "charm": 100,
                "loyalty": 100, "policy_lv": 10, "illness": 0,
            },
            ("city", "max"): {
                "city_durability": 60000, "city_morale": 200, "city_gold": 999999,
                "city_food": 999999, "city_troops": 200000, "city_wounded": 0,
                "city_provoked": 0, "city_disorder": 0, "city_scale": 4,
            },
            ("city", "weaken"): {"city_durability": 1, "city_morale": 1, "city_troops": 1},
            ("city", "purify"): {"city_provoked": 0, "city_disorder": 0, "city_wounded": 0},
            ("area", "max"): {
                "area_commerce": 1000, "area_agri": 1000, "area_barracks": 1000,
                "area_order": 100, "area_build_cap": 30,
            },
            ("gate", "max"): {
                "gate_durability": 60000, "gate_morale": 200, "gate_troops": 100000,
                "gate_wounded": 0, "gate_provoked": 0, "gate_disorder": 0,
            },
            ("gate", "weaken"): {"gate_durability": 1, "gate_morale": 1, "gate_troops": 1},
            ("gate", "purify"): {"gate_provoked": 0, "gate_disorder": 0, "gate_wounded": 0},
            ("unit", "max"): {
                "unit_morale": 200, "unit_troops": 50000, "unit_wounded": 0,
                "unit_gold": 100000, "unit_food": 100000,
                "unit_provoked": 0, "unit_disorder": 0, "unit_restrained": 0,
            },
            ("unit", "weaken"): {"unit_morale": 1, "unit_troops": 1},
            ("unit", "purify"): {"unit_provoked": 0, "unit_disorder": 0, "unit_restrained": 0, "unit_wounded": 0},
        }
        action_names = {"max": "一键全满", "weaken": "削弱(归1)", "purify": "一键净化"}
        values = presets.get((tbl_key, preset))
        if not values:
            return
        try:
            record_addr = self._resolve_live_group_record(tbl_key)
            if tbl_key == "officer":
                stats_keys = ("leadership", "war", "intelligence", "politics", "charm")
                for key, val in values.items():
                    if key in stats_keys:
                        off = HERITAGE_STATS_OFFSET + stats_keys.index(key)
                        self.mem.write_value(record_addr + off, "u8", val, record_patch=True, label=f"officer.{preset}.{key}")
                    elif key in OFFICER_EXTRA_FIELDS:
                        off, dtype, _ = OFFICER_EXTRA_FIELDS[key]
                        self.mem.write_value(record_addr + off, dtype, val, record_patch=True, label=f"officer.{preset}.{key}")
                fresh_stats = self.mem.read_bytes(record_addr + HERITAGE_STATS_OFFSET, 5)
                self.officer_auto_snapshot = (record_addr + HERITAGE_STATS_OFFSET, fresh_stats)
                self.follow_status.set("当前五维 · " + "/".join(map(str, fresh_stats)))
            else:
                fields_map = LIVE_RECORD_FIELDS[tbl_key]
                for key, val in values.items():
                    off, dtype, limit = fields_map[key]
                    scale = LIVE_FIELD_SCALES.get(key, 1)
                    raw_val = max(0, min(limit * scale, val * scale))
                    self.mem.write_value(record_addr + off, dtype, raw_val, record_patch=True, label=f"{tbl_key}.{preset}.{key}")
                try:
                    refreshed = read_live_targets(self.mem, LIVE_DETAIL_KINDS)
                    if len(refreshed) == 1 and tbl_key in refreshed:
                        self.location_snapshots[tbl_key] = refreshed[tbl_key]
                        if tbl_key in self.location_status:
                            self.location_status[tbl_key].set(self._format_location_status(tbl_key, refreshed[tbl_key][1]))
                except MemoryErrorWin32:
                    pass
            self.status_var.set(f"已对当前{self.category_names[tbl_key]}执行「{action_names[preset]}」（共写入 {len(values)} 项）")
            self._play_beep(True)
        except Exception as exc:
            self.status_var.set(f"{action_names[preset]}失败：{exc}")
            self._play_beep(False)

    def _disable_all_toggles(self) -> None:
        for pair in list(self.active_toggles.keys()):
            self.active_toggles[pair] = False
        self._switch_category(self.current_category)
        self.status_var.set("已关闭全部锁定")

    def _schedule_freeze_loop(self) -> None:
        if self.mem.is_attached:
            for (tbl_key, f_key), active in list(self.active_toggles.items()):
                if not active:
                    continue
                table = self.profile.tables.get(tbl_key)
                if table is None:
                    continue
                field_def = next((fd for fd in table.fields if fd.key == f_key), None)
                if field_def is None:
                    continue
                if tbl_key == "global" and self.officer_auto_enabled and tbl_key not in self.selected_record_addrs:
                    try:
                        resolved = resolve_live_global_field(self.mem, f_key)
                        if resolved is not None:
                            g_addr, g_dtype, g_val, _, _ = resolved
                            self.mem.write_value(g_addr, g_dtype, g_val, record_patch=False)
                    except MemoryErrorWin32 as exc:
                        self.active_toggles[(tbl_key, f_key)] = False
                        btn = self.trainer_buttons.get((tbl_key, f_key))
                        if btn is not None and btn.winfo_exists():
                            btn.configure(text="关", style="Off.TButton")
                        self.status_var.set(f"锁定已停止：{field_def.label} 写入失败，{exc}")
                    continue
                if tbl_key == "officer" and f_key == "max_cap" and self.officer_auto_enabled and tbl_key not in self.selected_record_addrs:
                    continue
                target_addr = self.selected_record_addrs.get(tbl_key) or table.base_address
                if target_addr <= 0:
                    continue
                try:
                    self.mem.write_value(
                        target_addr + field_def.offset,
                        field_def.dtype,
                        field_def.default_value,
                        record_patch=False,
                    )
                except MemoryErrorWin32 as exc:
                    self.active_toggles[(tbl_key, f_key)] = False
                    btn = self.trainer_buttons.get((tbl_key, f_key))
                    if btn is not None and btn.winfo_exists():
                        btn.configure(text="关", style="Off.TButton")
                    self.status_var.set(f"锁定已停止：{field_def.label} 写入失败，{exc}")
        self.after(250, self._schedule_freeze_loop)

    # ---------------- 扫描与配置 ----------------

    def _run_stat_scan(self) -> None:
        if not self.mem.is_attached:
            messagebox.showwarning("提示", "请先连接游戏进程。")
            return
        try:
            stats = tuple(int(v.get().strip()) for v in self.scan_stat_vars)
            dtype = self.scan_stat_dtype.get()
        except ValueError:
            messagebox.showerror("错误", "五维必须为整数。")
            return
        self._start_scan(lambda: self.mem.scan_officer_stats(stats, stat_dtype=dtype, max_results=150))  # type: ignore[arg-type]

    def _run_first_value_scan(self) -> None:
        if not self.mem.is_attached:
            messagebox.showwarning("提示", "请先连接游戏进程。")
            return
        try:
            val = parse_int(self.scan_val_var.get())
            dtype = self.scan_val_dtype.get()
        except ValueError:
            messagebox.showerror("错误", "请输入有效整数。")
            return
        self._start_scan(lambda: self.mem.scan_value(val, dtype=dtype, max_results=500))

    def _reset_officer_probe(self) -> None:
        if not self.scan_running:
            self.officer_probe = None
            self.officer_follow = None
            self.officer_snapshot = None
            try:
                self.follow_cache_path.unlink(missing_ok=True)
            except OSError as exc:
                self.status_var.set(f"无法清除校准缓存：{exc}")
                return
            self._switch_category(self.current_category)
            self.status_var.set("已重置。打开第一名武将，输入五维后点击「跟随测试 · 采样」。")

    def _run_officer_probe(self) -> None:
        if self.scan_running or not self.mem.is_attached:
            return
        try:
            stats = tuple(int(v.get().strip()) for v in self.scan_stat_vars)
            if not all(0 <= v <= 255 for v in stats):
                raise ValueError
        except ValueError:
            self._show_scan_failure("请输入五项 0 至 255 的整数。")
            return
        pid = self.mem.pid
        previous = self.officer_probe
        if previous is not None and previous[0] != pid:
            self._reset_officer_probe()
            previous = None
        if previous is not None and previous[3] == 2 and stats != previous[1]:
            self.status_var.set("第三次采样请切回第一名武将，并输入第一组五维。")
            return
        if previous is not None and previous[3] == 1 and stats == previous[1]:
            self.status_var.set("第二次采样请在游戏内换一名武将，并输入新的五维。")
            return

        slots: dict[int, int] = {}
        stats_address = 0
        self.status_var.set("请在 25 秒内切回游戏，停在该武将详情页直至采样完成。")

        def sample() -> List[int]:
            nonlocal stats_address
            deadline = monotonic() + 25
            focused_since = None
            while monotonic() < deadline:
                if foreground_process_id() == pid:
                    now = monotonic()
                    if focused_since is None:
                        focused_since = now
                    elif now - focused_since >= 0.5:
                        break
                else:
                    focused_since = None
                sleep(0.05)
            else:
                raise MemoryErrorWin32("等候游戏切到前台超时，请重新采样。")
            hits = self.mem.scan_officer_stats(stats, stat_dtype="u8", max_results=2)  # type: ignore[arg-type]
            if len(hits) != 1:
                raise MemoryErrorWin32("五维未能唯一定位，请核对五维或改用另一名武将。")
            stats_address = hits[0]
            if previous is not None:
                distance = stats_address - previous[4]
                if (previous[3] == 1 and (distance == 0 or distance % HERITAGE_OFFICER_STRIDE)) or (
                    previous[3] == 2 and distance != 0
                ):
                    raise MemoryErrorWin32("当前五维地址不符合传承版武将记录的间距，请核对目标。")
            found = (find_record_references(self.mem, stats_address) if previous is None else
                     match_record_references(self.mem, previous[2], stats_address))
            if foreground_process_id() != pid:
                raise MemoryErrorWin32("采样结束时游戏不在前台，请重新采样。")
            slots.update(found)
            return list(found)

        def finish(hits: List[int]) -> None:
            stage = 1 if previous is None else previous[3] + 1
            verified = tuple((slot, offset) for slot, offset in slots.items()
                             if offset == HERITAGE_STATS_OFFSET and slot % 8 == 0)
            if not hits or stage == 3:
                self.officer_probe = None
            else:
                self.officer_probe = (pid, stats if previous is None else previous[1], slots, stage,
                                      stats_address if previous is None else previous[4])
            self.scan_count_var.set(f"引用候选: {len(hits)}")
            for slot in hits[:200]:
                self.scan_tree.insert("", tk.END, values=(hex(slot), f"五维偏移 {slots[slot]:+#x}"))
            if not hits:
                self.status_var.set(f"第 {stage} 次采样没有吻合的直接引用。")
            elif stage == 1:
                self.status_var.set("第 1 次采样完成。切到第二名武将，输入其五维并再次采样。")
            elif stage == 2:
                self.status_var.set("第 2 次采样完成。切回第一名武将，输入原五维并再次采样。")
            elif len(verified) >= 2 and pid is not None and self.mem.process_name.lower() == "san14ce_sc.exe":
                self.officer_follow = OfficerFollowTarget(pid, verified, stats_address)
                self.officer_snapshot = (self.officer_follow, (stats_address, bytes(stats)))
                self.follow_status.set("跟随五维 · " + "/".join(map(str, stats)))
                for pair in self.active_toggles:
                    if pair[0] == "officer":
                        self.active_toggles[pair] = False
                self._switch_category(self.current_category)
                try:
                    self.officer_follow.save(self.mem, self.follow_cache_path)
                except (MemoryErrorWin32, OSError) as exc:
                    self.status_var.set(f"五维跟随已启用，校准缓存保存失败：{exc}")
                else:
                    self.status_var.set("五维跟随已启用。打开武将详情后可点击「写入」或在游戏中按快捷键。")
                self.notebook.select(self.main_tab)
            else:
                self.status_var.set(f"A→B→A 通过：{len(hits)} 个候选，未达到启用自动跟随的条件。")

        self._start_scan(sample, finish)

    def _start_scan(self, scan: Callable[[], List[int]], on_result: Optional[Callable[[List[int]], None]] = None) -> None:
        if self.scan_running:
            return
        self.scan_running = True
        self.scan_results = []
        self.scan_tree.delete(*self.scan_tree.get_children())
        result: queue.SimpleQueue[List[int] | Exception] = queue.SimpleQueue()
        started = monotonic()

        def worker() -> None:
            try:
                result.put(scan())
            except Exception as exc:
                result.put(exc)

        def poll() -> None:
            try:
                outcome = result.get_nowait()
            except queue.Empty:
                self.scan_count_var.set(f"扫描中 · {int(monotonic() - started)} 秒")
                self.after(200, poll)
                return
            self.scan_running = False
            if isinstance(outcome, Exception):
                self._show_scan_failure(str(outcome))
            else:
                (on_result or self._populate_scan_results)(outcome)

        threading.Thread(target=worker, daemon=True).start()
        poll()

    def _run_next_value_scan(self) -> None:
        if self.scan_running:
            return
        if not self.mem.is_attached or not self.scan_results:
            messagebox.showwarning("提示", "请先进行首次扫描。")
            return
        try:
            val = parse_int(self.scan_val_var.get())
            dtype = self.scan_val_dtype.get()
        except ValueError:
            messagebox.showerror("错误", "请输入有效整数。")
            return
        hits = self.mem.filter_value(self.scan_results, val, dtype=dtype)
        self._populate_scan_results(hits)

    def _show_scan_failure(self, error: str) -> None:
        self.scan_count_var.set("扫描失败")
        messagebox.showerror("扫描失败", error, parent=self)

    def _populate_scan_results(self, hits: List[int]) -> None:
        self.scan_results = hits
        self.scan_count_var.set(f"命中: {len(hits)}")
        self.scan_tree.delete(*self.scan_tree.get_children())
        for addr in hits[:200]:
            try:
                preview = self.mem.read_bytes(addr, 8).hex(" ").upper()
            except MemoryErrorWin32:
                preview = "??"
            self.scan_tree.insert("", tk.END, iid=str(addr), values=(hex(addr), preview))

    def _bind_selected_addr(self, table_key: str) -> None:
        sel = self.scan_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选中一个命中地址。")
            return
        if not sel[0].isdecimal() or int(sel[0]) not in self.scan_results:
            messagebox.showwarning("提示", "跟随测试结果只能用于检查，不能绑定为写入目标。", parent=self)
            return
        addr = int(sel[0])
        if table_key == "officer":
            self.officer_follow = None
            self.officer_probe = None
        self.profile.tables[table_key].base_address = addr
        self.selected_record_addrs[table_key] = addr
        self.status_var.set(f"已绑定 [{table_key}] 基址 -> {hex(addr)}")
        self._switch_category(table_key)
        self.notebook.select(self.main_tab)

    def _load_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="选择偏移配置文件",
            initialdir=str(self.profile_path.parent),
            filetypes=[("JSON 配置文件", "*.json")],
        )
        if not path:
            return
        try:
            profile = TrainerProfile.load(Path(path))
            self.active_toggles.clear()
            self.selected_record_addrs.clear()
            self.officer_follow = None
            self.officer_probe = None
            self.trainer_inputs.clear()
            self.profile = profile
            self.profile_path = Path(path)
            self.default_hotkeys = {
                (tbl_key, f.key): f.hotkey
                for tbl_key, tbl in self.profile.tables.items()
                for f in tbl.fields
            }
            self.hotkey_config_path = self.profile_path.with_name("custom_hotkeys.json")
            self._register_all_hotkeys()
            self._switch_category(self.current_category)
            self.status_var.set(f"已加载配置: {self.profile.name}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def _save_profile(self) -> None:
        try:
            self.profile.save(self.profile_path)
            self.status_var.set("配置已保存")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _undo_all_patches(self) -> None:
        if not self.mem.patch_history:
            self.status_var.set("暂无可撤销的修改")
            return
        self._disable_all_toggles()
        try:
            cnt = self.mem.undo_all()
            self._sync_custom_aura_patch_state()
            self.status_var.set(f"已恢复 {cnt} 条修改")
        except MemoryErrorWin32 as exc:
            messagebox.showerror("恢复失败", str(exc))

    def _on_close(self) -> None:
        self.hotkey_mgr.stop()
        self.mem.detach()
        self.destroy()
