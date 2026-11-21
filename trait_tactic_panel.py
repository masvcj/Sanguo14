"""Unified Trait (个性) and Tactic (战法/战技) editor panels supporting Live Memory & .s14 Save Files."""

from __future__ import annotations

from pathlib import Path
import json
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence

from custom_trait_dialog import CustomTraitWorkshopDialog
from custom_trait_editor import (
    CustomTraitDef,
    discover_edit_personality_file,
    list_live_custom_traits,
    load_custom_traits_file,
    load_edit_personality_file,
    write_live_custom_trait,
)
from game_catalog import (
    DEFAULT_CATALOG_PATH,
    GameCatalog,
    TACTIC_CATEGORY_NAMES,
    TRAIT_COLOR_NAMES,
)
from memory_engine import MemoryErrorWin32
from officer_auto_locator import read_live_targets, supported_executable
from officer_live_editor import (
    LiveOfficerLayout,
    LiveOfficerRecord,
    TACTIC_SLOTS,
    TRAIT_SLOTS,
    calibrate_live_layout_from_record,
    list_live_officers,
    read_live_officer,
    scan_live_officer_layout,
    write_live_tactics,
    write_live_traits,
)
from officer_probe import HERITAGE_STATS_OFFSET
from save_editor import (
    SaveDocument,
    SaveOfficer,
    discover_save_directories,
    list_recent_save_files,
)


def default_catalog_path() -> Path:
    """Resolve san14ce_catalog.json both from source and from a PyInstaller bundle."""
    if not getattr(sys, "frozen", False):
        return DEFAULT_CATALOG_PATH
    beside = Path(sys.executable).parent / "profiles" / DEFAULT_CATALOG_PATH.name
    if beside.is_file():
        return beside
    return DEFAULT_CATALOG_PATH


BUILTIN_SKILL_PRESETS: Dict[str, Dict[str, List[int]]] = {
    "内置 · 野战神将": {
        "traits": [67, 150, 37, 4, 106],          # 飞将、神将、洞察、疾走、神威
        "tactics": [93, 91, 2, 6, 8, 18, 19, 20, 0, 0],  # 天下无双、军神、突击、连弩、铁壁、鼓舞、治疗、镇静
    },
    "内置 · 谋略攻城": {
        "traits": [151, 37, 150, 5, 68],          # 卧龙、洞察、神将、扫荡、水战
        "tactics": [97, 21, 22, 23, 15, 5, 19, 20, 0, 0],  # 神算鬼谋、井阑、冲车、投石、混乱、火矢、治疗、镇静
    },
    "内置 · 内政神辅": {
        "traits": [109, 110, 113, 116, 120],      # 名声、振兴、征税、能吏、眼力
        "tactics": [97, 7, 18, 19, 20, 8, 0, 0, 0, 0],   # 神算鬼谋、激励、鼓舞、治疗、镇静、铁壁
    },
}


class OfficerSkillController:
    """Coordinates live-memory and .s14 save-file editing across the Tactics and Traits tabs."""

    def __init__(self, app: Any, catalog: Optional[GameCatalog] = None) -> None:
        self.app = app
        self.catalog = catalog or GameCatalog.load_default(default_catalog_path())
        self.presets_path = self.app.profile_path.with_name("skill_presets.json")
        self.custom_traits_path = self.app.profile_path.with_name("custom_traits.json")
        self.custom_traits: Dict[int, CustomTraitDef] = load_custom_traits_file(self.custom_traits_path)
        edit_p = discover_edit_personality_file()
        if edit_p is not None and edit_p.is_file():
            try:
                for ct in load_edit_personality_file(edit_p).values():
                    self.catalog.upsert_trait(ct.to_trait_info())
            except Exception:
                pass
        for ct in self.custom_traits.values():
            self.catalog.upsert_trait(ct.to_trait_info())
        self.custom_presets: Dict[str, Dict[str, List[int]]] = {}
        self.preset_var = tk.StringVar(value=next(iter(BUILTIN_SKILL_PRESETS)))
        self._preset_combos: List[ttk.Combobox] = []
        self._load_custom_presets()

        self.mode_var = tk.StringVar(value="live")  # "live" | "save"
        self.target_banner_var = tk.StringVar(value="未读取武将 · 请在剧本中打开武将详情点击「读取当前武将」，或切换到存档模式")
        self.source_info_var = tk.StringVar(value="实时模式：请先连接运行中的游戏并进入剧本")
        self.officer_filter_var = tk.StringVar(value="")
        self.officer_combo_var = tk.StringVar(value="")

        self.live_layout: Optional[LiveOfficerLayout] = None
        self.live_officers: List[LiveOfficerRecord] = []
        self.current_live_officer: Optional[LiveOfficerRecord] = None

        self.save_doc: Optional[SaveDocument] = None
        self.current_save_officer: Optional[SaveOfficer] = None

        self._filtered_officers: List[LiveOfficerRecord | SaveOfficer] = []
        self.draft_traits: List[int] = [0] * TRAIT_SLOTS
        self.draft_tactics: List[int] = [0] * TACTIC_SLOTS
        self._busy = False

        # Widget registries synchronized across both tabs
        self._officer_combos: List[ttk.Combobox] = []
        self._live_only_widgets: List[ttk.Widget] = []
        self._save_only_widgets: List[ttk.Widget] = []
        self._apply_buttons: List[ttk.Button] = []
        self._save_file_buttons: List[ttk.Button] = []

        # Traits tab widgets
        self.trait_slot_tree: Optional[ttk.Treeview] = None
        self.trait_catalog_tree: Optional[ttk.Treeview] = None
        self.trait_search_var = tk.StringVar(value="")
        self.trait_color_var = tk.StringVar(value="全部品级")
        self.trait_catalog_hdr_var = tk.StringVar(value=f"可选个性库（{len(self.catalog.traits)} 项）")
        self._trait_catalog_items: List[int] = []

        # Tactics tab widgets
        self.tactic_slot_tree: Optional[ttk.Treeview] = None
        self.tactic_catalog_tree: Optional[ttk.Treeview] = None
        self.tactic_search_var = tk.StringVar(value="")
        self.tactic_tier_var = tk.StringVar(value="全部级别")
        self.tactic_cat_var = tk.StringVar(value="全部分类")
        self.tactic_catalog_hdr_var = tk.StringVar(value=f"可选战法库（{len(self.catalog.tactics)} 项）")
        self._tactic_catalog_items: List[int] = []

    # ---------------- Shared Toolbar ----------------

    def _build_source_toolbar(self, parent: ttk.Frame, title: str, subtitle: str) -> None:
        header = ttk.Frame(parent, padding=(16, 12, 16, 4))
        header.pack(fill=tk.X)
        ttk.Label(header, text=title, style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.source_info_var, style="Badge.TLabel").pack(side=tk.RIGHT)

        ttk.Label(parent, text=subtitle, style="Muted.TLabel").pack(anchor=tk.W, padx=16, pady=(0, 8))

        bar1 = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        bar1.pack(fill=tk.X, padx=16, pady=(0, 6))

        ttk.Label(bar1, text="修改模式", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            bar1,
            text="剧本实时修改（内存）",
            variable=self.mode_var,
            value="live",
            command=self._on_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            bar1,
            text="存档文件修改（.s14）",
            variable=self.mode_var,
            value="save",
            command=self._on_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 18))

        # Live-mode controls
        btn_read_cur = ttk.Button(
            bar1, text="读取当前详情武将", style="Accent.TButton", command=self.read_current_live_officer
        )
        btn_scan_all = ttk.Button(bar1, text="扫描剧本全部武将", command=self.scan_all_live_officers)
        btn_read_cur.pack(side=tk.LEFT, padx=(0, 8))
        btn_scan_all.pack(side=tk.LEFT, padx=(0, 8))
        self._live_only_widgets.extend([btn_read_cur, btn_scan_all])

        # Save-mode controls
        btn_steam = ttk.Button(
            bar1, text="快速打开 Steam 存档", style="Accent.TButton", command=self.open_steam_save_picker
        )
        btn_browse = ttk.Button(bar1, text="选择 .s14 存档...", command=self.open_save_file_dialog)
        btn_save_as = ttk.Button(bar1, text="另存为...", command=self.save_as_dialog)
        self._save_only_widgets.extend([btn_steam, btn_browse, btn_save_as])

        ttk.Button(
            bar1, text="刷新页面 / 自创库", command=lambda: self.refresh_page_data(silent=False)
        ).pack(side=tk.RIGHT)

        # Second toolbar row: Officer search & selector
        bar2 = ttk.Frame(parent, padding=(16, 4, 16, 6))
        bar2.pack(fill=tk.X)
        ttk.Label(bar2, text="搜索武将").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(bar2, textvariable=self.officer_filter_var, width=14)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))
        search_entry.bind("<KeyRelease>", lambda _e: self._refresh_officer_dropdown())

        ttk.Label(bar2, text="当前武将").pack(side=tk.LEFT, padx=(0, 6))
        combo = ttk.Combobox(bar2, textvariable=self.officer_combo_var, state="readonly", width=36)
        combo.pack(side=tk.LEFT, padx=(0, 12))
        combo.bind("<<ComboboxSelected>>", self._on_officer_combo_selected)
        self._officer_combos.append(combo)

        ttk.Label(bar2, textvariable=self.target_banner_var, style="Muted.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Third compact row: Skill preset templates
        bar3 = ttk.Frame(parent, padding=(16, 0, 16, 6))
        bar3.pack(fill=tk.X)
        ttk.Label(bar3, text="方案预设", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        preset_cb = ttk.Combobox(bar3, textvariable=self.preset_var, state="readonly", width=22)
        preset_cb.pack(side=tk.LEFT, padx=(0, 8))
        self._preset_combos.append(preset_cb)
        ttk.Button(bar3, text="套用到当前武将", command=self.apply_skill_preset).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bar3, text="保存当前为预设", command=self.save_current_as_preset).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bar3, text="删除自定义预设", command=self.delete_selected_preset).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(
            bar3, text="无限制自创个性", style="Accent.TButton", command=self.open_custom_trait_workshop
        ).pack(side=tk.RIGHT)
        self._refresh_preset_combos()

        self._sync_mode_widgets()

    # ---------------- Build Traits Tab (个性) ----------------

    def build_traits_tab(self, parent: ttk.Frame) -> None:
        self._build_source_toolbar(
            parent,
            "武将个性编辑（5 槽位）",
            "支持在剧本游玩时直接写入内存（切换武将页签或重开详情即刷新），也支持直接修改 .s14 存档文件（自动生成 .bak 备份）。",
        )

        # Bottom action bar (packed with side=BOTTOM before split so it is never clipped on smaller screens/DPI)
        actions = ttk.Frame(parent, padding=(16, 6, 16, 12))
        actions.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            actions, text="替换选中槽位（双击右侧也可）", command=lambda: self._assign_selected_trait(add_to_empty=False)
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="添加到首个空槽", command=lambda: self._assign_selected_trait(add_to_empty=True)
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="无限制自创个性...", style="Accent.TButton", command=self.open_custom_trait_workshop
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="同步自创库到剧本", command=self.sync_custom_traits_to_game
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="刷新页面", command=lambda: self.refresh_page_data(silent=False)
        ).pack(side=tk.LEFT, padx=(0, 8))

        btn_save_file = ttk.Button(actions, text="保存存档文件（自动备份）", command=self.save_current_document)
        btn_apply = ttk.Button(actions, text="应用个性修改", style="Accent.TButton", command=self.apply_traits)
        btn_apply.pack(side=tk.RIGHT)
        btn_save_file.pack(side=tk.RIGHT, padx=(0, 8))
        self._apply_buttons.append(btn_apply)
        self._save_file_buttons.append(btn_save_file)

        split = ttk.Frame(parent, padding=(16, 4, 16, 6))
        split.pack(fill=tk.BOTH, expand=True)
        split.columnconfigure(0, weight=5)
        split.columnconfigure(1, weight=7)
        split.rowconfigure(1, weight=1)

        # Left header: owned 5 traits
        left_hdr = ttk.Frame(split)
        left_hdr.grid(row=0, column=0, sticky="ew", padx=(0, 12), pady=(0, 6))
        ttk.Label(left_hdr, text="当前武将个性（共 5 槽位）", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(left_hdr, text="清空选中槽", command=self._clear_selected_trait_slot).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(left_hdr, text="全部清空", command=self._clear_all_trait_slots).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(left_hdr, text="刷新", command=lambda: self.refresh_page_data(silent=False)).pack(side=tk.RIGHT)

        left_box = ttk.Frame(split)
        left_box.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.trait_slot_tree = ttk.Treeview(
            left_box,
            columns=("slot", "id", "name", "color", "desc"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("slot", "槽位", 54, tk.CENTER),
            ("id", "ID", 46, tk.CENTER),
            ("name", "个性", 76, tk.W),
            ("color", "品级", 50, tk.CENTER),
            ("desc", "效果说明", 230, tk.W),
        ):
            self.trait_slot_tree.heading(col, text=text)
            self.trait_slot_tree.column(col, width=width, anchor=anchor)
        self.trait_slot_tree.tag_configure("gold", foreground="#ffb34a")
        self.trait_slot_tree.tag_configure("blue", foreground="#70b8ff")
        self.trait_slot_tree.tag_configure("red", foreground="#ff6b6b")
        self.trait_slot_tree.tag_configure("empty", foreground="#7e8387")
        left_scroll = ttk.Scrollbar(left_box, orient=tk.VERTICAL, command=self.trait_slot_tree.yview)
        self.trait_slot_tree.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.trait_slot_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Right header: full catalog + search/filter
        right_hdr = ttk.Frame(split)
        right_hdr.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(right_hdr, textvariable=self.trait_catalog_hdr_var, style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            right_hdr, text="刷新个性库", command=lambda: self.refresh_page_data(silent=False)
        ).pack(side=tk.LEFT, padx=(8, 0))
        color_cb = ttk.Combobox(
            right_hdr,
            textvariable=self.trait_color_var,
            values=("全部品级", "金色", "蓝色", "红色", "特殊"),
            state="readonly",
            width=9,
        )
        color_cb.pack(side=tk.RIGHT)
        color_cb.bind("<<ComboboxSelected>>", lambda _e: self._populate_trait_catalog())
        t_search = ttk.Entry(right_hdr, textvariable=self.trait_search_var, width=18)
        t_search.pack(side=tk.RIGHT, padx=(8, 8))
        t_search.bind("<KeyRelease>", lambda _e: self._populate_trait_catalog())
        ttk.Label(right_hdr, text="筛选", style="Muted.TLabel").pack(side=tk.RIGHT)

        right_box = ttk.Frame(split)
        right_box.grid(row=1, column=1, sticky="nsew")
        self.trait_catalog_tree = ttk.Treeview(
            right_box,
            columns=("id", "name", "color", "desc"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("id", "ID", 46, tk.CENTER),
            ("name", "个性", 82, tk.W),
            ("color", "品级", 52, tk.CENTER),
            ("desc", "效果说明", 360, tk.W),
        ):
            self.trait_catalog_tree.heading(col, text=text)
            self.trait_catalog_tree.column(col, width=width, anchor=anchor)
        self.trait_catalog_tree.tag_configure("gold", foreground="#ffb34a")
        self.trait_catalog_tree.tag_configure("blue", foreground="#70b8ff")
        self.trait_catalog_tree.tag_configure("red", foreground="#ff6b6b")
        self.trait_catalog_tree.bind("<Double-1>", lambda _e: self._assign_selected_trait(add_to_empty=False))
        right_scroll = ttk.Scrollbar(right_box, orient=tk.VERTICAL, command=self.trait_catalog_tree.yview)
        self.trait_catalog_tree.configure(yscrollcommand=right_scroll.set)
        right_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.trait_catalog_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._populate_trait_catalog()
        self._refresh_trait_slots()
        self._sync_mode_widgets()

    # ---------------- Build Tactics Tab (战法 / 战技) ----------------

    def build_tactics_tab(self, parent: ttk.Frame) -> None:
        self._build_source_toolbar(
            parent,
            "武将战法 / 战技编辑（10 槽位）",
            "槽位 1 通常为固有/主战法，槽位 2~10 为习得战法。提示：DLC 联动战法（ID 170~183）请确认游戏已启用对应 DLC 后再装备。",
        )

        # Bottom action bar (packed with side=BOTTOM before split so it is never clipped on smaller screens/DPI)
        actions = ttk.Frame(parent, padding=(16, 6, 16, 12))
        actions.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            actions, text="替换选中槽位（双击右侧也可）", command=lambda: self._assign_selected_tactic(add_to_empty=False)
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="添加到首个空槽", command=lambda: self._assign_selected_tactic(add_to_empty=True)
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            actions, text="刷新页面", command=lambda: self.refresh_page_data(silent=False)
        ).pack(side=tk.LEFT, padx=(0, 8))

        btn_save_file = ttk.Button(actions, text="保存存档文件（自动备份）", command=self.save_current_document)
        btn_apply = ttk.Button(actions, text="应用战法修改", style="Accent.TButton", command=self.apply_tactics)
        btn_apply.pack(side=tk.RIGHT)
        btn_save_file.pack(side=tk.RIGHT, padx=(0, 8))
        self._apply_buttons.append(btn_apply)
        self._save_file_buttons.append(btn_save_file)

        split = ttk.Frame(parent, padding=(16, 4, 16, 6))
        split.pack(fill=tk.BOTH, expand=True)
        split.columnconfigure(0, weight=5)
        split.columnconfigure(1, weight=7)
        split.rowconfigure(1, weight=1)

        # Left header: owned 10 tactics
        left_hdr = ttk.Frame(split)
        left_hdr.grid(row=0, column=0, sticky="ew", padx=(0, 12), pady=(0, 6))
        ttk.Label(left_hdr, text="当前武将战法（共 10 槽位）", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(left_hdr, text="清空选中槽", command=self._clear_selected_tactic_slot).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(left_hdr, text="全部清空", command=self._clear_all_tactic_slots).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(left_hdr, text="刷新", command=lambda: self.refresh_page_data(silent=False)).pack(side=tk.RIGHT)

        left_box = ttk.Frame(split)
        left_box.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.tactic_slot_tree = ttk.Treeview(
            left_box,
            columns=("slot", "id", "name", "cat", "tier"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("slot", "槽位", 96, tk.CENTER),
            ("id", "ID", 46, tk.CENTER),
            ("name", "战法 / 战技", 110, tk.W),
            ("cat", "分类", 62, tk.CENTER),
            ("tier", "级别", 62, tk.CENTER),
        ):
            self.tactic_slot_tree.heading(col, text=text)
            self.tactic_slot_tree.column(col, width=width, anchor=anchor)
        self.tactic_slot_tree.tag_configure("unique", foreground="#ffb34a")
        self.tactic_slot_tree.tag_configure("normal", foreground="#70b8ff")
        self.tactic_slot_tree.tag_configure("dlc", foreground="#d98aff")
        self.tactic_slot_tree.tag_configure("empty", foreground="#7e8387")
        left_scroll = ttk.Scrollbar(left_box, orient=tk.VERTICAL, command=self.tactic_slot_tree.yview)
        self.tactic_slot_tree.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tactic_slot_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Right header: full tactic catalog + filters
        right_hdr = ttk.Frame(split)
        right_hdr.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(right_hdr, textvariable=self.tactic_catalog_hdr_var, style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            right_hdr, text="刷新战法库", command=lambda: self.refresh_page_data(silent=False)
        ).pack(side=tk.LEFT, padx=(8, 0))

        cat_cb = ttk.Combobox(
            right_hdr,
            textvariable=self.tactic_cat_var,
            values=("全部分类", "战斗", "射击", "强化", "削弱", "计略", "辅助", "攻城", "水战"),
            state="readonly",
            width=8,
        )
        cat_cb.pack(side=tk.RIGHT)
        cat_cb.bind("<<ComboboxSelected>>", lambda _e: self._populate_tactic_catalog())

        tier_cb = ttk.Combobox(
            right_hdr,
            textvariable=self.tactic_tier_var,
            values=("全部级别", "通用", "固有", "DLC"),
            state="readonly",
            width=8,
        )
        tier_cb.pack(side=tk.RIGHT, padx=(6, 6))
        tier_cb.bind("<<ComboboxSelected>>", lambda _e: self._populate_tactic_catalog())

        k_search = ttk.Entry(right_hdr, textvariable=self.tactic_search_var, width=14)
        k_search.pack(side=tk.RIGHT, padx=(6, 6))
        k_search.bind("<KeyRelease>", lambda _e: self._populate_tactic_catalog())
        ttk.Label(right_hdr, text="筛选", style="Muted.TLabel").pack(side=tk.RIGHT)

        right_box = ttk.Frame(split)
        right_box.grid(row=1, column=1, sticky="nsew")
        self.tactic_catalog_tree = ttk.Treeview(
            right_box,
            columns=("id", "name", "cat", "tier", "pinyin"),
            show="headings",
            selectmode="browse",
        )
        for col, text, width, anchor in (
            ("id", "ID", 52, tk.CENTER),
            ("name", "战法 / 战技", 120, tk.W),
            ("cat", "分类", 70, tk.CENTER),
            ("tier", "级别", 70, tk.CENTER),
            ("pinyin", "拼音", 160, tk.W),
        ):
            self.tactic_catalog_tree.heading(col, text=text)
            self.tactic_catalog_tree.column(col, width=width, anchor=anchor)
        self.tactic_catalog_tree.tag_configure("unique", foreground="#ffb34a")
        self.tactic_catalog_tree.tag_configure("normal", foreground="#70b8ff")
        self.tactic_catalog_tree.tag_configure("dlc", foreground="#d98aff")
        self.tactic_catalog_tree.bind("<Double-1>", lambda _e: self._assign_selected_tactic(add_to_empty=False))
        right_scroll = ttk.Scrollbar(right_box, orient=tk.VERTICAL, command=self.tactic_catalog_tree.yview)
        self.tactic_catalog_tree.configure(yscrollcommand=right_scroll.set)
        right_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tactic_catalog_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._populate_tactic_catalog()
        self._refresh_tactic_slots()
        self._sync_mode_widgets()

    # ---------------- Mode Switching & Process Hooks ----------------

    def on_process_attached(self) -> None:
        """Reset cached live layout when attaching to a new game PID."""
        if self.live_layout is not None and self.live_layout.pid != (self.app.mem.pid or 0):
            self.live_layout = None
            self.live_officers = []
            self.current_live_officer = None
        if self.mode_var.get() == "live":
            self._update_source_banner()

    def _on_mode_changed(self) -> None:
        self._sync_mode_widgets()
        self._refresh_officer_dropdown()
        if self.mode_var.get() == "live":
            if self.current_live_officer is not None:
                self._load_officer_into_draft(self.current_live_officer)
            else:
                self._clear_draft()
        else:
            if self.current_save_officer is not None:
                self._load_officer_into_draft(self.current_save_officer)
            else:
                self._clear_draft()
        self._update_source_banner()

    def _sync_mode_widgets(self) -> None:
        is_live = self.mode_var.get() == "live"
        for w in self._live_only_widgets:
            if is_live:
                w.pack(side=tk.LEFT, padx=(0, 8))
            else:
                w.pack_forget()
        for w in self._save_only_widgets:
            if is_live:
                w.pack_forget()
            else:
                w.pack(side=tk.LEFT, padx=(0, 8))
        for btn in self._save_file_buttons:
            if is_live:
                btn.pack_forget()
            else:
                btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _update_source_banner(self) -> None:
        if self.mode_var.get() == "live":
            if not self.app.mem.is_attached:
                self.source_info_var.set("实时模式：未连接游戏")
            elif self.live_layout is not None:
                self.source_info_var.set(
                    f"实时已校准 · 校验 {self.live_layout.verified_matches} 名史实武将 · 共 {len(self.live_officers) or self.live_layout.officer_count} 人"
                )
            else:
                self.source_info_var.set(f"实时已连接 {self.app.mem.process_name} · 待读取武将")
        else:
            if self.save_doc is None:
                self.source_info_var.set("存档模式：尚未加载 .s14 存档")
            else:
                dirty_tag = " · 有未保存改动" if self.save_doc.dirty else " · 已保存"
                m = self.save_doc.metadata
                self.source_info_var.set(f"{self.save_doc.path.name} ({m.scenario} · {m.ruler}){dirty_tag}")

    # ---------------- Live Mode Operations ----------------

    def _run_background(self, task: Callable[[], Any], on_done: Callable[[Any], None], busy_msg: str) -> None:
        if self._busy:
            self.app.status_var.set("当前已有读取/扫描任务正在进行，请稍候...")
            return
        self._busy = True
        self.app.status_var.set(busy_msg)
        q: queue.SimpleQueue[tuple[bool, Any]] = queue.SimpleQueue()

        def worker() -> None:
            try:
                q.put((True, task()))
            except Exception as exc:
                q.put((False, exc))

        def poll() -> None:
            try:
                ok, payload = q.get_nowait()
            except queue.Empty:
                self.app.after(100, poll)
                return
            self._busy = False
            if ok:
                on_done(payload)
            else:
                self.app.status_var.set(f"操作失败：{payload}")
                messagebox.showerror("操作失败", str(payload), parent=self.app)

        threading.Thread(target=worker, daemon=True).start()
        poll()

    def read_current_live_officer(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先点击右上角「连接游戏」，并在游戏中进入剧本。", parent=self.app)
            return

        def work() -> tuple[LiveOfficerLayout, LiveOfficerRecord, List[LiveOfficerRecord]]:
            mem = self.app.mem
            if not supported_executable(mem):
                raise MemoryErrorWin32("自动读取详情武将仅支持简体中文传承版 SAN14CE_SC.exe；请尝试点击「扫描剧本全部武将」。")
            target = read_live_targets(mem, ("officer",)).get("officer")
            if target is None:
                raise MemoryErrorWin32("请先在游戏中打开目标武将的详情页，再点击「读取当前详情武将」。")
            record_addr = target[0] - HERITAGE_STATS_OFFSET
            layout = self.live_layout
            if layout is None or layout.pid != (mem.pid or 0):
                layout = calibrate_live_layout_from_record(mem, record_addr)
            officer = read_live_officer(mem, layout, record_addr)
            officers = self.live_officers if self.live_layout == layout and self.live_officers else list_live_officers(mem, layout)
            return layout, officer, officers

        def done(result: tuple[LiveOfficerLayout, LiveOfficerRecord, List[LiveOfficerRecord]]) -> None:
            layout, officer, officers = result
            self._pull_live_custom_traits_into_catalog()
            self.live_layout = layout
            self.live_officers = officers
            self.current_live_officer = officer
            self._refresh_officer_dropdown(select_record=officer)
            self._load_officer_into_draft(officer)
            self._update_source_banner()
            self.app.status_var.set(f"已读取游戏内当前武将：{officer.full_name} ({hex(officer.address)})")
            self.app._play_beep(True)

        self._run_background(work, done, "正在读取游戏内当前武将并校验个性/战法结构...")

    def scan_all_live_officers(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先点击右上角「连接游戏」，并在游戏中进入剧本。", parent=self.app)
            return

        def work() -> tuple[LiveOfficerLayout, List[LiveOfficerRecord]]:
            mem = self.app.mem
            layout = scan_live_officer_layout(mem)
            officers = list_live_officers(mem, layout)
            if not officers:
                raise MemoryErrorWin32("已定位武将表，但未读取到有效武将记录。")
            return layout, officers

        def done(result: tuple[LiveOfficerLayout, List[LiveOfficerRecord]]) -> None:
            layout, officers = result
            self._pull_live_custom_traits_into_catalog()
            self.live_layout = layout
            self.live_officers = officers
            first = officers[0]
            self.current_live_officer = first
            self._refresh_officer_dropdown(select_record=first)
            self._load_officer_into_draft(first)
            self._update_source_banner()
            self.app.status_var.set(
                f"已扫描剧本全部武将：共 {len(officers)} 人（结构校验 {layout.verified_matches} 名史实武将）"
            )
            self.app._play_beep(True)

        self._run_background(work, done, "正在扫描当前剧本中的全部武将数据表（约 1~3 秒）...")

    # ---------------- Save Mode Operations ----------------

    def open_steam_save_picker(self) -> None:
        dirs = discover_save_directories()
        recent: List[Path] = []
        for folder in dirs:
            recent.extend(list_recent_save_files(folder))
        if not recent:
            messagebox.showinfo(
                "未自动找到 Steam 存档",
                "未能在默认 Steam userdata 目录找到三国志14 存档，请手动选择 .s14 存档文件。",
                parent=self.app,
            )
            self.open_save_file_dialog()
            return

        win = tk.Toplevel(self.app)
        win.title("选择 Steam 三国志14 存档 (.s14)")
        win.geometry("620x420")
        win.configure(bg=self.app.colors["bg"])
        win.transient(self.app)
        win.grab_set()

        ttk.Label(
            win,
            text=f"目录：{recent[0].parent}\n提示：含 exce 的为威力加强传承版存档（autosdexce / svdexce）。",
            style="Muted.TLabel",
            padding=(14, 12, 14, 6),
        ).pack(anchor=tk.W)

        frame = ttk.Frame(win, padding=(14, 4, 14, 10))
        frame.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(frame, columns=("name", "size", "mtime"), show="headings", selectmode="browse")
        tree.heading("name", text="存档文件名")
        tree.heading("size", text="大小")
        tree.heading("mtime", text="修改时间")
        tree.column("name", width=260, anchor=tk.W)
        tree.column("size", width=90, anchor=tk.E)
        tree.column("mtime", width=180, anchor=tk.CENTER)
        scr = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scr.set)
        scr.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        from datetime import datetime

        for idx, p in enumerate(recent):
            st = p.stat()
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            tree.insert("", tk.END, iid=str(idx), values=(p.name, f"{st.st_size // 1024} KB", mtime))
        if recent:
            tree.selection_set("0")

        def confirm() -> None:
            sel = tree.selection()
            if not sel:
                return
            chosen = recent[int(sel[0])]
            win.destroy()
            self.load_save_file(chosen)

        tree.bind("<Double-1>", lambda _e: confirm())
        btns = ttk.Frame(win, padding=(14, 0, 14, 12))
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="手动浏览其他目录...", command=lambda: (win.destroy(), self.open_save_file_dialog())).pack(
            side=tk.LEFT
        )
        ttk.Button(btns, text="打开选中存档", style="Accent.TButton", command=confirm).pack(side=tk.RIGHT)

    def open_save_file_dialog(self) -> None:
        dirs = discover_save_directories()
        initial_dir = str(dirs[0]) if dirs else str(Path.home())
        path = filedialog.askopenfilename(
            title="选择三国志14 存档文件 (.s14)",
            initialdir=initial_dir,
            filetypes=[("三国志14 存档", "*.s14"), ("所有文件", "*.*")],
            parent=self.app,
        )
        if path:
            self.load_save_file(Path(path))

    def load_save_file(self, path: Path) -> None:
        def work() -> SaveDocument:
            return SaveDocument.load(path)

        def done(doc: SaveDocument) -> None:
            self.save_doc = doc
            self.mode_var.set("save")
            self._sync_mode_widgets()
            # Default to Cao Cao if present, else first officer
            preferred = doc.search_officers("曹操")
            first = preferred[0] if preferred else (doc.officers[0] if doc.officers else None)
            self.current_save_officer = first
            self._refresh_officer_dropdown(select_record=first)
            if first is not None:
                self._load_officer_into_draft(first)
            else:
                self._clear_draft()
            self._update_source_banner()
            self.app.status_var.set(
                f"已加载存档 {doc.path.name}：{doc.metadata.scenario}（{doc.metadata.ruler}），共 {len(doc.officers)} 名武将"
            )
            self.app._play_beep(True)

        self._run_background(work, done, f"正在解压并解析存档 {path.name}...")

    def save_current_document(self, target_path: Optional[Path] = None) -> None:
        if self.save_doc is None:
            messagebox.showwarning("尚未加载存档", "请先打开一个 .s14 存档文件。", parent=self.app)
            return
        doc = self.save_doc

        def work() -> Path:
            return doc.save(target_path=target_path, create_backup=True)

        def done(saved_path: Path) -> None:
            self._update_source_banner()
            bak = saved_path.with_suffix(saved_path.suffix + ".bak")
            bak_note = f"（已自动保留备份 {bak.name}）" if bak.exists() else ""
            self.app.status_var.set(f"存档保存成功：{saved_path.name}{bak_note}")
            self.app._play_beep(True)
            messagebox.showinfo("保存成功", f"已写入并校验存档：\n{saved_path}\n{bak_note}", parent=self.app)

        self._run_background(work, done, f"正在重新压缩并校验存档 {doc.path.name}...")

    def save_as_dialog(self) -> None:
        if self.save_doc is None:
            messagebox.showwarning("尚未加载存档", "请先打开一个 .s14 存档文件。", parent=self.app)
            return
        path = filedialog.asksaveasfilename(
            title="另存为三国志14 存档 (.s14)",
            initialdir=str(self.save_doc.path.parent),
            initialfile=self.save_doc.path.name,
            defaultextension=".s14",
            filetypes=[("三国志14 存档", "*.s14")],
            parent=self.app,
        )
        if path:
            self.save_current_document(target_path=Path(path))

    # ---------------- Officer Selection & Draft Sync ----------------

    def _refresh_officer_dropdown(self, select_record: Optional[LiveOfficerRecord | SaveOfficer] = None) -> None:
        query = self.officer_filter_var.get().strip().lower()
        if self.mode_var.get() == "live":
            pool: Sequence[LiveOfficerRecord | SaveOfficer] = self.live_officers
        else:
            pool = self.save_doc.officers if self.save_doc is not None else ()

        if query:
            filtered = [
                off
                for off in pool
                if query in off.full_name.lower()
                or query in off.zi.lower()
                or query in getattr(off, "pinyin", "").lower()
                or query == str(off.index)
            ]
        else:
            filtered = list(pool)

        if select_record is not None and select_record not in filtered:
            filtered.insert(0, select_record)

        self._filtered_officers = filtered[:400]
        labels = [off.display_label for off in self._filtered_officers]
        for cb in self._officer_combos:
            cb["values"] = labels

        if select_record is not None and select_record in self._filtered_officers:
            self.officer_combo_var.set(select_record.display_label)
        elif self._filtered_officers and self.officer_combo_var.get() not in labels:
            self.officer_combo_var.set(labels[0])
            self._select_officer_from_list(0)
        elif not labels:
            self.officer_combo_var.set("")

    def _on_officer_combo_selected(self, event: tk.Event) -> None:
        widget = event.widget
        if isinstance(widget, ttk.Combobox):
            idx = widget.current()
            if 0 <= idx < len(self._filtered_officers):
                self._select_officer_from_list(idx)

    def _select_officer_from_list(self, idx: int) -> None:
        rec = self._filtered_officers[idx]
        if self.mode_var.get() == "live" and isinstance(rec, LiveOfficerRecord):
            # Re-read fresh bytes from memory so we never edit stale data!
            if self.live_layout is not None and self.app.mem.is_attached:
                try:
                    rec = read_live_officer(self.app.mem, self.live_layout, rec.address)
                except MemoryErrorWin32:
                    pass
            self.current_live_officer = rec
        elif isinstance(rec, SaveOfficer):
            self.current_save_officer = rec
        self._load_officer_into_draft(rec)

    def _load_officer_into_draft(self, rec: LiveOfficerRecord | SaveOfficer) -> None:
        self.draft_traits = list(rec.traits[:TRAIT_SLOTS]) + [0] * max(0, TRAIT_SLOTS - len(rec.traits))
        self.draft_tactics = list(rec.tactics[:TACTIC_SLOTS]) + [0] * max(0, TACTIC_SLOTS - len(rec.tactics))
        extra = f"内存 {hex(rec.address)}" if isinstance(rec, LiveOfficerRecord) else f"存档索引 #{rec.index}"
        self.target_banner_var.set(
            f"当前：{rec.full_name}  五维 {'/'.join(map(str, rec.stats))}  ({extra})"
        )
        self._refresh_trait_slots()
        self._refresh_tactic_slots()

    def _clear_draft(self) -> None:
        self.draft_traits = [0] * TRAIT_SLOTS
        self.draft_tactics = [0] * TACTIC_SLOTS
        self.target_banner_var.set("未选中武将")
        self._refresh_trait_slots()
        self._refresh_tactic_slots()

    # ---------------- Skill Presets (战法 / 个性预设方案) ----------------

    def _load_custom_presets(self) -> None:
        self.custom_presets = {}
        if not self.presets_path.is_file():
            return
        try:
            raw = json.loads(self.presets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for name, body in raw.items():
            if not isinstance(name, str) or not isinstance(body, dict):
                continue
            traits = body.get("traits")
            tactics = body.get("tactics")
            if isinstance(traits, list) and isinstance(tactics, list):
                t_list = [int(v) for v in traits[:TRAIT_SLOTS]] + [0] * max(0, TRAIT_SLOTS - len(traits))
                k_list = [int(v) for v in tactics[:TACTIC_SLOTS]] + [0] * max(0, TACTIC_SLOTS - len(tactics))
                self.custom_presets[name] = {"traits": t_list, "tactics": k_list}

    def _save_custom_presets(self) -> None:
        if not self.custom_presets:
            try:
                self.presets_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.presets_path.parent.mkdir(parents=True, exist_ok=True)
        self.presets_path.write_text(
            json.dumps(self.custom_presets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _all_presets(self) -> Dict[str, Dict[str, List[int]]]:
        merged: Dict[str, Dict[str, List[int]]] = dict(BUILTIN_SKILL_PRESETS)
        merged.update(self.custom_presets)
        return merged

    def _refresh_preset_combos(self) -> None:
        names = list(self._all_presets().keys())
        for cb in self._preset_combos:
            if cb.winfo_exists():
                cb["values"] = names
        if names and self.preset_var.get() not in names:
            self.preset_var.set(names[0])

    def apply_skill_preset(self, preset_name: Optional[str] = None) -> None:
        name = preset_name or self.preset_var.get()
        preset = self._all_presets().get(name)
        if preset is None:
            self.app.status_var.set("未找到所选预设方案。")
            return
        self.draft_traits = list(preset["traits"][:TRAIT_SLOTS]) + [0] * max(0, TRAIT_SLOTS - len(preset["traits"]))
        self.draft_tactics = list(preset["tactics"][:TACTIC_SLOTS]) + [0] * max(0, TACTIC_SLOTS - len(preset["tactics"]))
        self._refresh_trait_slots()
        self._refresh_tactic_slots()
        self.app.status_var.set(
            f"已套用预设「{name}」（个性 {sum(1 for x in self.draft_traits if x)} 项 / 战法 {sum(1 for x in self.draft_tactics if x)} 项），请点击「应用修改」写入。"
        )
        self.app._play_beep(True)

    def save_current_as_preset(self, preset_name: Optional[str] = None) -> None:
        name = preset_name
        if name is None:
            name = simpledialog.askstring("保存预设方案", "请输入预设名称（例如：我的无双吕布）：", parent=self.app)
        if not name or not name.strip():
            return
        clean = name.strip()
        if clean in BUILTIN_SKILL_PRESETS:
            messagebox.showwarning("名称不可用", "不能覆盖内置预设名称，请换一个自定义名称。", parent=self.app)
            return
        self.custom_presets[clean] = {
            "traits": list(self.draft_traits[:TRAIT_SLOTS]),
            "tactics": list(self.draft_tactics[:TACTIC_SLOTS]),
        }
        try:
            self._save_custom_presets()
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.app)
            return
        self.preset_var.set(clean)
        self._refresh_preset_combos()
        self.app.status_var.set(f"已保存自定义预设方案「{clean}」")
        self.app._play_beep(True)

    def delete_selected_preset(self, preset_name: Optional[str] = None) -> bool:
        name = preset_name or self.preset_var.get()
        if name in BUILTIN_SKILL_PRESETS:
            self.app.status_var.set("内置预设方案不可删除。")
            return False
        if name not in self.custom_presets:
            return False
        del self.custom_presets[name]
        try:
            self._save_custom_presets()
        except OSError as exc:
            self.app.status_var.set(f"删除预设失败：{exc}")
            return False
        self._refresh_preset_combos()
        self.app.status_var.set(f"已删除自定义预设方案「{name}」")
        return True

    # ---------------- Traits Table & Actions ----------------

    def _trait_tag(self, color: int) -> str:
        return {3: "gold", 1: "blue", 2: "red"}.get(color, "")

    def _populate_trait_catalog(self) -> None:
        self.trait_catalog_hdr_var.set(f"可选个性库（{len(self.catalog.traits)} 项）")
        if self.trait_catalog_tree is None:
            return
        color_label = self.trait_color_var.get()
        color_map = {"金色": 3, "蓝色": 1, "红色": 2, "特殊": 0}
        color_filter = color_map.get(color_label)
        items = self.catalog.search_traits(self.trait_search_var.get(), color_filter=color_filter)
        self.trait_catalog_tree.delete(*self.trait_catalog_tree.get_children())
        self._trait_catalog_items = []
        for item in items:
            self._trait_catalog_items.append(item.id)
            tag = self._trait_tag(item.color)
            self.trait_catalog_tree.insert(
                "",
                tk.END,
                iid=str(item.id),
                values=(item.id, item.name, item.color_label, item.description),
                tags=(tag,) if tag else (),
            )

    def _refresh_trait_slots(self, keep_slot: Optional[int] = None) -> None:
        if self.trait_slot_tree is None:
            return
        prev_sel = self.trait_slot_tree.selection()
        selected_slot = keep_slot if keep_slot is not None else (int(prev_sel[0]) if prev_sel else 0)
        self.trait_slot_tree.delete(*self.trait_slot_tree.get_children())
        for idx, tid in enumerate(self.draft_traits):
            if tid == 0:
                self.trait_slot_tree.insert(
                    "", tk.END, iid=str(idx), values=(f"槽位 {idx + 1}", "-", "（空）", "-", ""), tags=("empty",)
                )
            else:
                info = self.catalog.trait_by_id.get(tid)
                if info is not None:
                    tag = self._trait_tag(info.color)
                    self.trait_slot_tree.insert(
                        "",
                        tk.END,
                        iid=str(idx),
                        values=(f"槽位 {idx + 1}", info.id, info.name, info.color_label, info.description),
                        tags=(tag,) if tag else (),
                    )
                else:
                    self.trait_slot_tree.insert(
                        "", tk.END, iid=str(idx), values=(f"槽位 {idx + 1}", tid, f"个性#{tid}", "未知", "")
                    )
        if 0 <= selected_slot < TRAIT_SLOTS:
            self.trait_slot_tree.selection_set(str(selected_slot))

    def _assign_selected_trait(self, *, add_to_empty: bool) -> None:
        if self.trait_catalog_tree is None or self.trait_slot_tree is None:
            return
        cat_sel = self.trait_catalog_tree.selection()
        if not cat_sel:
            self.app.status_var.set("请先在右侧个性库中选中一个个性。")
            return
        trait_id = int(cat_sel[0])
        if add_to_empty:
            if 0 not in self.draft_traits:
                self.app.status_var.set("5 个个性槽位已满，请先选中左侧槽位进行替换或清空。")
                return
            slot = self.draft_traits.index(0)
        else:
            slot_sel = self.trait_slot_tree.selection()
            slot = int(slot_sel[0]) if slot_sel else 0
        self.draft_traits[slot] = trait_id
        self._refresh_trait_slots(keep_slot=min(slot + 1, TRAIT_SLOTS - 1))
        if self.mode_var.get() == "live" and self.app.mem.is_attached and self.live_layout is not None and self.current_live_officer is not None:
            self.apply_traits()
        else:
            self.app.status_var.set(f"已将槽位 {slot + 1} 设为「{self.catalog.trait_name(trait_id)}」，点击「应用个性修改」生效。")

    def _clear_selected_trait_slot(self) -> None:
        if self.trait_slot_tree is None:
            return
        sel = self.trait_slot_tree.selection()
        if not sel:
            return
        slot = int(sel[0])
        self.draft_traits[slot] = 0
        self._refresh_trait_slots(keep_slot=slot)
        if self.mode_var.get() == "live" and self.app.mem.is_attached and self.live_layout is not None and self.current_live_officer is not None:
            self.apply_traits()

    def _clear_all_trait_slots(self) -> None:
        self.draft_traits = [0] * TRAIT_SLOTS
        self._refresh_trait_slots(keep_slot=0)
        if self.mode_var.get() == "live" and self.app.mem.is_attached and self.live_layout is not None and self.current_live_officer is not None:
            self.apply_traits()

    def _ensure_live_custom_traits_synced(self, trait_ids: Sequence[int]) -> None:
        """Ensure any custom traits (256..355) being equipped are written into live CPersonalityData."""
        if not self.app.mem.is_attached:
            return
        workshop = getattr(self, "custom_workshop", None)
        for tid in trait_ids:
            if not (256 <= int(tid) <= 355):
                continue
            ct = self.custom_traits.get(int(tid))
            if ct is None and workshop is not None:
                ct = workshop.live_traits.get(int(tid)) or workshop.edit_file_traits.get(int(tid))
            if ct is not None:
                try:
                    write_live_custom_trait(self.app.mem, ct)
                except Exception:
                    pass

    def apply_traits(self) -> None:
        if self.mode_var.get() == "live":
            if not self.app.mem.is_attached or self.live_layout is None or self.current_live_officer is None:
                messagebox.showwarning("尚未读取武将", "请先连接游戏并点击「读取当前详情武将」或「扫描剧本全部武将」。", parent=self.app)
                return
            try:
                self._ensure_live_custom_traits_synced(self.draft_traits)
                written = write_live_traits(
                    self.app.mem,
                    self.live_layout,
                    self.current_live_officer.address,
                    self.draft_traits,
                    label=f"{self.current_live_officer.full_name}.个性",
                )
                self.current_live_officer.traits = written
                self.app.status_var.set(
                    f"已实时写入 {self.current_live_officer.full_name} 个性："
                    + "、".join(self.catalog.trait_name(t) for t in written if t)
                    + "（在游戏内点一下「能力」再切回「基本」或重开详情即刷新显示）"
                )
                self.app._play_beep(True)
            except Exception as exc:
                self.app.status_var.set(f"实时写入个性失败：{exc}")
                self.app._play_beep(False)
                messagebox.showerror("写入失败", str(exc), parent=self.app)
        else:
            if self.save_doc is None or self.current_save_officer is None:
                messagebox.showwarning("尚未加载存档", "请先打开 .s14 存档并选择目标武将。", parent=self.app)
                return
            try:
                updated = self.save_doc.set_officer_traits(self.current_save_officer.index, self.draft_traits)
                self.current_save_officer = updated
                self._update_source_banner()
                self.app.status_var.set(
                    f"已更新存档缓冲中 {updated.full_name} 的个性（共 {sum(1 for t in updated.traits if t)} 项），请点击「保存存档文件」写入磁盘。"
                )
                self.app._play_beep(True)
            except Exception as exc:
                self.app.status_var.set(f"更新存档个性失败：{exc}")
                messagebox.showerror("修改失败", str(exc), parent=self.app)

    # ---------------- Tactics Table & Actions ----------------

    def _tactic_tag(self, tier: str) -> str:
        return {"固有": "unique", "通用": "normal", "DLC": "dlc"}.get(tier, "")

    def _populate_tactic_catalog(self) -> None:
        self.tactic_catalog_hdr_var.set(f"可选战法库（{len(self.catalog.tactics)} 项）")
        if self.tactic_catalog_tree is None:
            return
        tier_label = self.tactic_tier_var.get()
        tier_filter = None if tier_label == "全部级别" else tier_label
        cat_label = self.tactic_cat_var.get()
        cat_map = {v: k for k, v in TACTIC_CATEGORY_NAMES.items() if k > 0}
        cat_filter = cat_map.get(cat_label)
        items = self.catalog.search_tactics(
            self.tactic_search_var.get(), tier_filter=tier_filter, category_filter=cat_filter
        )
        self.tactic_catalog_tree.delete(*self.tactic_catalog_tree.get_children())
        self._tactic_catalog_items = []
        for item in items:
            self._tactic_catalog_items.append(item.id)
            tag = self._tactic_tag(item.tier)
            self.tactic_catalog_tree.insert(
                "",
                tk.END,
                iid=str(item.id),
                values=(item.id, item.name, item.category_label, item.tier, item.pinyin),
                tags=(tag,) if tag else (),
            )

    def _refresh_tactic_slots(self, keep_slot: Optional[int] = None) -> None:
        if self.tactic_slot_tree is None:
            return
        prev_sel = self.tactic_slot_tree.selection()
        selected_slot = keep_slot if keep_slot is not None else (int(prev_sel[0]) if prev_sel else 0)
        self.tactic_slot_tree.delete(*self.tactic_slot_tree.get_children())
        for idx, kid in enumerate(self.draft_tactics):
            slot_label = "槽位 1 (固有)" if idx == 0 else f"槽位 {idx + 1}"
            if kid == 0:
                self.tactic_slot_tree.insert(
                    "", tk.END, iid=str(idx), values=(slot_label, "-", "（空）", "-", "-"), tags=("empty",)
                )
            else:
                info = self.catalog.tactic_by_id.get(kid)
                if info is not None:
                    tag = self._tactic_tag(info.tier)
                    self.tactic_slot_tree.insert(
                        "",
                        tk.END,
                        iid=str(idx),
                        values=(slot_label, info.id, info.name, info.category_label, info.tier),
                        tags=(tag,) if tag else (),
                    )
                else:
                    self.tactic_slot_tree.insert(
                        "", tk.END, iid=str(idx), values=(slot_label, kid, f"战法#{kid}", "未知", "未知")
                    )
        if 0 <= selected_slot < TACTIC_SLOTS:
            self.tactic_slot_tree.selection_set(str(selected_slot))

    def _assign_selected_tactic(self, *, add_to_empty: bool) -> None:
        if self.tactic_catalog_tree is None or self.tactic_slot_tree is None:
            return
        cat_sel = self.tactic_catalog_tree.selection()
        if not cat_sel:
            self.app.status_var.set("请先在右侧战法库中选中一个战法。")
            return
        tactic_id = int(cat_sel[0])
        if add_to_empty:
            if 0 not in self.draft_tactics:
                self.app.status_var.set("10 个战法槽位已满，请先选中左侧槽位进行替换或清空。")
                return
            slot = self.draft_tactics.index(0)
        else:
            slot_sel = self.tactic_slot_tree.selection()
            slot = int(slot_sel[0]) if slot_sel else 0
        self.draft_tactics[slot] = tactic_id
        self._refresh_tactic_slots(keep_slot=min(slot + 1, TACTIC_SLOTS - 1))
        self.app.status_var.set(f"已将槽位 {slot + 1} 设为「{self.catalog.tactic_name(tactic_id)}」，点击「应用战法修改」生效。")

    def _clear_selected_tactic_slot(self) -> None:
        if self.tactic_slot_tree is None:
            return
        sel = self.tactic_slot_tree.selection()
        if not sel:
            return
        slot = int(sel[0])
        self.draft_tactics[slot] = 0
        self._refresh_tactic_slots(keep_slot=slot)

    def _clear_all_tactic_slots(self) -> None:
        self.draft_tactics = [0] * TACTIC_SLOTS
        self._refresh_tactic_slots(keep_slot=0)

    def apply_tactics(self) -> None:
        if self.mode_var.get() == "live":
            if not self.app.mem.is_attached or self.live_layout is None or self.current_live_officer is None:
                messagebox.showwarning("尚未读取武将", "请先连接游戏并点击「读取当前详情武将」或「扫描剧本全部武将」。", parent=self.app)
                return
            try:
                written = write_live_tactics(
                    self.app.mem,
                    self.live_layout,
                    self.current_live_officer.address,
                    self.draft_tactics,
                    label=f"{self.current_live_officer.full_name}.战法",
                )
                self.current_live_officer.tactics = written
                self.app.status_var.set(
                    f"已实时写入 {self.current_live_officer.full_name} 战法："
                    + "、".join(self.catalog.tactic_name(k) for k in written if k)
                    + "（在游戏内切换页签或重开详情即刷新）"
                )
                self.app._play_beep(True)
            except Exception as exc:
                self.app.status_var.set(f"实时写入战法失败：{exc}")
                self.app._play_beep(False)
                messagebox.showerror("写入失败", str(exc), parent=self.app)
        else:
            if self.save_doc is None or self.current_save_officer is None:
                messagebox.showwarning("尚未加载存档", "请先打开 .s14 存档并选择目标武将。", parent=self.app)
                return
            try:
                updated = self.save_doc.set_officer_tactics(self.current_save_officer.index, self.draft_tactics)
                self.current_save_officer = updated
                self._update_source_banner()
                self.app.status_var.set(
                    f"已更新存档缓冲中 {updated.full_name} 的战法（共 {sum(1 for k in updated.tactics if k)} 项），请点击「保存存档文件」写入磁盘。"
                )
                self.app._play_beep(True)
            except Exception as exc:
                self.app.status_var.set(f"更新存档战法失败：{exc}")
                messagebox.showerror("修改失败", str(exc), parent=self.app)

    # ---------------- Unrestricted Custom Traits (无限制自创个性) ----------------

    def _pull_live_custom_traits_into_catalog(self) -> None:
        self.refresh_page_data(silent=True, reload_officer=False)

    def refresh_page_data(self, *, silent: bool = False, reload_officer: bool = True) -> None:
        """Reload all custom traits (disk, global file, live memory) into the catalog and refresh UI tables."""
        merged: Dict[int, CustomTraitDef] = {}

        # 1. Start with live in-game custom traits if attached
        if self.app.mem.is_attached:
            try:
                for ct in list_live_custom_traits(self.app.mem):
                    merged[ct.slot_id] = ct
            except MemoryErrorWin32:
                pass

        # 2. Overlay global editpersonalityexceSC.s14 file
        workshop = getattr(self, "custom_workshop", None)
        edit_p = (workshop.edit_file_path if workshop is not None and workshop.edit_file_path else None) or discover_edit_personality_file()
        if edit_p is not None and edit_p.is_file():
            try:
                for ct in load_edit_personality_file(edit_p).values():
                    merged[ct.slot_id] = ct
            except Exception:
                pass

        # 3. Overlay local custom_traits.json & workshop latest edits (highest priority so user edits never get overwritten by stale memory)
        disk_custom = load_custom_traits_file(self.custom_traits_path)
        for sid, ct in disk_custom.items():
            merged[sid] = ct
        for sid, ct in self.custom_traits.items():
            merged[sid] = ct
        self.custom_traits = dict(merged)

        # 4. If attached to live game, push latest custom trait definitions into live CPersonalityData so game memory stays in sync
        if self.app.mem.is_attached and merged:
            for ct in merged.values():
                try:
                    write_live_custom_trait(self.app.mem, ct)
                except Exception:
                    pass

        # 5. Remove deleted custom trait IDs (256..355) and upsert all active custom traits into catalog
        for sid in range(256, 356):
            if sid not in merged and sid in self.catalog.trait_by_id:
                self.catalog.remove_trait(sid)
        for ct in merged.values():
            self.catalog.upsert_trait(ct.to_trait_info())

        # 6. If reload_officer is True and an officer is currently loaded in live mode, re-read their latest traits & tactics
        if reload_officer and self.mode_var.get() == "live" and self.app.mem.is_attached and self.live_layout is not None and self.current_live_officer is not None:
            try:
                fresh = read_live_officer(self.app.mem, self.live_layout, self.current_live_officer.address)
                self.current_live_officer = fresh
                self.draft_traits = list(fresh.traits)
                self.draft_tactics = list(fresh.tactics)
                self._update_source_banner()
            except Exception:
                pass

        self._populate_trait_catalog()
        self._populate_tactic_catalog()
        self._refresh_trait_slots()
        self._refresh_tactic_slots()
        if workshop is not None:
            workshop.live_traits = dict(merged)
            workshop._refresh_tree()

        if not silent:
            self.app.status_var.set(
                f"已刷新页面：可选个性库共 {len(self.catalog.traits)} 项（含 {len(merged)} 项自创个性），可选战法库共 {len(self.catalog.tactics)} 项"
            )
            self.app._play_beep(True)

    def build_custom_traits_tab(self, parent: ttk.Frame) -> None:
        self.custom_workshop = CustomTraitWorkshopDialog(
            self,
            on_changed=self._on_custom_trait_changed,
            parent=parent,
        )

    def open_custom_trait_workshop(self) -> None:
        tab = getattr(self.app, "custom_trait_tab", None)
        if tab is not None:
            self.app.notebook.select(tab)
            if hasattr(self, "custom_workshop"):
                self.custom_workshop._reload_all_sources(silent=True)
            return
        CustomTraitWorkshopDialog(self, on_changed=self._on_custom_trait_changed)

    def _on_custom_trait_changed(self, slot_id: Optional[int], equip: bool) -> None:
        self._populate_trait_catalog()
        if slot_id is not None and equip:
            if self.mode_var.get() == "live" and self.app.mem.is_attached and self.current_live_officer is None:
                try:
                    self.read_current_detail_officer()
                except Exception:
                    pass
            if slot_id not in self.draft_traits:
                if 0 in self.draft_traits:
                    pos = self.draft_traits.index(0)
                elif self.trait_slot_tree is not None and self.trait_slot_tree.selection():
                    pos = int(self.trait_slot_tree.selection()[0])
                else:
                    pos = 0
                self.draft_traits[pos] = slot_id
                next_pos = (pos + 1) % TRAIT_SLOTS
            else:
                next_pos = (self.draft_traits.index(slot_id) + 1) % TRAIT_SLOTS
            self._refresh_trait_slots(keep_slot=next_pos)
            # If a live officer is already loaded, apply immediately in one click!
            if self.mode_var.get() == "live" and self.app.mem.is_attached and self.live_layout is not None and self.current_live_officer is not None:
                self.apply_traits()
                return
            self.app.status_var.set(
                f"已创建并放入槽位：[{slot_id}]「{self.catalog.trait_name(slot_id)}」，请先读取目标武将后点击「应用个性修改」"
            )
        else:
            self._refresh_trait_slots()
            if slot_id is not None:
                self.app.status_var.set(
                    f"已更新自创个性 [{slot_id}]「{self.catalog.trait_name(slot_id)}」，现在可在右侧个性库中直接搜索并装备"
                )

    def sync_custom_traits_to_game(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先连接游戏并进入剧本。", parent=self.app)
            return
        if not self.custom_traits:
            messagebox.showinfo("本地自创库为空", "请先点击「无限制自创个性...」创建至少一个自创个性。", parent=self.app)
            return
        count = 0
        try:
            for ct in self.custom_traits.values():
                written = write_live_custom_trait(self.app.mem, ct)
                self.catalog.upsert_trait(written.to_trait_info())
                count += 1
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc), parent=self.app)
            return
        self._populate_trait_catalog()
        self._refresh_trait_slots()
        self.app.status_var.set(f"已将本地库全部 {count} 个无限制自创个性注入当前剧本")
        self.app._play_beep(True)
