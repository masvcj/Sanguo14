"""Unrestricted Custom Trait Workshop Dialog (无限制自创个性工坊) for SAN14CE.

Supports BOTH:
1. Pre-scenario global custom traits (editpersonalityexceSC.s14 + live CEditPersonalityData/CPersonalityData
   on Title Menu) — directly selectable when editing Historical Officers & Custom Officers before entering a scenario!
2. In-scenario live memory injection & one-click equip to the currently selected officer.
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from custom_trait_editor import (
    AURA_COND1_IDS,
    COLOR_OPTIONS,
    CUSTOM_TRAIT_MAX_ID,
    CUSTOM_TRAIT_MIN_ID,
    CUSTOM_TRAIT_TEMPLATES,
    EDIT_FILE_MAX_VAL,
    EDIT_FILE_MIN_VAL,
    EDIT_PERSONALITY_FILENAME,
    EFFECT_BY_ID,
    TRAIT_COND1_OPTIONS,
    TRAIT_COND2_OPTIONS,
    TRAIT_EFFECT_DEFS,
    CustomTraitDef,
    auto_generate_description,
    clear_live_custom_trait,
    clear_live_edit_personality,
    discover_edit_personality_file,
    expand_dual_self_aura_traits,
    find_next_free_custom_slot,
    list_live_custom_traits,
    load_edit_personality_file,
    normalize_custom_trait,
    save_custom_traits_file,
    save_edit_personality_file,
    write_live_custom_trait,
    write_live_edit_personality,
)
from memory_engine import MemoryErrorWin32
from profile_manager import parse_int


def _effect_label(eid: int) -> str:
    if eid == 0:
        return "0 - （无第二效果）"
    edef = EFFECT_BY_ID.get(eid)
    if edef is None:
        return f"{eid} - 自定义效果 #{eid}"
    return f"{edef.id} - [{edef.category}] {edef.name}  【正常值: {edef.normal_text}】"


def _cond_label(options: Tuple[Tuple[int, str], ...], cid: int) -> str:
    for k, desc in options:
        if k == cid:
            return f"{k} - {desc}"
    return f"{cid} - 条件 #{cid}"


def _color_label(col: int) -> str:
    for k, name in COLOR_OPTIONS:
        if k == col:
            return f"{k} - {name}"
    return f"{col} - 特殊"


def _leading_int(text: str, default: int = 0) -> int:
    head = text.split("-", 1)[0].strip()
    return parse_int(head, default)


class CustomTraitWorkshopDialog(ttk.Frame):
    """Workshop panel/dialog for creating unrestricted custom traits usable before & during scenarios.

    Can be embedded directly inside a Notebook tab (`parent` is a `ttk.Frame`) or opened standalone
    when `parent` is omitted (creates a `tk.Toplevel` wrapper automatically).
    """

    def __init__(
        self,
        controller: Any,
        *,
        on_changed: Callable[[Optional[int], bool], None],
        parent: Optional[tk.Widget] = None,
    ) -> None:
        self.controller = controller
        self.app = controller.app
        self.on_changed = on_changed
        self._toplevel: Optional[tk.Toplevel] = None
        if parent is None:
            top = tk.Toplevel(controller.app)
            top.title("无限制自创个性工坊（支持进剧本前给史实/自创武将选用）")
            top.geometry("1140x730")
            top.minsize(1040, 660)
            top.configure(bg=self.app.colors["bg"])
            top.transient(self.app)
            self._toplevel = top
            super().__init__(top)
            self.pack(fill=tk.BOTH, expand=True)
        else:
            super().__init__(parent)
            self.pack(fill=tk.BOTH, expand=True)

        self.edit_file_path: Optional[Path] = discover_edit_personality_file()
        self.edit_file_traits: Dict[int, CustomTraitDef] = {}
        self.live_traits: Dict[int, CustomTraitDef] = {}
        self.effect1_labels: List[str] = [_effect_label(e.id) for e in TRAIT_EFFECT_DEFS]
        self.effect2_labels: List[str] = [_effect_label(0)] + list(self.effect1_labels)

        self.edit_file_var = tk.StringVar(
            value=str(self.edit_file_path) if self.edit_file_path else f"未自动找到 {EDIT_PERSONALITY_FILENAME}（可点击右侧手动指定）"
        )
        self.slot_var = tk.StringVar(value=str(CUSTOM_TRAIT_MIN_ID))
        self.name_var = tk.StringVar(value="战神无双")
        self.color_var = tk.StringVar(value=_color_label(3))
        self.eff1_filter_var = tk.StringVar(value="")
        self.eff1_var = tk.StringVar(value=_effect_label(19))
        self.eff1_val_var = tk.StringVar(value="30")
        self.eff1_norm_var = tk.StringVar(value="")
        self.eff1_hint_var = tk.StringVar(value="")
        self.eff2_filter_var = tk.StringVar(value="")
        self.eff2_var = tk.StringVar(value=_effect_label(51))
        self.eff2_val_var = tk.StringVar(value="1")
        self.eff2_norm_var = tk.StringVar(value="")
        self.eff2_hint_var = tk.StringVar(value="")
        self.cond1_var = tk.StringVar(value=_cond_label(TRAIT_COND1_OPTIONS, 0))
        self.cond1_param_var = tk.StringVar(value="0")
        self.cond2_var = tk.StringVar(value=_cond_label(TRAIT_COND2_OPTIONS, 0))
        self.desc_var = tk.StringVar(value="全地形全兵种常驻：自部队全能力大幅上升(+30)，无视敌方控制区域")

        self._build_ui()
        self._reload_all_sources(silent=True)
        self._pick_initial_slot()
        self._update_hints()

        if self._toplevel is not None:
            self._toplevel.update_idletasks()
            x = self.app.winfo_rootx() + max(20, (self.app.winfo_width() - self._toplevel.winfo_width()) // 2)
            y = self.app.winfo_rooty() + max(20, (self.app.winfo_height() - self._toplevel.winfo_height()) // 2)
            self._toplevel.geometry(f"+{x}+{y}")

    def destroy(self) -> None:
        if self._toplevel is not None:
            top = self._toplevel
            self._toplevel = None
            top.destroy()
        else:
            super().destroy()

    def _build_ui(self) -> None:
        st = ttk.Style(self)
        st.configure("Mini.TEntry", padding=(6, 2))
        st.configure("Mini.TCombobox", padding=3)

        # Compact single top bar: Title + Global edit file path + Change path button
        fbar = ttk.Frame(self, style="Card.TFrame", padding=(12, 6))
        fbar.pack(fill=tk.X, padx=16, pady=(6, 4))
        ttk.Label(fbar, text="无限制自创个性工坊", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(fbar, text="全局文件:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(fbar, textvariable=self.edit_file_var, style="CardMuted.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(fbar, text="更换路径...", style="Mini.TButton", command=self._choose_edit_file).pack(side=tk.RIGHT)

        split = ttk.Frame(self, padding=(16, 2, 16, 8))
        split.pack(fill=tk.BOTH, expand=True)
        split.columnconfigure(0, weight=4)
        split.columnconfigure(1, weight=6)
        split.rowconfigure(0, weight=1)

        # ---- Left pane: Custom trait list (256..355) ----
        left = ttk.Frame(split, style="Card.TFrame", padding=(12, 8))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ttk.Label(left, text="自创个性槽位列表（256~355，共 100 槽）", style="Group.TLabel").pack(anchor=tk.W, pady=(0, 6))

        # Pack bottom buttons in left pane first so they are never clipped
        lbtns3 = ttk.Frame(left, style="Card.TFrame")
        lbtns3.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            lbtns3,
            text="一键同步全部到当前运行游戏内存",
            command=self._sync_all_to_game,
        ).pack(fill=tk.X)

        lbtns2 = ttk.Frame(left, style="Card.TFrame")
        lbtns2.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 4))
        ttk.Button(
            lbtns2,
            text="一键同步全部到全局文件（进剧本前可选）",
            command=self._sync_all_to_global_file,
        ).pack(fill=tk.X)

        lbtns1 = ttk.Frame(left, style="Card.TFrame")
        lbtns1.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 4))
        ttk.Button(lbtns1, text="新建空槽位", command=self._new_free_slot).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lbtns1, text="刷新读取", command=lambda: self._reload_all_sources(silent=False)).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(lbtns1, text="删除/清空", command=self._delete_selected).pack(side=tk.RIGHT)

        tbox = ttk.Frame(left, style="Card.TFrame")
        tbox.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tbox, columns=("id", "name", "color", "state"), show="headings", selectmode="browse")
        for col, text, w, anc in (
            ("id", "槽位", 50, tk.CENTER),
            ("name", "名称", 90, tk.W),
            ("color", "品级", 50, tk.CENTER),
            ("state", "生效位置", 104, tk.CENTER),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor=anc)
        self.tree.tag_configure("gold", foreground="#ffb34a")
        self.tree.tag_configure("blue", foreground="#70b8ff")
        self.tree.tag_configure("red", foreground="#ff6b6b")
        self.tree.tag_configure("custom", foreground="#d98aff")
        scr = ttk.Scrollbar(tbox, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scr.set)
        scr.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ---- Right pane: Unrestricted Editor ----
        right = ttk.Frame(split, style="Card.TFrame", padding=(14, 8))
        right.grid(row=0, column=1, sticky="nsew")

        # Pack bottom action buttons FIRST with side=tk.BOTTOM so they always get full height and are never clipped
        act = ttk.Frame(right, style="Card.TFrame", padding=(0, 4, 0, 2))
        act.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(
            act,
            text="写入全局（进剧本前给史实/自创武将可选）",
            style="Accent.TButton",
            command=self._commit_to_global_file,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            act,
            text="写入剧本并装备到当前武将",
            command=lambda: self._commit_to_game(equip=True),
        ).pack(side=tk.RIGHT)
        ttk.Button(
            act,
            text="仅写剧本内存",
            command=lambda: self._commit_to_game(equip=False),
        ).pack(side=tk.RIGHT, padx=(0, 6))

        # Single-row templates bar
        trow = ttk.Frame(right, style="Card.TFrame")
        trow.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(trow, text="神技模板:", style="Group.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        for tmpl in CUSTOM_TRAIT_TEMPLATES:
            ttk.Button(
                trow,
                text=tmpl.name,
                style="Mini.TButton",
                command=lambda t=tmpl: self._load_template(t),
            ).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Separator(right).pack(fill=tk.X, pady=(0, 6))

        # Basic row
        brow = ttk.Frame(right, style="Card.TFrame")
        brow.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(brow, text="槽位 ID (256-355)", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(brow, textvariable=self.slot_var, width=6, justify=tk.CENTER, style="Mini.TEntry").pack(
            side=tk.LEFT, padx=(6, 14)
        )
        ttk.Label(brow, text="名称 (≤5字)", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(brow, textvariable=self.name_var, width=12, style="Mini.TEntry").pack(side=tk.LEFT, padx=(6, 14))
        ttk.Label(brow, text="品级", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            brow,
            textvariable=self.color_var,
            values=[_color_label(k) for k, _ in COLOR_OPTIONS],
            state="readonly",
            width=16,
            style="Mini.TCombobox",
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Effect 1
        e1_hdr = ttk.Frame(right, style="Card.TFrame")
        e1_hdr.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(e1_hdr, text="效果 1（必选）", style="Group.TLabel").pack(side=tk.LEFT)
        e1_filter = ttk.Entry(e1_hdr, textvariable=self.eff1_filter_var, width=14, style="Mini.TEntry")
        e1_filter.pack(side=tk.RIGHT)
        e1_filter.bind("<KeyRelease>", lambda _e: self._filter_effect_combo(1))
        ttk.Label(e1_hdr, text="筛选词条", style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(8, 4))

        self.eff1_combo = ttk.Combobox(right, textvariable=self.eff1_var, values=self.effect1_labels, style="Mini.TCombobox")
        self.eff1_combo.pack(fill=tk.X, pady=(0, 3))
        self.eff1_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_effect_selected(1))
        self.eff1_combo.bind("<KeyRelease>", lambda _e: self._update_hints())

        e1_val_row = ttk.Frame(right, style="Card.TFrame")
        e1_val_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(e1_val_row, text="设定数值", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(e1_val_row, textvariable=self.eff1_val_var, width=7, justify=tk.CENTER, style="Mini.TEntry").pack(
            side=tk.LEFT, padx=(6, 6)
        )
        ttk.Button(e1_val_row, text="填正常值", style="Mini.TButton", command=lambda: self._fill_normal_value(1)).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Label(e1_val_row, textvariable=self.eff1_norm_var, style="Badge.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(e1_val_row, textvariable=self.eff1_hint_var, style="CardMuted.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Effect 2
        e2_hdr = ttk.Frame(right, style="Card.TFrame")
        e2_hdr.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(e2_hdr, text="效果 2（可选 · 任意第二神技自由组合）", style="Group.TLabel").pack(side=tk.LEFT)
        e2_filter = ttk.Entry(e2_hdr, textvariable=self.eff2_filter_var, width=14, style="Mini.TEntry")
        e2_filter.pack(side=tk.RIGHT)
        e2_filter.bind("<KeyRelease>", lambda _e: self._filter_effect_combo(2))
        ttk.Label(e2_hdr, text="筛选词条", style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(8, 4))

        self.eff2_combo = ttk.Combobox(right, textvariable=self.eff2_var, values=self.effect2_labels, style="Mini.TCombobox")
        self.eff2_combo.pack(fill=tk.X, pady=(0, 3))
        self.eff2_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_effect_selected(2))
        self.eff2_combo.bind("<KeyRelease>", lambda _e: self._update_hints())

        e2_val_row = ttk.Frame(right, style="Card.TFrame")
        e2_val_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(e2_val_row, text="设定数值", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(e2_val_row, textvariable=self.eff2_val_var, width=7, justify=tk.CENTER, style="Mini.TEntry").pack(
            side=tk.LEFT, padx=(6, 6)
        )
        ttk.Button(e2_val_row, text="填正常值", style="Mini.TButton", command=lambda: self._fill_normal_value(2)).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Label(e2_val_row, textvariable=self.eff2_norm_var, style="Badge.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(e2_val_row, textvariable=self.eff2_hint_var, style="CardMuted.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Condition 1 on its own full-width row + Aura range on the right
        c1_row = ttk.Frame(right, style="Card.TFrame")
        c1_row.pack(fill=tk.X, pady=(2, 3))
        ttk.Label(c1_row, text="触发条件 1", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Entry(c1_row, textvariable=self.cond1_param_var, width=4, justify=tk.CENTER, style="Mini.TEntry").pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Label(c1_row, text="光环格", style="CardMuted.TLabel").pack(side=tk.RIGHT, padx=(8, 0))
        c1_cb = ttk.Combobox(
            c1_row,
            textvariable=self.cond1_var,
            values=[_cond_label(TRAIT_COND1_OPTIONS, k) for k, _ in TRAIT_COND1_OPTIONS],
            state="readonly",
            style="Mini.TCombobox",
        )
        c1_cb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        c1_cb.bind("<<ComboboxSelected>>", self._on_cond1_changed)

        # Condition 2 on its own full-width row
        c2_row = ttk.Frame(right, style="Card.TFrame")
        c2_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(c2_row, text="附加限制 2", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            c2_row,
            textvariable=self.cond2_var,
            values=[_cond_label(TRAIT_COND2_OPTIONS, k) for k, _ in TRAIT_COND2_OPTIONS],
            state="readonly",
            style="Mini.TCombobox",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # Description
        dhdr = ttk.Frame(right, style="Card.TFrame")
        dhdr.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(dhdr, text="说明文（≤50字，实时注入剧本时显示；全局文件由游戏自动生成说明）", style="Card.TLabel").pack(side=tk.LEFT)
        ttk.Button(dhdr, text="自动生成说明", style="Mini.TButton", command=self._auto_fill_desc).pack(side=tk.RIGHT)
        ttk.Entry(right, textvariable=self.desc_var, style="Mini.TEntry").pack(fill=tk.X, pady=(0, 4))

        ttk.Label(
            right,
            text="说明：写入「全局文件」后无需进剧本，主菜单「编辑新武将 / 编辑史实武将」即可直接选用（开局加载范围 -100~+2000，已远超官方上限）；若需 >2000（如动员 +20000），进剧本后再点「写入剧本」即可。",
            style="CardMuted.TLabel",
            wraplength=610,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 4))

    def _choose_edit_file(self) -> None:
        initial = str(self.edit_file_path.parent) if self.edit_file_path else str(Path.home())
        path = filedialog.asksaveasfilename(
            title=f"选择或新建 {EDIT_PERSONALITY_FILENAME}",
            initialdir=initial,
            initialfile=EDIT_PERSONALITY_FILENAME,
            defaultextension=".s14",
            filetypes=[("三国志14 自创个性文件", "*.s14"), ("所有文件", "*.*")],
            parent=self._toplevel or self.app,
        )
        if path:
            self.edit_file_path = Path(path)
            self.edit_file_var.set(str(self.edit_file_path))
            self._reload_all_sources(silent=True)

    def _filter_effect_combo(self, which: int) -> None:
        q = (self.eff1_filter_var if which == 1 else self.eff2_filter_var).get().strip().lower()
        base = self.effect1_labels if which == 1 else self.effect2_labels
        filtered = [lbl for lbl in base if not q or q in lbl.lower()]
        cb = self.eff1_combo if which == 1 else self.eff2_combo
        cb["values"] = filtered or base

    def _on_effect_selected(self, which: int) -> None:
        var = self.eff1_var if which == 1 else self.eff2_var
        val_var = self.eff1_val_var if which == 1 else self.eff2_val_var
        eid = _leading_int(var.get(), 0)
        edef = EFFECT_BY_ID.get(eid)
        if edef is not None:
            val_var.set(str(edef.default_val))
        elif eid == 0:
            val_var.set("0")
        self._update_hints()

    def _on_cond1_changed(self, _event: tk.Event) -> None:
        c1 = _leading_int(self.cond1_var.get(), 0)
        if c1 in AURA_COND1_IDS:
            if parse_int(self.cond1_param_var.get(), 0) == 0:
                self.cond1_param_var.set("3")
        else:
            self.cond1_param_var.set("0")

    def _fill_normal_value(self, which: int) -> None:
        var = self.eff1_var if which == 1 else self.eff2_var
        val_var = self.eff1_val_var if which == 1 else self.eff2_val_var
        eid = _leading_int(var.get(), 0)
        edef = EFFECT_BY_ID.get(eid)
        if edef is not None:
            val_var.set(str(edef.normal_val))
        elif eid == 0:
            val_var.set("0")

    def _update_hints(self) -> None:
        e1 = EFFECT_BY_ID.get(_leading_int(self.eff1_var.get(), 0))
        if e1 is not None:
            self.eff1_norm_var.set(f"正常值: {e1.normal_text}")
            self.eff1_hint_var.set(f"突破建议: {e1.hint}")
        else:
            self.eff1_norm_var.set("正常值: 自定义")
            self.eff1_hint_var.set("提示：可输入任意引擎效果 ID (1~126)")

        e2_id = _leading_int(self.eff2_var.get(), 0)
        e2 = EFFECT_BY_ID.get(e2_id)
        if e2 is not None:
            self.eff2_norm_var.set(f"正常值: {e2.normal_text}")
            self.eff2_hint_var.set(f"突破建议: {e2.hint}")
        elif e2_id == 0:
            self.eff2_norm_var.set("未启用")
            self.eff2_hint_var.set("提示：可任意混搭第二条神技")
        else:
            self.eff2_norm_var.set("正常值: 自定义")
            self.eff2_hint_var.set("提示：自定义效果 ID")

    def _collect_form(self) -> CustomTraitDef:
        return CustomTraitDef(
            slot_id=parse_int(self.slot_var.get(), CUSTOM_TRAIT_MIN_ID),
            name=self.name_var.get().strip()[:5],
            description=self.desc_var.get().strip()[:50],
            color=_leading_int(self.color_var.get(), 3),
            eff1_id=_leading_int(self.eff1_var.get(), 0),
            eff1_val=parse_int(self.eff1_val_var.get(), 0),
            eff2_id=_leading_int(self.eff2_var.get(), 0),
            eff2_val=parse_int(self.eff2_val_var.get(), 0),
            cond1=_leading_int(self.cond1_var.get(), 0),
            cond2=_leading_int(self.cond2_var.get(), 0),
            cond1_param=parse_int(self.cond1_param_var.get(), 0),
        )

    def _load_into_form(self, trait: CustomTraitDef, *, keep_slot: bool = False) -> None:
        if not keep_slot:
            self.slot_var.set(str(trait.slot_id))
        self.name_var.set(trait.name)
        self.color_var.set(_color_label(trait.color))
        self.eff1_var.set(_effect_label(trait.eff1_id))
        self.eff1_val_var.set(str(trait.eff1_val))
        self.eff2_var.set(_effect_label(trait.eff2_id))
        self.eff2_val_var.set(str(trait.eff2_val))
        self.cond1_var.set(_cond_label(TRAIT_COND1_OPTIONS, trait.cond1))
        self.cond1_param_var.set(str(trait.cond1_param))
        self.cond2_var.set(_cond_label(TRAIT_COND2_OPTIONS, trait.cond2))
        self.desc_var.set(trait.description)
        self._update_hints()

    def _load_template(self, tmpl: CustomTraitDef) -> None:
        self._load_into_form(tmpl, keep_slot=True)

    def _auto_fill_desc(self) -> None:
        trait = self._collect_form()
        self.desc_var.set(auto_generate_description(trait))

    def _merged_traits(self) -> Dict[int, CustomTraitDef]:
        merged: Dict[int, CustomTraitDef] = dict(self.live_traits)
        merged.update(self.edit_file_traits)
        merged.update(self.controller.custom_traits)
        return merged

    def _refresh_tree(self, select_id: Optional[int] = None) -> None:
        self.tree.delete(*self.tree.get_children())
        merged = self._merged_traits()
        color_names = {3: "金色", 5: "自创", 1: "蓝色", 2: "红色", 0: "特殊"}
        tag_map = {3: "gold", 5: "custom", 1: "blue", 2: "red"}
        for sid in sorted(merged):
            t = merged[sid]
            in_global = sid in self.edit_file_traits
            in_live = sid in self.live_traits
            if in_global and in_live:
                loc = "全局+内存"
            elif in_global:
                loc = "全局文件"
            elif in_live:
                loc = "当前剧本"
            else:
                loc = "本地库"
            if t.cond1 == 9:
                scope = f"[{t.cond1_param or 3}格友军]"
            elif t.cond1 == 10:
                scope = f"[{t.cond1_param or 3}格敌军]"
            elif t.cond1 == 0:
                scope = "[自身]"
            else:
                scope = f"[条件{t.cond1}]"
            state = f"{loc} {scope}"
            tag = tag_map.get(t.color, "")
            self.tree.insert(
                "",
                tk.END,
                iid=str(sid),
                values=(sid, t.name, color_names.get(t.color, "特殊"), state),
                tags=(tag,) if tag else (),
            )
        if select_id is not None and self.tree.exists(str(select_id)):
            self.tree.selection_set(str(select_id))

    def _reload_all_sources(self, *, silent: bool) -> None:
        self.edit_file_traits = {}
        if self.edit_file_path and self.edit_file_path.is_file():
            try:
                self.edit_file_traits = load_edit_personality_file(self.edit_file_path)
                for t in self.edit_file_traits.values():
                    self.controller.catalog.upsert_trait(t.to_trait_info())
            except Exception as exc:
                if not silent:
                    messagebox.showwarning("读取全局文件失败", str(exc), parent=self._toplevel or self.app)

        self.live_traits = {}
        if self.app.mem.is_attached:
            try:
                for t in list_live_custom_traits(self.app.mem):
                    self.live_traits[t.slot_id] = t
                    self.controller.catalog.upsert_trait(t.to_trait_info())
            except MemoryErrorWin32:
                pass
        self._refresh_tree()

    def _pick_initial_slot(self) -> None:
        used = set(self._merged_traits().keys())
        for sid in range(CUSTOM_TRAIT_MIN_ID, CUSTOM_TRAIT_MAX_ID + 1):
            if sid not in used:
                self.slot_var.set(str(sid))
                return

    def _new_free_slot(self) -> None:
        used = set(self._merged_traits().keys())
        try:
            sid = find_next_free_custom_slot(self.app.mem, used)
        except Exception:
            sid = next((i for i in range(CUSTOM_TRAIT_MIN_ID, CUSTOM_TRAIT_MAX_ID + 1) if i not in used), CUSTOM_TRAIT_MIN_ID)
        self.slot_var.set(str(sid))
        self.name_var.set("")
        self.desc_var.set("")
        self.app.status_var.set(f"已分配空闲自创槽位 #{sid}")

    def _on_tree_select(self, _event: tk.Event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        sid = int(sel[0])
        trait = self.live_traits.get(sid) or self.controller.custom_traits.get(sid) or self.edit_file_traits.get(sid)
        if trait is not None:
            self._load_into_form(trait, keep_slot=False)

    def _commit_to_global_file(self) -> None:
        """Write to editpersonalityexceSC.s14 (+ live CEditPersonalityData & CPersonalityData if game running!)."""
        if self.edit_file_path is None:
            self._choose_edit_file()
            if self.edit_file_path is None:
                return
        try:
            traits_to_write = expand_dual_self_aura_traits(self._collect_form())
        except Exception as exc:
            messagebox.showerror("参数无效", str(exc), parent=self._toplevel or self.app)
            return

        # Reload current file first so we never lose other slots created in-game!
        existing: Dict[int, CustomTraitDef] = {}
        if self.edit_file_path.is_file():
            try:
                existing = load_edit_personality_file(self.edit_file_path)
            except Exception:
                existing = dict(self.edit_file_traits)
        for t in traits_to_write:
            existing[t.slot_id] = t

        try:
            save_edit_personality_file(self.edit_file_path, existing, create_backup=True)
        except Exception as exc:
            messagebox.showerror("写入全局文件失败", str(exc), parent=self._toplevel or self.app)
            return

        self.edit_file_traits = existing
        for t in traits_to_write:
            self.controller.custom_traits[t.slot_id] = t
            self.controller.catalog.upsert_trait(t.to_trait_info())
        try:
            save_custom_traits_file(self.controller.custom_traits_path, self.controller.custom_traits)
        except OSError:
            pass

        # If SAN14CE_SC.exe is running (Title Menu OR Scenario), live-sync memory so user sees it IMMEDIATELY!
        live_synced = False
        if self.app.mem.is_attached:
            try:
                for t in traits_to_write:
                    write_live_edit_personality(self.app.mem, t)
                    write_live_custom_trait(self.app.mem, t)
                    self.live_traits[t.slot_id] = t
                live_synced = True
            except Exception:
                pass

        primary = traits_to_write[0]
        self._load_into_form(primary, keep_slot=False)
        self._refresh_tree(select_id=primary.slot_id)
        for t in traits_to_write:
            self.on_changed(t.slot_id, False)

        clamped_note = ""
        if primary.eff1_val > EDIT_FILE_MAX_VAL or primary.eff1_val < EDIT_FILE_MIN_VAL or (
            primary.eff2_id and (primary.eff2_val > EDIT_FILE_MAX_VAL or primary.eff2_val < EDIT_FILE_MIN_VAL)
        ):
            clamped_note = f"\n（注：全局文件已自动适配游戏开局读取上限 {EDIT_FILE_MIN_VAL}~{EDIT_FILE_MAX_VAL}；如需更大数值，进剧本后再点「写入剧本内存」即可。）"

        sync_msg = (
            "已同步写入游戏当前内存！现在切回游戏主菜单的「编辑新武将 / 编辑史实武将」即可直接选用，无需重启游戏。"
            if live_synced
            else "下次打开游戏后，在主菜单「编辑新武将 / 编辑史实武将」中即可直接选用，新开剧本也会自动带入。"
        )
        slot_desc = " + ".join(f"#{t.slot_id}「{t.name}」" for t in traits_to_write)
        dual_note = (
            f"\n\n【自身+友军光环双吃提示】已自动为你生成双槽组合：{slot_desc}。\n在武将个性里同时勾选这两个槽位，自身自部队与周围 {traits_to_write[1].cond1_param} 格友军都能吃到完整加成！"
            if len(traits_to_write) > 1
            else ""
        )
        self.app.status_var.set(f"已写入全局自创个性 {slot_desc} -> {self.edit_file_path.name}")
        self.app._play_beep(True)
        messagebox.showinfo(
            "写入全局自创个性成功",
            f"已写入槽位 {slot_desc} 到：\n{self.edit_file_path}\n\n{sync_msg}{dual_note}{clamped_note}",
            parent=self._toplevel or self.app,
        )

    def _sync_all_to_global_file(self) -> None:
        if self.edit_file_path is None:
            self._choose_edit_file()
            if self.edit_file_path is None:
                return
        merged = self._merged_traits()
        if not merged:
            messagebox.showinfo("暂无自创个性", "列表中还没有任何自创个性。", parent=self._toplevel or self.app)
            return
        try:
            save_edit_personality_file(self.edit_file_path, merged, create_backup=True)
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc), parent=self._toplevel or self.app)
            return
        self.edit_file_traits = dict(merged)
        live_cnt = 0
        if self.app.mem.is_attached:
            for t in merged.values():
                try:
                    write_live_edit_personality(self.app.mem, t)
                    write_live_custom_trait(self.app.mem, t)
                    self.live_traits[t.slot_id] = t
                    live_cnt += 1
                except Exception:
                    pass
        self._refresh_tree()
        self.on_changed(None, False)
        self.app.status_var.set(
            f"已将全部 {len(merged)} 个自创个性写入全局文件 {self.edit_file_path.name}"
            + (f"（并实时同步 {live_cnt} 项到游戏内存）" if live_cnt else "")
        )
        self.app._play_beep(True)

    def _commit_to_game(self, *, equip: bool) -> None:
        try:
            traits_to_write = expand_dual_self_aura_traits(self._collect_form())
        except Exception as exc:
            messagebox.showerror("参数无效", str(exc), parent=self._toplevel or self.app)
            return
        written_list: List[CustomTraitDef] = []
        try:
            for trait in traits_to_write:
                written = write_live_custom_trait(self.app.mem, trait)
                written_list.append(written)
        except Exception as exc:
            messagebox.showerror("写入剧本失败", str(exc), parent=self._toplevel or self.app)
            return
        for written in written_list:
            self.live_traits[written.slot_id] = written
            self.controller.custom_traits[written.slot_id] = written
            self.controller.catalog.upsert_trait(written.to_trait_info())
        try:
            save_custom_traits_file(self.controller.custom_traits_path, self.controller.custom_traits)
        except OSError:
            pass
        primary = written_list[0]
        self._load_into_form(primary, keep_slot=False)
        self._refresh_tree(select_id=primary.slot_id)
        for written in written_list:
            self.on_changed(written.slot_id, equip)
        self.app._play_beep(True)

    def _sync_all_to_game(self) -> None:
        merged = self._merged_traits()
        if not merged:
            messagebox.showinfo("列表为空", "还没有保存的自创个性。", parent=self._toplevel or self.app)
            return
        count = 0
        try:
            for trait in merged.values():
                written = write_live_custom_trait(self.app.mem, trait)
                try:
                    write_live_edit_personality(self.app.mem, trait)
                except Exception:
                    pass
                self.live_traits[written.slot_id] = written
                self.controller.catalog.upsert_trait(written.to_trait_info())
                count += 1
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc), parent=self._toplevel or self.app)
            return
        self._refresh_tree()
        self.on_changed(None, False)
        self.app.status_var.set(f"已将全部 {count} 个自创个性同步注入到当前游戏内存")
        self.app._play_beep(True)

    def _delete_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        sid = int(sel[0])
        if sid in self.edit_file_traits and self.edit_file_path is not None:
            del self.edit_file_traits[sid]
            try:
                save_edit_personality_file(self.edit_file_path, self.edit_file_traits, create_backup=True)
            except Exception as exc:
                messagebox.showerror("更新全局文件失败", str(exc), parent=self._toplevel or self.app)
                return
        if self.app.mem.is_attached:
            try:
                clear_live_edit_personality(self.app.mem, sid)
            except Exception:
                pass
            try:
                clear_live_custom_trait(self.app.mem, sid)
            except Exception:
                pass
            self.live_traits.pop(sid, None)
        if sid in self.controller.custom_traits:
            del self.controller.custom_traits[sid]
            save_custom_traits_file(self.controller.custom_traits_path, self.controller.custom_traits)
        self.controller.catalog.remove_trait(sid)
        self._refresh_tree()
        self.on_changed(None, False)
        self.app.status_var.set(f"已从全局文件与本地库清除自创个性槽位 #{sid}")
