"""GUI tabs for Officer Deep Editor (武将进阶) and World / Force / Diplomacy / Treasures (势力/外交/名品)."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from game_world_editor import (
    DOCTRINE_NAMES,
    FORMATIONS,
    FORMATION_MASK_ALL,
    FORMATION_MASK_LAND,
    FORMATION_MASK_LAND_WATER,
    ILLNESS_NAMES,
    DiplomacyEntry,
    ForeignRelationEntry,
    OfficerDeepInfo,
    TreasureEntry,
    WorldContext,
    batch_boost_player_officers,
    batch_max_player_areas,
    batch_supply_player_cities,
    grant_item_to_officer,
    list_diplomacy,
    list_items,
    list_live_person_index,
    read_officer_deep,
    read_policy_catalog,
    read_world_context,
    resolve_officer_id_from_address,
    set_all_diplomacy_max,
    set_force_friendliness,
    set_foreign_relation,
    write_officer_deep,
)
from memory_engine import MemoryErrorWin32
from officer_auto_locator import read_live_targets, resolve_live_global_field, supported_executable
from officer_probe import HERITAGE_STATS_OFFSET
from profile_manager import parse_int


class WorldEditorController:
    """Manages the '武将进阶' and '势力 / 外交 / 名品' tabs for live SAN14CE_SC.exe sessions."""

    def __init__(self, app: Any) -> None:
        self.app = app

        # Officer Deep Editor state
        self.deep_banner_var = tk.StringVar(value="未读取武将 · 点击「读取当前详情武将」或「加载全部武将」")
        self.deep_search_var = tk.StringVar(value="")
        self.deep_combo_var = tk.StringVar(value="")
        self.person_index: List[Tuple[int, int, str, int]] = []  # (oid, addr, name, dist)
        self._filtered_persons: List[Tuple[int, int, str, int]] = []
        self.current_deep: Optional[OfficerDeepInfo] = None
        self.policies: List[Tuple[int, str]] = []

        self.formation_vars: Dict[int, tk.BooleanVar] = {bit: tk.BooleanVar(value=False) for bit, _, _ in FORMATIONS}
        self.deep_birth_var = tk.StringVar(value="180")
        self.deep_death_var = tk.StringVar(value="260")
        self.deep_loyalty_var = tk.StringVar(value="100")
        self.deep_illness_var = tk.StringVar(value="0 - 健康")
        self.deep_ism_var = tk.StringVar(value="1 - 王道")
        self.deep_policy_var = tk.StringVar(value="")
        self.deep_policy_lv_var = tk.StringVar(value="5")
        self.deep_cap_var = tk.StringVar(value="100")

        self.deep_combo: Optional[ttk.Combobox] = None
        self.deep_policy_combo: Optional[ttk.Combobox] = None

        # World / Force / Diplomacy / Treasures state
        self.world_summary_var = tk.StringVar(value="未刷新 · 连接游戏进入剧本后点击「刷新世界数据」")
        self.world_ctx: Optional[WorldContext] = None

        self.batch_gold_var = tk.StringVar(value="999999")
        self.batch_food_var = tk.StringVar(value="999999")
        self.batch_troops_var = tk.StringVar(value="200000")
        self.batch_dur_var = tk.StringVar(value="60000")
        self.batch_morale_var = tk.StringVar(value="200")
        self.batch_area_dev_var = tk.StringVar(value="1000")
        self.batch_area_order_var = tk.StringVar(value="100")
        self.batch_area_cap_var = tk.StringVar(value="")

        self.diplo_entries: List[DiplomacyEntry] = []
        self.foreign_entries: List[ForeignRelationEntry] = []
        self.diplo_val_var = tk.StringVar(value="100")
        self.foreign_val_var = tk.StringVar(value="1000")
        self.foreign_lv_var = tk.StringVar(value="4")
        self.diplo_tree: Optional[ttk.Treeview] = None
        self.foreign_tree: Optional[ttk.Treeview] = None

        self.all_items: List[TreasureEntry] = []
        self.filtered_items: List[TreasureEntry] = []
        self.item_search_var = tk.StringVar(value="")
        self.item_filter_var = tk.StringVar(value="全部名品")
        self.item_target_search_var = tk.StringVar(value="")
        self.item_target_combo_var = tk.StringVar(value="")
        self._item_target_persons: List[Tuple[int, int, str, int]] = []
        self.item_tree: Optional[ttk.Treeview] = None
        self.item_target_combo: Optional[ttk.Combobox] = None

    def on_process_attached(self) -> None:
        self.current_deep = None
        self.world_ctx = None
        self.person_index = []
        self.policies = []
        self.all_items = []
        self.diplo_entries = []
        self.foreign_entries = []
        self.deep_banner_var.set("已连接游戏 · 点击「读取当前详情武将」或「加载全部武将」")
        self.world_summary_var.set("已连接游戏 · 点击「刷新世界数据」加载势力、外交与名品")

    # ========================================================================
    # Tab: 武将进阶 (Officer Deep Editor)
    # ========================================================================

    def build_officer_deep_tab(self, parent: ttk.Frame) -> None:
        hdr = ttk.Frame(parent, padding=(16, 12, 16, 4))
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="武将进阶编辑（阵型 / 延寿 / 主义 / 政策 / 能力上限）", style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Label(hdr, textvariable=self.deep_banner_var, style="Badge.TLabel").pack(side=tk.RIGHT)

        bar = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        bar.pack(fill=tk.X, padx=16, pady=(4, 8))
        ttk.Button(bar, text="读取当前详情武将", style="Accent.TButton", command=self.read_current_deep_officer).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bar, text="加载全部武将列表", command=self.load_all_persons).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(bar, text="搜索", style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        s_entry = ttk.Entry(bar, textvariable=self.deep_search_var, width=12)
        s_entry.pack(side=tk.LEFT, padx=(0, 10))
        s_entry.bind("<KeyRelease>", lambda _e: self._refresh_deep_combo())

        ttk.Label(bar, text="选择武将", style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        self.deep_combo = ttk.Combobox(bar, textvariable=self.deep_combo_var, state="readonly", width=26)
        self.deep_combo.pack(side=tk.LEFT)
        self.deep_combo.bind("<<ComboboxSelected>>", self._on_deep_combo_selected)

        body = ttk.Frame(parent, padding=(16, 4, 16, 6))
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=5)

        # Left card: Formations
        fcard = ttk.Frame(body, style="Card.TFrame", padding=16)
        fcard.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ttk.Label(fcard, text="可出征阵型（共 14 项）", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 10))

        groups = ("陆战", "攻城", "水战", "异族")
        for grp in groups:
            gframe = ttk.Frame(fcard, style="Card.TFrame")
            gframe.pack(fill=tk.X, pady=(4, 6))
            ttk.Label(gframe, text=f"{grp}：", style="CardMuted.TLabel", width=6).pack(side=tk.LEFT, anchor=tk.N, pady=3)
            items_frame = ttk.Frame(gframe, style="Card.TFrame")
            items_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
            for idx, (bit, name, g) in enumerate([x for x in FORMATIONS if x[2] == grp]):
                ttk.Checkbutton(items_frame, text=name, variable=self.formation_vars[bit]).grid(
                    row=idx // 4, column=idx % 4, sticky=tk.W, padx=(0, 12), pady=3
                )

        ttk.Separator(fcard).pack(fill=tk.X, pady=12)
        fbtns = ttk.Frame(fcard, style="Card.TFrame")
        fbtns.pack(fill=tk.X)
        ttk.Button(fbtns, text="全陆战阵型", command=lambda: self._set_formation_mask(FORMATION_MASK_LAND)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fbtns, text="全水陆阵型", command=lambda: self._set_formation_mask(FORMATION_MASK_LAND_WATER)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fbtns, text="含四夷全解锁", command=lambda: self._set_formation_mask(FORMATION_MASK_ALL)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fbtns, text="清空", command=lambda: self._set_formation_mask(0)).pack(side=tk.RIGHT)

        # Right card: Lifespan, Loyalty, Policy, Doctrine, Stat Cap
        rcard = ttk.Frame(body, style="Card.TFrame", padding=16)
        rcard.grid(row=0, column=1, sticky="nsew")
        ttk.Label(rcard, text="生平 / 状态 / 施政 / 能力上限", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 10))

        grid = ttk.Frame(rcard, style="Card.TFrame")
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        def add_row(r: int, label: str, widget_builder) -> None:
            ttk.Label(grid, text=label, style="Card.TLabel", width=12).grid(row=r, column=0, sticky=tk.W, pady=6)
            w = widget_builder(grid)
            w.grid(row=r, column=1, sticky="ew", pady=6, padx=(6, 0))

        add_row(0, "出生年份", lambda p: ttk.Entry(p, textvariable=self.deep_birth_var, width=12))
        def death_row(p: ttk.Frame) -> ttk.Frame:
            fr = ttk.Frame(p, style="Card.TFrame")
            ttk.Entry(fr, textvariable=self.deep_death_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Button(fr, text="延寿至 350 年", command=lambda: self.deep_death_var.set("350")).pack(side=tk.LEFT)
            return fr
        add_row(1, "卒年（延寿）", death_row)

        def loy_row(p: ttk.Frame) -> ttk.Frame:
            fr = ttk.Frame(p, style="Card.TFrame")
            ttk.Entry(fr, textvariable=self.deep_loyalty_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Button(fr, text="满忠诚 100", command=lambda: self.deep_loyalty_var.set("100")).pack(side=tk.LEFT)
            return fr
        add_row(2, "忠诚度", loy_row)

        add_row(
            3,
            "伤病状态",
            lambda p: ttk.Combobox(
                p,
                textvariable=self.deep_illness_var,
                values=[f"{k} - {v}" for k, v in ILLNESS_NAMES.items()],
                state="readonly",
            ),
        )
        add_row(
            4,
            "主义",
            lambda p: ttk.Combobox(
                p,
                textvariable=self.deep_ism_var,
                values=[f"{k} - {v}" for k, v in DOCTRINE_NAMES.items()],
                state="readonly",
            ),
        )
        def policy_builder(p: ttk.Frame) -> ttk.Combobox:
            cb = ttk.Combobox(p, textvariable=self.deep_policy_var, state="readonly")
            self.deep_policy_combo = cb
            return cb
        add_row(5, "自带政策", policy_builder)
        add_row(6, "政策等级 (1-10)", lambda p: ttk.Entry(p, textvariable=self.deep_policy_lv_var, width=12))

        def cap_row(p: ttk.Frame) -> ttk.Frame:
            fr = ttk.Frame(p, style="Card.TFrame")
            ttk.Entry(fr, textvariable=self.deep_cap_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Button(fr, text="突破 120", command=lambda: self.deep_cap_var.set("120")).pack(side=tk.LEFT, padx=(0, 6))
            ttk.Button(fr, text="默认 100", command=lambda: self.deep_cap_var.set("100")).pack(side=tk.LEFT)
            return fr
        add_row(7, "五维能力上限", cap_row)

        footer = ttk.Frame(parent, padding=(16, 6, 16, 12))
        footer.pack(fill=tk.X)
        ttk.Label(
            footer,
            text="提示：修改实时生效（游戏内切换页签或重开详情即刷新），且全部支持底部「恢复全部修改」撤销。",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Button(footer, text="应用武将进阶修改", style="Accent.TButton", command=self.apply_deep_officer).pack(side=tk.RIGHT)

    def _set_formation_mask(self, mask: int) -> None:
        for bit, var in self.formation_vars.items():
            var.set(bool(mask & (1 << bit)))

    def _current_formation_mask(self) -> int:
        mask = 0
        for bit, var in self.formation_vars.items():
            if var.get():
                mask |= 1 << bit
        return mask

    def _ensure_policies_loaded(self) -> None:
        if self.policies or not self.app.mem.is_attached:
            return
        try:
            self.policies = read_policy_catalog(self.app.mem)
        except MemoryErrorWin32:
            self.policies = []
        if self.deep_policy_combo is not None:
            self.deep_policy_combo["values"] = [f"{pid} - {name}" for pid, name in self.policies]

    def _load_deep_into_ui(self, info: OfficerDeepInfo) -> None:
        self.current_deep = info
        self._ensure_policies_loaded()
        self.deep_banner_var.set(
            f"当前：{info.full_name} (#{info.officer_id}) · 五维 {'/'.join(map(str, info.stats))}"
        )
        self._set_formation_mask(info.formations_mask)
        self.deep_birth_var.set(str(info.birth_year))
        self.deep_death_var.set(str(info.death_year))
        self.deep_loyalty_var.set(str(info.loyalty))
        self.deep_illness_var.set(f"{info.illness} - {ILLNESS_NAMES.get(info.illness, '未知')}")
        self.deep_ism_var.set(f"{info.ism} - {DOCTRINE_NAMES.get(info.ism, '未知')}")
        pol_map = dict(self.policies)
        self.deep_policy_var.set(f"{info.policy_id} - {pol_map.get(info.policy_id, f'政策#{info.policy_id}')}")
        self.deep_policy_lv_var.set(str(info.policy_lv))
        self.deep_cap_var.set(str(max(info.stat_caps) if info.stat_caps else 100))

    def read_current_deep_officer(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先点击右上角「连接游戏」。", parent=self.app)
            return
        try:
            if not supported_executable(self.app.mem):
                raise MemoryErrorWin32("武将进阶功能仅支持简体中文传承版 SAN14CE_SC.exe。")
            target = read_live_targets(self.app.mem, ("officer",)).get("officer")
            if target is None:
                raise MemoryErrorWin32("请先在游戏中打开目标武将的详情页。")
            record_addr = target[0] - HERITAGE_STATS_OFFSET
            oid = resolve_officer_id_from_address(self.app.mem, record_addr)
            info = read_officer_deep(self.app.mem, record_addr, oid)
            self._load_deep_into_ui(info)
            self.app.status_var.set(f"已读取 {info.full_name} 的进阶属性（阵型掩码 {hex(info.formations_mask)}）")
            self.app._play_beep(True)
        except Exception as exc:
            self.app.status_var.set(f"读取失败：{exc}")
            self.app._play_beep(False)
            messagebox.showerror("读取失败", str(exc), parent=self.app)

    def load_all_persons(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先点击右上角「连接游戏」。", parent=self.app)
            return
        try:
            self._ensure_policies_loaded()
            self.person_index = list_live_person_index(self.app.mem)
            self._refresh_deep_combo()
            self._refresh_item_target_combo()
            if self.person_index and self.current_deep is None:
                oid, addr, _, _ = self.person_index[0]
                self._load_deep_into_ui(read_officer_deep(self.app.mem, addr, oid))
            self.app.status_var.set(f"已加载剧本中全部 {len(self.person_index)} 名武将")
            self.app._play_beep(True)
        except Exception as exc:
            self.app.status_var.set(f"加载失败：{exc}")
            messagebox.showerror("加载失败", str(exc), parent=self.app)

    def _refresh_deep_combo(self) -> None:
        if self.deep_combo is None:
            return
        q = self.deep_search_var.get().strip().lower()
        items = [p for p in self.person_index if not q or q in p[2].lower() or q == str(p[0])]
        self._filtered_persons = items[:300]
        labels = [f"#{oid:04d} {name}" for oid, _, name, _ in self._filtered_persons]
        self.deep_combo["values"] = labels
        if labels and self.deep_combo_var.get() not in labels:
            self.deep_combo_var.set(labels[0])

    def _on_deep_combo_selected(self, _event: tk.Event) -> None:
        if self.deep_combo is None or not self.app.mem.is_attached:
            return
        idx = self.deep_combo.current()
        if 0 <= idx < len(self._filtered_persons):
            oid, addr, _, _ = self._filtered_persons[idx]
            try:
                self._load_deep_into_ui(read_officer_deep(self.app.mem, addr, oid))
            except Exception as exc:
                self.app.status_var.set(f"读取武将失败：{exc}")

    @staticmethod
    def _leading_int(text: str, default: int) -> int:
        head = text.split("-", 1)[0].strip()
        return parse_int(head, default)

    def apply_deep_officer(self) -> None:
        if self.current_deep is None or not self.app.mem.is_attached:
            messagebox.showwarning("未选择武将", "请先读取当前详情武将或从列表选择一名武将。", parent=self.app)
            return
        try:
            updated = write_officer_deep(
                self.app.mem,
                self.current_deep.address,
                formations_mask=self._current_formation_mask(),
                birth_year=parse_int(self.deep_birth_var.get(), self.current_deep.birth_year),
                death_year=parse_int(self.deep_death_var.get(), self.current_deep.death_year),
                loyalty=parse_int(self.deep_loyalty_var.get(), self.current_deep.loyalty),
                illness=self._leading_int(self.deep_illness_var.get(), self.current_deep.illness),
                ism=self._leading_int(self.deep_ism_var.get(), self.current_deep.ism),
                policy_id=self._leading_int(self.deep_policy_var.get(), self.current_deep.policy_id),
                policy_lv=parse_int(self.deep_policy_lv_var.get(), self.current_deep.policy_lv),
                stat_cap_value=parse_int(self.deep_cap_var.get(), 100),
                label_prefix=self.current_deep.full_name,
            )
            self._load_deep_into_ui(updated)
            self.app.status_var.set(
                f"已应用 {updated.full_name} 的进阶修改（卒年 {updated.death_year} · 忠诚 {updated.loyalty} · 阵型 {hex(updated.formations_mask)}）"
            )
            self.app._play_beep(True)
        except Exception as exc:
            self.app.status_var.set(f"写入进阶属性失败：{exc}")
            self.app._play_beep(False)
            messagebox.showerror("写入失败", str(exc), parent=self.app)

    # ========================================================================
    # Tab: 势力 / 外交 / 名品 (Force Batch, Diplomacy/Foreign, Treasures)
    # ========================================================================

    def build_world_tab(self, parent: ttk.Frame) -> None:
        hdr = ttk.Frame(parent, padding=(16, 12, 16, 6))
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="势力批量 / 外交异族 / 名品管理", style="Section.TLabel").pack(side=tk.LEFT)
        ttk.Button(hdr, text="刷新世界数据", style="Accent.TButton", command=self.refresh_world_all).pack(side=tk.RIGHT)
        ttk.Label(hdr, textvariable=self.world_summary_var, style="Badge.TLabel").pack(side=tk.RIGHT, padx=(0, 10))

        sub = ttk.Notebook(parent)
        sub.pack(fill=tk.BOTH, expand=True, padx=16, pady=(2, 10))
        force_tab = ttk.Frame(sub)
        diplo_tab = ttk.Frame(sub)
        item_tab = ttk.Frame(sub)
        sub.add(force_tab, text="己方势力批量修改")
        sub.add(diplo_tab, text="外交 / 异民族 / 海外诸国")
        sub.add(item_tab, text="名品归属管理")

        self._build_force_batch_subtab(force_tab)
        self._build_diplomacy_subtab(diplo_tab)
        self._build_items_subtab(item_tab)

    def _build_force_batch_subtab(self, parent: ttk.Frame) -> None:
        body = ttk.Frame(parent, padding=14)
        body.pack(fill=tk.BOTH, expand=True)

        # Row 0: Instant Global Player Boosts
        gcard = ttk.Frame(body, style="Card.TFrame", padding=14)
        gcard.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(gcard, text="全局势力快捷", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 14))
        ttk.Button(gcard, text="命令书拉满 (99)", style="Accent.TButton", command=self._quick_max_orders).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(gcard, text="施政等级拉满 (Lv.10)", style="Accent.TButton", command=self._quick_max_admin).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Label(gcard, text="（仅作用于当前玩家操控势力，支持底部撤销）", style="CardMuted.TLabel").pack(side=tk.LEFT)

        # Row 1: Batch Cities
        ccard = ttk.Frame(body, style="Card.TFrame", padding=14)
        ccard.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(ccard, text="己方全都市一键补给（同时清空伤兵与混乱/挑衅）", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 8))
        crow = ttk.Frame(ccard, style="Card.TFrame")
        crow.pack(fill=tk.X)
        for lbl, var in (
            ("资金", self.batch_gold_var),
            ("兵粮", self.batch_food_var),
            ("士兵", self.batch_troops_var),
            ("耐久", self.batch_dur_var),
            ("士气", self.batch_morale_var),
        ):
            ttk.Label(crow, text=lbl, style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(crow, textvariable=var, width=9).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(crow, text="执行全都市补给", style="Accent.TButton", command=self.run_batch_cities).pack(side=tk.RIGHT)

        # Row 2: Batch Areas
        acard = ttk.Frame(body, style="Card.TFrame", padding=14)
        acard.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(acard, text="己方全区域一键满开发 / 满治安", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 8))
        arow = ttk.Frame(acard, style="Card.TFrame")
        arow.pack(fill=tk.X)
        for lbl, var in (
            ("商/农/兵开发 (0-1000)", self.batch_area_dev_var),
            ("治安 (0-100)", self.batch_area_order_var),
            ("建设上限 (留空不改)", self.batch_area_cap_var),
        ):
            ttk.Label(arow, text=lbl, style="CardMuted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
            ttk.Entry(arow, textvariable=var, width=8).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(arow, text="执行全区域开发", style="Accent.TButton", command=self.run_batch_areas).pack(side=tk.RIGHT)

        # Row 3: Batch Officers
        ocard = ttk.Frame(body, style="Card.TFrame", padding=14)
        ocard.pack(fill=tk.X)
        ttk.Label(ocard, text="己方全武将一键强化", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 10))
        orow = ttk.Frame(ocard, style="Card.TFrame")
        orow.pack(fill=tk.X)
        ttk.Button(
            orow,
            text="全武将满忠诚 + 治愈伤病",
            style="Accent.TButton",
            command=lambda: self.run_batch_officers(loyalty=100, cure_illness=True),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            orow,
            text="全武将解锁全陆战阵型",
            command=lambda: self.run_batch_officers(loyalty=None, cure_illness=False, unlock_formations_mask=FORMATION_MASK_LAND),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            orow,
            text="全武将解锁全水陆阵型",
            command=lambda: self.run_batch_officers(loyalty=None, cure_illness=False, unlock_formations_mask=FORMATION_MASK_LAND_WATER),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            orow,
            text="全武将延寿至 350 年",
            command=lambda: self.run_batch_officers(loyalty=None, cure_illness=False, extend_death_year=350),
        ).pack(side=tk.LEFT)

    def _build_diplomacy_subtab(self, parent: ttk.Frame) -> None:
        split = ttk.Frame(parent, padding=14)
        split.pack(fill=tk.BOTH, expand=True)
        split.columnconfigure(0, weight=6)
        split.columnconfigure(1, weight=5)
        split.rowconfigure(1, weight=1)

        # Left: Forces & Tribes
        lhdr = ttk.Frame(split)
        lhdr.grid(row=0, column=0, sticky="ew", padx=(0, 12), pady=(0, 6))
        ttk.Label(lhdr, text="势力与五异族友好度（0~100）", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(lhdr, text="全势力+五异族满友好", style="Accent.TButton", command=lambda: self._all_diplo(True, 100)).pack(side=tk.RIGHT)

        lbox = ttk.Frame(split)
        lbox.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        self.diplo_tree = ttk.Treeview(lbox, columns=("id", "name", "kind", "friend"), show="headings", selectmode="browse")
        for col, text, w, anc in (("id", "ID", 48, tk.CENTER), ("name", "势力 / 异民族", 150, tk.W), ("kind", "类别", 72, tk.CENTER), ("friend", "友好度", 72, tk.CENTER)):
            self.diplo_tree.heading(col, text=text)
            self.diplo_tree.column(col, width=w, anchor=anc)
        lscr = ttk.Scrollbar(lbox, orient=tk.VERTICAL, command=self.diplo_tree.yview)
        self.diplo_tree.configure(yscrollcommand=lscr.set)
        lscr.pack(side=tk.RIGHT, fill=tk.Y)
        self.diplo_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        lctrl = ttk.Frame(split, padding=(0, 8, 12, 0))
        lctrl.grid(row=2, column=0, sticky="ew")
        ttk.Label(lctrl, text="友好度", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(lctrl, textvariable=self.diplo_val_var, width=6).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(lctrl, text="应用选中", command=self._apply_selected_diplo).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lctrl, text="设为 100", command=lambda: self._apply_selected_diplo(100)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lctrl, text="设为 0", command=lambda: self._apply_selected_diplo(0)).pack(side=tk.LEFT)

        # Right: 4 Foreign Nations
        rhdr = ttk.Frame(split)
        rhdr.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Label(rhdr, text="海外诸国（天竺/贵霜/安息/大秦）", style="Group.TLabel").pack(side=tk.LEFT)
        ttk.Button(rhdr, text="四国一键满友好满通商", style="Accent.TButton", command=self._max_all_foreign).pack(side=tk.RIGHT)

        rbox = ttk.Frame(split)
        rbox.grid(row=1, column=1, sticky="nsew")
        self.foreign_tree = ttk.Treeview(rbox, columns=("id", "name", "friend", "lv"), show="headings", selectmode="browse")
        for col, text, w, anc in (("id", "ID", 44, tk.CENTER), ("name", "国家", 110, tk.W), ("friend", "友好度", 80, tk.CENTER), ("lv", "通商等级", 80, tk.CENTER)):
            self.foreign_tree.heading(col, text=text)
            self.foreign_tree.column(col, width=w, anchor=anc)
        self.foreign_tree.pack(fill=tk.BOTH, expand=True)

        rctrl = ttk.Frame(split, padding=(0, 8, 0, 0))
        rctrl.grid(row=2, column=1, sticky="ew")
        ttk.Label(rctrl, text="友好", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(rctrl, textvariable=self.foreign_val_var, width=6).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(rctrl, text="等级(0-4)", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(rctrl, textvariable=self.foreign_lv_var, width=4).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(rctrl, text="应用选中诸国", command=self._apply_selected_foreign).pack(side=tk.LEFT)

    def _build_items_subtab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, padding=(14, 12, 14, 6))
        top.pack(fill=tk.X)
        ttk.Label(top, text="搜索名品", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        isearch = ttk.Entry(top, textvariable=self.item_search_var, width=16)
        isearch.pack(side=tk.LEFT, padx=(0, 10))
        isearch.bind("<KeyRelease>", lambda _e: self._populate_item_tree())

        icb = ttk.Combobox(top, textvariable=self.item_filter_var, values=("全部名品", "武将持有", "未发现/无主"), state="readonly", width=11)
        icb.pack(side=tk.LEFT, padx=(0, 16))
        icb.bind("<<ComboboxSelected>>", lambda _e: self._populate_item_tree())

        ttk.Label(top, text="授予目标武将", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        tsearch = ttk.Entry(top, textvariable=self.item_target_search_var, width=10)
        tsearch.pack(side=tk.LEFT, padx=(0, 6))
        tsearch.bind("<KeyRelease>", lambda _e: self._refresh_item_target_combo())

        self.item_target_combo = ttk.Combobox(top, textvariable=self.item_target_combo_var, state="readonly", width=22)
        self.item_target_combo.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(top, text="设为当前详情武将", command=self._pick_open_officer_for_item).pack(side=tk.LEFT)

        box = ttk.Frame(parent, padding=(14, 2, 14, 6))
        box.pack(fill=tk.BOTH, expand=True)
        self.item_tree = ttk.Treeview(box, columns=("id", "name", "owner", "status"), show="headings", selectmode="browse")
        for col, text, w, anc in (
            ("id", "ID", 60, tk.CENTER),
            ("name", "名品名称", 220, tk.W),
            ("owner", "当前持有者", 160, tk.W),
            ("status", "状态", 110, tk.CENTER),
        ):
            self.item_tree.heading(col, text=text)
            self.item_tree.column(col, width=w, anchor=anc)
        scr = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=scr.set)
        scr.pack(side=tk.RIGHT, fill=tk.Y)
        self.item_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.item_tree.bind("<Double-1>", lambda _e: self._grant_selected_item())

        ftr = ttk.Frame(parent, padding=(14, 4, 14, 12))
        ftr.pack(fill=tk.X)
        ttk.Label(ftr, text="提示：选中名品（或双击）即可直接授予右侧选中的武将。", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(ftr, text="将选中名品授予目标武将", style="Accent.TButton", command=self._grant_selected_item).pack(side=tk.RIGHT)

    # ---------------- World Data Refresh & Actions ----------------

    def refresh_world_all(self) -> None:
        if not self.app.mem.is_attached:
            messagebox.showwarning("未连接游戏", "请先点击右上角「连接游戏」，并进入剧本。", parent=self.app)
            return
        try:
            ctx = read_world_context(self.app.mem)
            self.world_ctx = ctx
            self.world_summary_var.set(
                f"当前势力：{ctx.player_ruler_name} · 命令书 {ctx.orders} · 施政 Lv.{ctx.doctrine_level} · 都市 {ctx.player_city_count} · 武将 {ctx.player_officer_count}"
            )
            forces, tribes, foreigns = list_diplomacy(self.app.mem)
            self.diplo_entries = list(forces) + list(tribes)
            self.foreign_entries = list(foreigns)
            self._populate_diplomacy_trees()
            self.all_items = list_items(self.app.mem)
            self._populate_item_tree()
            if not self.person_index:
                self.person_index = list_live_person_index(self.app.mem)
                self._refresh_deep_combo()
            self._refresh_item_target_combo(prefer_oid=ctx.player_ruler_id)
            self.app.status_var.set(
                f"已刷新世界数据：{ctx.player_ruler_name}军 · 外交 {len(self.diplo_entries)} 项 · 名品 {len(self.all_items)} 件"
            )
            self.app._play_beep(True)
        except Exception as exc:
            self.app.status_var.set(f"刷新世界数据失败：{exc}")
            self.app._play_beep(False)
            messagebox.showerror("刷新失败", str(exc), parent=self.app)

    def _quick_max_orders(self) -> None:
        try:
            resolved = resolve_live_global_field(self.app.mem, "inf_orders")
            if resolved is None:
                raise MemoryErrorWin32("无法定位命令书地址。")
            addr, dtype, val, _, _ = resolved
            self.app.mem.write_value(addr, dtype, val, record_patch=True, label="全局.命令书")
            self.refresh_world_all()
            self.app.status_var.set(f"已将当前势力命令书拉满至 {val}")
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _quick_max_admin(self) -> None:
        try:
            resolved = resolve_live_global_field(self.app.mem, "max_admin")
            if resolved is None:
                raise MemoryErrorWin32("无法定位施政经验地址。")
            addr, dtype, val, _, _ = resolved
            self.app.mem.write_value(addr, dtype, val, record_patch=True, label="全局.施政等级")
            self.refresh_world_all()
            self.app.status_var.set("已将当前势力施政等级拉满至 Lv.10")
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def run_batch_cities(self) -> None:
        try:
            names = batch_supply_player_cities(
                self.app.mem,
                gold=parse_int(self.batch_gold_var.get(), 999999),
                food=parse_int(self.batch_food_var.get(), 999999),
                troops=parse_int(self.batch_troops_var.get(), 200000),
                durability=parse_int(self.batch_dur_var.get(), 60000),
                morale=parse_int(self.batch_morale_var.get(), 200),
            )
            self.app.status_var.set(f"已补给己方全部 {len(names)} 座都市：{'、'.join(names[:8])}{'...' if len(names) > 8 else ''}")
            self.app._play_beep(True)
        except Exception as exc:
            self.app._play_beep(False)
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def run_batch_areas(self) -> None:
        try:
            cap_txt = self.batch_area_cap_var.get().strip()
            cap_val = parse_int(cap_txt, 10) if cap_txt else None
            cnt = batch_max_player_areas(
                self.app.mem,
                dev=parse_int(self.batch_area_dev_var.get(), 1000),
                order=parse_int(self.batch_area_order_var.get(), 100),
                build_cap=cap_val,
            )
            self.app.status_var.set(f"已更新己方全部 {cnt} 个区域的开发与治安")
            self.app._play_beep(True)
        except Exception as exc:
            self.app._play_beep(False)
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def run_batch_officers(self, **kwargs: Any) -> None:
        try:
            cnt = batch_boost_player_officers(self.app.mem, **kwargs)
            self.app.status_var.set(f"已批量强化己方全部 {cnt} 名在职武将")
            self.app._play_beep(True)
        except Exception as exc:
            self.app._play_beep(False)
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _populate_diplomacy_trees(self) -> None:
        if self.diplo_tree is not None:
            self.diplo_tree.delete(*self.diplo_tree.get_children())
            for entry in self.diplo_entries:
                self.diplo_tree.insert(
                    "",
                    tk.END,
                    iid=str(entry.force_id),
                    values=(entry.force_id, entry.name, "异民族" if entry.is_tribe else "势力", entry.friendliness),
                )
        if self.foreign_tree is not None:
            self.foreign_tree.delete(*self.foreign_tree.get_children())
            for entry in self.foreign_entries:
                self.foreign_tree.insert(
                    "",
                    tk.END,
                    iid=str(entry.foreign_id),
                    values=(entry.foreign_id, entry.name, entry.friendliness, f"Lv.{entry.trade_level}"),
                )

    def _apply_selected_diplo(self, override: Optional[int] = None) -> None:
        if self.diplo_tree is None:
            return
        sel = self.diplo_tree.selection()
        if not sel:
            self.app.status_var.set("请先在列表中选中一个势力或异民族。")
            return
        fid = int(sel[0])
        val = override if override is not None else parse_int(self.diplo_val_var.get(), 100)
        try:
            written = set_force_friendliness(self.app.mem, fid, val)
            forces, tribes, foreigns = list_diplomacy(self.app.mem)
            self.diplo_entries = list(forces) + list(tribes)
            self.foreign_entries = list(foreigns)
            self._populate_diplomacy_trees()
            self.diplo_tree.selection_set(str(fid))
            self.app.status_var.set(f"已将与势力 #{fid} 的双向友好度设为 {written}")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _all_diplo(self, include_tribes: bool, value: int) -> None:
        try:
            cnt = set_all_diplomacy_max(self.app.mem, include_tribes=include_tribes, value=value)
            forces, tribes, foreigns = list_diplomacy(self.app.mem)
            self.diplo_entries = list(forces) + list(tribes)
            self.foreign_entries = list(foreigns)
            self._populate_diplomacy_trees()
            self.app.status_var.set(f"已将全部 {cnt} 个势力/异民族的双向友好度设为 {value}")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _apply_selected_foreign(self) -> None:
        if self.foreign_tree is None:
            return
        sel = self.foreign_tree.selection()
        if not sel:
            self.app.status_var.set("请先在右侧选中一个海外国家。")
            return
        fn_id = int(sel[0])
        try:
            res = set_foreign_relation(
                self.app.mem,
                fn_id,
                friendliness=parse_int(self.foreign_val_var.get(), 1000),
                trade_level=parse_int(self.foreign_lv_var.get(), 4),
            )
            _, _, self.foreign_entries = list_diplomacy(self.app.mem)
            self._populate_diplomacy_trees()
            self.foreign_tree.selection_set(str(fn_id))
            self.app.status_var.set(f"已更新 {res.name}：友好度 {res.friendliness} · 通商等级 Lv.{res.trade_level}")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _max_all_foreign(self) -> None:
        try:
            for fn_id in (1, 2, 3, 4):
                set_foreign_relation(self.app.mem, fn_id, friendliness=1000, trade_level=4)
            _, _, self.foreign_entries = list_diplomacy(self.app.mem)
            self._populate_diplomacy_trees()
            self.app.status_var.set("已将海外四国（天竺/贵霜/安息/大秦）全部设为满友好 (1000) 且满通商 (Lv.4)")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc), parent=self.app)

    def _populate_item_tree(self) -> None:
        if self.item_tree is None:
            return
        q = self.item_search_var.get().strip().lower()
        mode = self.item_filter_var.get()
        self.filtered_items = []
        self.item_tree.delete(*self.item_tree.get_children())
        for item in self.all_items:
            if q and q not in item.name.lower() and q not in item.owner_name.lower() and q != str(item.item_id):
                continue
            if mode == "武将持有" and item.owner_id == 0:
                continue
            if mode == "未发现/无主" and item.owner_id != 0:
                continue
            self.filtered_items.append(item)
            self.item_tree.insert(
                "",
                tk.END,
                iid=str(item.item_id),
                values=(item.item_id, item.name, item.owner_name, item.status_label),
            )

    def _refresh_item_target_combo(self, prefer_oid: Optional[int] = None) -> None:
        if self.item_target_combo is None:
            return
        q = self.item_target_search_var.get().strip().lower()
        items = [p for p in self.person_index if not q or q in p[2].lower() or q == str(p[0])]
        self._item_target_persons = items[:300]
        labels = [f"#{oid:04d} {name}" for oid, _, name, _ in self._item_target_persons]
        self.item_target_combo["values"] = labels
        if prefer_oid is not None:
            for oid, _, name, _ in self._item_target_persons:
                if oid == prefer_oid:
                    self.item_target_combo_var.set(f"#{oid:04d} {name}")
                    return
        if labels and self.item_target_combo_var.get() not in labels:
            self.item_target_combo_var.set(labels[0])

    def _pick_open_officer_for_item(self) -> None:
        try:
            target = read_live_targets(self.app.mem, ("officer",)).get("officer")
            if target is None:
                raise MemoryErrorWin32("请先在游戏中打开目标武将详情页。")
            record_addr = target[0] - HERITAGE_STATS_OFFSET
            oid = resolve_officer_id_from_address(self.app.mem, record_addr)
            info = read_officer_deep(self.app.mem, record_addr, oid)
            if not self.person_index:
                self.person_index = list_live_person_index(self.app.mem)
            self.item_target_search_var.set("")
            self._refresh_item_target_combo(prefer_oid=oid)
            self.item_target_combo_var.set(f"#{oid:04d} {info.full_name}")
            self.app.status_var.set(f"已将名品授予目标设为当前武将：{info.full_name}")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("无法识别当前武将", str(exc), parent=self.app)

    def _grant_selected_item(self) -> None:
        if self.item_tree is None:
            return
        sel = self.item_tree.selection()
        if not sel:
            self.app.status_var.set("请先选中一件名品。")
            return
        target_text = self.item_target_combo_var.get().strip()
        if not target_text.startswith("#"):
            messagebox.showwarning("未选择目标武将", "请先刷新世界数据并选择要授予的武将。", parent=self.app)
            return
        oid = parse_int(target_text.split()[0][1:], 0)
        oname = target_text.split(" ", 1)[1] if " " in target_text else str(oid)
        iid = int(sel[0])
        entry = next((x for x in self.all_items if x.item_id == iid), None)
        if entry is None:
            return
        try:
            grant_item_to_officer(self.app.mem, entry.address, oid, item_name=entry.name, officer_name=oname)
            self.all_items = list_items(self.app.mem)
            self._populate_item_tree()
            if self.item_tree.exists(str(iid)):
                self.item_tree.selection_set(str(iid))
            self.app.status_var.set(f"已将名品「{entry.name}」授予 {oname}（游戏内切换详情刷新）")
            self.app._play_beep(True)
        except Exception as exc:
            messagebox.showerror("授予失败", str(exc), parent=self.app)
