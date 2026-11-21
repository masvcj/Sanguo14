"""Unrestricted Custom Trait (无限制自创个性) engine for SAN14PK Heritage Edition (SAN14CE_SC.exe).

Writes directly to the game's 100 custom trait slots in CPersonalityData (IDs 256..355,
mgr + 0x7DD18, vtable 0x13AC240, stride 0xE8), bypassing all official editor restrictions:
- Any of the 126 real engine effects (including legendary golden effects like 飞将/洞察/火神/卧龙)
- Any combination of 2 effects + any trigger condition (default: unconditional, all terrains & units)
- Unrestricted signed 16-bit effect values (-1000..+30000 instead of official +2/+6 caps)
- Natively saved & loaded by SAN14CE inside .s14 save files, plus local JSON library persistence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from game_catalog import TraitInfo
from memory_engine import MemoryErrorWin32, ProcessMemory
from officer_auto_locator import MANAGER_PTR_RVA, supported_executable
from save_editor import discover_save_directories

MGR_PERSONALITY_ARR = 0x7DD18
PERSONALITY_RECORD_TYPE = 0x13AC240
PERSONALITY_STRIDE = 0xE8
CUSTOM_TRAIT_MIN_ID = 256
CUSTOM_TRAIT_MAX_ID = 355
TOTAL_PERSONALITY_SLOTS = 356

# Global pre-scenario edit data (CEditPersonalityData[0..99] & editpersonalityexceSC.s14)
EDIT_MGR_PTR_RVA = 0x2148880
EDIT_MGR_PERSONALITY_ARR = 0xB4E18
EDIT_PERSONALITY_RECORD_TYPE = 0x13A9318
EDIT_PERSONALITY_STRIDE = 0x50
EDIT_PERSONALITY_SLOT_COUNT = 100
EDIT_PERSONALITY_FILE_RECORD_SIZE = 63
EDIT_PERSONALITY_FILE_SIZE = EDIT_PERSONALITY_SLOT_COUNT * EDIT_PERSONALITY_FILE_RECORD_SIZE  # 6300 bytes
EDIT_PERSONALITY_FILENAME = "editpersonalityexceSC.s14"
EDIT_PERSONALITY_VERSION = 2

MIN_EFFECT_VAL = -1000
MAX_EFFECT_VAL = 30000
EDIT_FILE_MIN_VAL = -100
EDIT_FILE_MAX_VAL = 2000


@dataclass(frozen=True)
class TraitEffectDef:
    id: int
    category: str
    name: str
    default_val: int
    normal_val: int
    normal_text: str
    hint: str
    passive_neg_range: bool = False  # Official engine uses range_flag = -1 when cond1 == 0


# Verified effect IDs & official normal values extracted directly from SAN14CE_SC.exe CPersonalityData (1..255).
TRAIT_EFFECT_DEFS: Tuple[TraitEffectDef, ...] = (
    # --- 战斗核心 ---
    TraitEffectDef(19, "战斗强化", "全能力上升（攻军/攻城/防御/破城/机动）", 30, 2, "2 (奸雄/水战2~3)", "可填 10~500 突破上限"),
    TraitEffectDef(5, "战斗强化", "攻军上升（对部队攻击）", 50, 6, "6 (扫荡) / 3 (骁将)", "可填 10~500 突破上限"),
    TraitEffectDef(7, "战斗强化", "防御上升", 50, 6, "6 (坚守) / 3 (强将)", "可填 10~500 突破上限"),
    TraitEffectDef(4, "战斗强化", "机动上升", 50, 6, "6 (疾走/脱兔)", "可填 10~500 突破上限"),
    TraitEffectDef(6, "战斗强化", "攻城上升（对据点攻击）", 50, 6, "6 (远矢) / 4 (发明)", "可填 10~500 突破上限"),
    TraitEffectDef(8, "战斗强化", "破城上升（对耐久破坏）", 50, 6, "6 (摧墙) / 4 (发明)", "可填 10~500 突破上限"),
    TraitEffectDef(97, "战斗强化", "老将全能力上升且士气不易降", 30, 3, "3 (老将)", "可填 10~500 突破上限"),
    TraitEffectDef(13, "战斗强化", "士气不易下降", 80, 20, "20 (长驱) / 10 (求心)", "填 80~100 几乎不掉士气", True),
    TraitEffectDef(77, "战斗强化", "指挥士兵上限增加（动员/名门）", 20000, 1000, "1000 (动员) / 2000 (名门)", "可填 5000~30000 突破官职上限"),
    TraitEffectDef(79, "战斗强化", "击破敌军时士气上升（昂扬）", 50, 10, "10 (昂扬/诗韵)", "可填 20~200", True),
    TraitEffectDef(80, "战斗强化", "每回合士气增减（奏乐正/威风负）", 30, 10, "+10 (奏乐) / -4 (威风)", "正数回己方士气，负数降敌方士气"),
    TraitEffectDef(108, "战斗强化", "极易发动连携合作（辅佐）", 100, 25, "25 (辅佐)", "填 100 极易触发连携"),
    TraitEffectDef(103, "战斗强化", "攻击障碍物大幅减耐久（粉碎）", 500, 200, "200 (粉碎)", "可填 500~2000"),
    TraitEffectDef(112, "战斗强化", "减少来自建筑的攻击伤害（金刚）", 90, 50, "50 (金刚)", "填 80~100 大幅减伤"),
    # --- 传奇神技（无视规则/免疫/特殊）---
    TraitEffectDef(51, "传奇神技", "飞将 · 无视敌方控制区域（ZOC）", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(55, "传奇神技", "无视包围效果（飞将/斗将）", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(21, "传奇神技", "洞察 · 状态异常完全无效", 1, 1, "1 (开关型)", "填 1 免疫混乱/止步/挑衅", True),
    TraitEffectDef(98, "传奇神技", "卧龙 · 异常战法必中且延长时间", -30, -30, "-30 (卧龙) / -20 (书痴)", "负值延长敌方异常，可填 -30~-100", True),
    TraitEffectDef(25, "传奇神技", "火神 · 免疫火伤害且火计必成", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    TraitEffectDef(53, "传奇神技", "强运/护卫 · 不会负伤与战死", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    TraitEffectDef(54, "传奇神技", "强运/血路 · 不会被俘虏", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    TraitEffectDef(56, "传奇神技", "藤甲 · 非火伤害减半（火伤3倍）", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    TraitEffectDef(81, "传奇神技", "破陷 · 接近敌方陷阱自动解除", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    TraitEffectDef(102, "传奇神技", "灭火 · 自动扑灭移动目标之火", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(49, "传奇神技", "石兵 · 可建造石兵八阵", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(52, "传奇神技", "解毒 · 不受毒的影响", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(115, "传奇神技", "看破 · 免疫【伪报】", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(117, "传奇神技", "察觉 · 解除范围友军【伪报】", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(96, "传奇神技", "泰然 · 断粮时不陷入异常", 1, 1, "1 (开关型)", "填 1 即可生效", True),
    # --- 续航、捕缚与单挑 ---
    TraitEffectDef(20, "续航捕缚", "自部队兵粮消耗减少（节约）", 80, 20, "20 (节约)", "填 80~100 几乎不耗粮", True),
    TraitEffectDef(62, "续航捕缚", "每回合自动恢复伤兵（再起/黄天）", 50, 10, "10 (再起/黄天)", "可填 30~100"),
    TraitEffectDef(57, "续航捕缚", "极易俘虏敌方武将（捕缚）", 100, 30, "30 (捕缚) / 35 (追兵)", "填 100 极易抓将", True),
    TraitEffectDef(82, "续航捕缚", "易使敌将负伤或战死（勇士）", 50, 10, "10 (勇士) / 11 (勇者)", "可填 30~100"),
    TraitEffectDef(30, "续航捕缚", "易单挑且单挑获得优势（一骑）", 100, 30, "30 (一骑)", "可填 50~200"),
    TraitEffectDef(29, "续航捕缚", "容易发生单挑（豪杰）", 80, 30, "30 (豪杰)", "可填 50~200"),
    TraitEffectDef(109, "续航捕缚", "击破敌军获得资金（掠夺）", 1000, 100, "100 (掠夺)", "可填 500~10000"),
    TraitEffectDef(110, "续航捕缚", "击破敌军获得兵粮（掠夺）", 2000, 500, "500 (掠夺)", "可填 1000~20000"),
    TraitEffectDef(124, "续航捕缚", "击破敌方设施获得资金（夺取）", 2000, 200, "200 (夺取)", "可填 1000~10000"),
    TraitEffectDef(14, "续航捕缚", "减少扩大占领所需兵力（英名）", 80, 30, "30 (英名)", "填 80~100 极易涂色"),
    TraitEffectDef(28, "续航捕缚", "陷阱与设施建设速度上升（筑城）", 200, 30, "30 (筑城) / 100 (炼金)", "可填 100~500"),
    TraitEffectDef(32, "续航捕缚", "输送部队可输送量增加（调配）", 100, 20, "20 (调配)", "可填 50~500"),
    TraitEffectDef(94, "续航捕缚", "输送部队机动上升（运输）", 100, 30, "30 (运输)", "可填 50~300"),
    TraitEffectDef(99, "续航捕缚", "降低敌军异常战法成功率（凤雏）", 100, 30, "30 (凤雏)", "填 100 极难中异常"),
    TraitEffectDef(9, "续航捕缚", "缩短陷入异常状态的时间（神眼）", 80, 50, "50 (神眼)", "填 80~100"),
    TraitEffectDef(26, "续航捕缚", "减少火伤害且提升火计成功率", 80, 30, "30 (火攻)", "填 80~100", True),
    TraitEffectDef(27, "续航捕缚", "减少敌方陷阱伤害（慎重）", 80, 30, "30 (慎重)", "填 80~100", True),
    # --- 内政与经济 ---
    TraitEffectDef(45, "内政经济", "王佐 · 所在城市全部区域开发加速", 200, 30, "30 (王佐)", "可填 100~500"),
    TraitEffectDef(126, "内政经济", "明敏 · 兼任多区域官员能力不降", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(71, "内政经济", "富豪 · 收入时额外获得资金", 5000, 500, "500 (富豪)", "可填 2000~30000"),
    TraitEffectDef(72, "内政经济", "米道 · 收入时额外获得兵粮", 10000, 1000, "1000 (米道)", "可填 5000~30000"),
    TraitEffectDef(33, "内政经济", "振兴 · 区域开发大幅上升", 200, 30, "30 (振兴) / 100 (炼金)", "可填 100~500"),
    TraitEffectDef(34, "内政经济", "法律 · 区域治安大幅上升", 200, 30, "30 (法律)", "可填 100~500"),
    TraitEffectDef(35, "内政经济", "文化 · 区域人口大幅上升", 200, 30, "30 (文化)", "可填 100~500"),
    TraitEffectDef(36, "内政经济", "征税 · 区域资金收入大幅上升", 200, 30, "30 (征税)", "可填 100~500"),
    TraitEffectDef(37, "内政经济", "农政 · 区域兵粮收入大幅上升", 200, 30, "30 (农政)", "可填 100~500"),
    TraitEffectDef(38, "内政经济", "教化 · 区域士兵大幅上升", 200, 30, "30 (教化)", "可填 100~500"),
    TraitEffectDef(39, "内政经济", "能吏 · 区域任命费用减少", 90, 30, "30 (能吏)", "填 90~100 几乎零费用"),
    TraitEffectDef(15, "内政经济", "名声 · 区域土地占领大幅扩大", 50, 10, "10 (名声) / 11 (诡辩)", "可填 30~100"),
    TraitEffectDef(16, "内政经济", "操练 · 训练官员士气上升加速", 200, 30, "30 (操练)", "可填 100~500"),
    TraitEffectDef(17, "内政经济", "招募 · 募兵官员募兵大幅增加", 200, 30, "30 (招募)", "可填 100~500"),
    TraitEffectDef(43, "内政经济", "修理 · 所在据点耐久恢复增加", 200, 30, "30 (修理)", "可填 100~500"),
    TraitEffectDef(44, "内政经济", "医术 · 所在城市伤兵伤病恢复加速", 200, 30, "30 (医术) / 31 (慈爱)", "可填 100~500"),
    TraitEffectDef(70, "内政经济", "筹措 · 太守据点兵粮消耗减少", 80, 20, "20 (筹措)", "填 80~100 极省粮"),
    TraitEffectDef(66, "内政经济", "抚慰 · 所在城市不会出贼", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(67, "内政经济", "占术 · 无灾害且容易丰收", 100, 30, "30 (占术)", "可填 50~200"),
    TraitEffectDef(68, "内政经济", "坚牢 · 城市遭攻击反击伤害上升", 200, 30, "30 (坚牢)", "可填 100~500"),
    TraitEffectDef(74, "内政经济", "柱石 · 全城区域治安易提升", 200, 30, "30 (柱石)", "可填 100~500"),
    TraitEffectDef(100, "内政经济", "众望 · 防止其他势力怀柔区域", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(101, "内政经济", "仁政 · 太守据点武将忠诚不自然下降", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(107, "内政经济", "监守 · 太守据点俘虏不会逃亡", 1, 1, "1 (开关型)", "填 1 即可生效"),
    TraitEffectDef(125, "内政经济", "安泰 · 防止进攻导致区域开发下降", 1, 1, "1 (开关型)", "填 1 即可生效"),
    # --- 人事、成长与外交 ---
    TraitEffectDef(18, "人事外交", "人脉 · 擢用探索天数大幅缩短", 90, 50, "50 (人脉) / 60 (探求)", "填 80~95 极速完成"),
    TraitEffectDef(63, "人事外交", "眼力 · 擢用探索获得大幅优待", 100, 30, "30 (眼力) / 40 (如今)", "可填 60~200"),
    TraitEffectDef(64, "人事外交", "论客 · 外交获得大幅优待", 100, 30, "30 (论客)", "可填 60~200"),
    TraitEffectDef(65, "人事外交", "策士 · 计略获得大幅优待", 100, 30, "30 (策士) / 50 (冷彻)", "可填 60~200"),
    TraitEffectDef(75, "人事外交", "潜在 · 获得经验值大幅上升", 200, 30, "30 (潜在)", "可填 100~500"),
    TraitEffectDef(76, "人事外交", "史官 · 主义经验获得量大幅增加", 200, 30, "30 (史官)", "可填 100~500"),
    TraitEffectDef(48, "人事外交", "学者 · 每回合自动获得主义经验", 500, 100, "100 (学者)", "可填 300~5000"),
    TraitEffectDef(122, "人事外交", "腾达 · 功绩获得量大幅增加", 200, 30, "30 (腾达)", "可填 100~500"),
    TraitEffectDef(47, "人事外交", "强健 · 极不易伤病", 100, 30, "30 (强健)", "填 80~100"),
    TraitEffectDef(113, "人事外交", "特使 · 海外交易友好度上升加速", 200, 20, "20 (特使)", "可填 100~500"),
    TraitEffectDef(114, "人事外交", "向导 · 海外交易天数缩短", 80, 10, "10 (向导)", "填 50~90"),
)

EFFECT_BY_ID: Dict[int, TraitEffectDef] = {e.id: e for e in TRAIT_EFFECT_DEFS}

DUAL_SELF_AURA_COND1 = 99

# Verified condition IDs (+0xA4)
TRAIT_COND1_OPTIONS: Tuple[Tuple[int, str], ...] = (
    (0, "无条件 · 自身自部队常驻生效（全地形全兵种无限制！）"),
    (DUAL_SELF_AURA_COND1, "自身+光环双吃 · 自身与范围内友军同时生效（自动生成自身+光环双槽）"),
    (9, "仅友军光环 · 范围内己方其他部队生效（原版奸雄/神将，不含自身）"),
    (10, "敌军光环 · 范围内敌方部队生效（配合负值削弱敌军）"),
    (12, "条件 · 仅兵器阵型时生效"),
    (14, "条件 · 位于大河/水战时生效"),
    (15, "条件 · 位于森林/密林时生效"),
    (16, "条件 · 位于山地/山路时生效"),
    (19, "条件 · 大河以外全地形生效（如飞将）"),
    (21, "光环 · 范围内己方男性部队生效"),
    (22, "光环 · 范围内敌方男性部队生效"),
    (25, "条件 · 撤退时生效"),
    (28, "条件 · 位于府上时生效"),
    (32, "条件 · 周围敌军较多时生效（神威）"),
    (34, "条件 · 位于己方势力区域内生效（地利）"),
    (40, "条件 · 击破范围内敌方部队时触发（昂扬）"),
)

# Verified secondary condition IDs (+0xAC)
TRAIT_COND2_OPTIONS: Tuple[Tuple[int, str], ...] = (
    (0, "无额外限制（推荐 · 舰船兵器全适用）"),
    (1, "常规光环/范围标记（神将/昂扬等使用）"),
    (37, "排除舰船与兵器阵型（原版陆战限制）"),
)

COLOR_OPTIONS: Tuple[Tuple[int, str], ...] = (
    (3, "金色（传奇）"),
    (5, "自创专属（官方样式）"),
    (1, "蓝色（正面）"),
    (2, "红色（负面）"),
    (0, "特殊"),
)

AURA_COND1_IDS = frozenset({9, 10, 21, 22, 31, 32, 39, 40, DUAL_SELF_AURA_COND1})


@dataclass
class CustomTraitDef:
    slot_id: int
    name: str
    description: str
    color: int = 3
    eff1_id: int = 19
    eff1_val: int = 30
    eff2_id: int = 0
    eff2_val: int = 0
    cond1: int = 0
    cond2: int = 0
    pinyin: str = ""
    cond1_param: int = 0
    cond2_param: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CustomTraitDef":
        return cls(
            slot_id=int(data.get("slot_id", CUSTOM_TRAIT_MIN_ID)),
            name=str(data.get("name", ""))[:5],
            description=str(data.get("description", ""))[:50],
            color=int(data.get("color", 3)),
            eff1_id=int(data.get("eff1_id", 19)),
            eff1_val=int(data.get("eff1_val", 30)),
            eff2_id=int(data.get("eff2_id", 0)),
            eff2_val=int(data.get("eff2_val", 0)),
            cond1=int(data.get("cond1", 0)),
            cond2=int(data.get("cond2", 0)),
            pinyin=str(data.get("pinyin", ""))[:15],
            cond1_param=int(data.get("cond1_param", 0)),
            cond2_param=int(data.get("cond2_param", 0)),
        )

    def to_trait_info(self) -> TraitInfo:
        return TraitInfo(
            id=self.slot_id,
            name=self.name or f"自创#{self.slot_id}",
            pinyin=self.pinyin or f"custom{self.slot_id}",
            color=self.color,
            description=self.description,
        )


# 6 unrestricted starter templates for one-click inspiration
CUSTOM_TRAIT_TEMPLATES: Tuple[CustomTraitDef, ...] = (
    CustomTraitDef(
        slot_id=256,
        name="战神无双",
        description="全地形全兵种常驻：自部队全能力大幅上升(+30)，无视敌方控制区域",
        color=3,
        eff1_id=19,
        eff1_val=30,
        eff2_id=51,
        eff2_val=1,
        cond1=0,
        cond2=0,
    ),
    CustomTraitDef(
        slot_id=257,
        name="不败金身",
        description="自部队防御大幅上升(+50)，且完全免疫一切异常状态",
        color=3,
        eff1_id=7,
        eff1_val=50,
        eff2_id=21,
        eff2_val=1,
        cond1=0,
        cond2=0,
    ),
    CustomTraitDef(
        slot_id=258,
        name="神速破阵",
        description="自部队机动大幅上升(+50)，移动无视敌方包围效果",
        color=3,
        eff1_id=4,
        eff1_val=50,
        eff2_id=55,
        eff2_val=1,
        cond1=0,
        cond2=0,
    ),
    CustomTraitDef(
        slot_id=259,
        name="百万大军",
        description="突破官职限制：指挥士兵上限+20000，且部队兵粮消耗减少80%",
        color=3,
        eff1_id=77,
        eff1_val=20000,
        eff2_id=20,
        eff2_val=80,
        cond1=0,
        cond2=0,
    ),
    CustomTraitDef(
        slot_id=260,
        name="天命内政",
        description="所在城市全部区域开发大幅加速(+200)，收入时额外获得5000资金",
        color=3,
        eff1_id=45,
        eff1_val=200,
        eff2_id=71,
        eff2_val=5000,
        cond1=0,
        cond2=0,
    ),
    CustomTraitDef(
        slot_id=261,
        name="霸主光环",
        description="自身及范围3格内友军全能力大幅上升(+30)，且士气极不易下降",
        color=3,
        eff1_id=19,
        eff1_val=30,
        eff2_id=13,
        eff2_val=80,
        cond1=DUAL_SELF_AURA_COND1,
        cond2=0,
        cond1_param=3,
    ),
)


def auto_generate_description(trait: CustomTraitDef) -> str:
    """Build a clear concise Chinese description (<=50 chars) from effects & conditions."""
    prefix = ""
    if trait.cond1 == DUAL_SELF_AURA_COND1:
        r = trait.cond1_param or 3
        prefix = f"自身与{r}格友军："
    elif trait.cond1 == 9:
        r = trait.cond1_param or 3
        prefix = f"范围{r}格友军："
    elif trait.cond1 == 10:
        r = trait.cond1_param or 3
        prefix = f"范围{r}格敌军："
    elif trait.cond1 == 14:
        prefix = "水战时："
    elif trait.cond1 == 15:
        prefix = "林战时："
    elif trait.cond1 == 16:
        prefix = "山战时："
    elif trait.cond1 == 40:
        prefix = "击破敌军："

    parts: List[str] = []
    for eid, val in ((trait.eff1_id, trait.eff1_val), (trait.eff2_id, trait.eff2_val)):
        if not eid:
            continue
        edef = EFFECT_BY_ID.get(eid)
        if edef is None:
            parts.append(f"效果#{eid}({val:+d})")
        elif edef.default_val == 1 and val == 1:
            parts.append(edef.name.split("（", 1)[0])
        else:
            short = edef.name.split("（", 1)[0]
            parts.append(f"{short}({val:+d})")
    text = prefix + "，".join(parts)
    return text[:50]


def _encode_utf16_fixed(text: str, total_bytes: int) -> bytes:
    max_chars = (total_bytes // 2) - 1
    enc = text[:max_chars].encode("utf-16-le") + b"\x00\x00"
    if len(enc) < total_bytes:
        enc += b"\x00" * (total_bytes - len(enc))
    return enc[:total_bytes]


def _read_utf16z(buf: bytes, offset: int, max_bytes: int) -> str:
    raw = buf[offset : min(len(buf), offset + max_bytes)]
    for i in range(0, len(raw) - 1, 2):
        if raw[i] == 0 and raw[i + 1] == 0:
            raw = raw[:i]
            break
    return raw.decode("utf-16-le", errors="replace").strip()


def _compute_engine_flags(trait: CustomTraitDef) -> Tuple[int, int]:
    """Return (range_flag i16, flags u8) matching official CPersonalityData conventions."""
    e1 = EFFECT_BY_ID.get(trait.eff1_id)
    e2 = EFFECT_BY_ID.get(trait.eff2_id)
    needs_neg_range = (
        trait.cond1 == 0
        and ((e1 is not None and e1.passive_neg_range) or (e2 is not None and e2.passive_neg_range))
    ) or trait.cond1 in (39, 40)
    range_flag = -1 if needs_neg_range else 0
    # 0x1A = active(0x10) | domestic(0x08) | combat(0x02) — recognized by all engine hooks!
    flags = 0x1A
    return range_flag, flags


def _get_personality_table(mem: ProcessMemory) -> int:
    if not mem.is_attached or not mem.base_address:
        raise MemoryErrorWin32("请先连接游戏并进入剧本。")
    if not supported_executable(mem):
        raise MemoryErrorWin32("实时自创个性仅支持简体中文传承版 SAN14CE_SC.exe。")
    mgr = int(mem.read_value(mem.base_address + MANAGER_PTR_RVA, "u64"))
    if mgr < 0x10000:
        raise MemoryErrorWin32("尚未进入剧本，无法读取个性数据表。")
    ptable = int(mem.read_value(mgr + MGR_PERSONALITY_ARR, "u64"))
    if ptable < 0x10000 or int(mem.read_value(ptable, "u64")) != mem.base_address + PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32("个性数据表结构校验失败。")
    return ptable


def read_live_trait(mem: ProcessMemory, slot_id: int) -> CustomTraitDef:
    """Read one CPersonalityData entry (1..355) from live game memory."""
    if not 1 <= slot_id < TOTAL_PERSONALITY_SLOTS:
        raise ValueError(f"个性槽位 ID 超出范围 (1~{TOTAL_PERSONALITY_SLOTS - 1}): {slot_id}")
    ptable = _get_personality_table(mem)
    addr = ptable + slot_id * PERSONALITY_STRIDE
    raw = mem.read_bytes(addr, PERSONALITY_STRIDE)
    if struct.unpack_from("<Q", raw, 0)[0] != mem.base_address + PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32(f"个性槽位 #{slot_id} 结构校验失败。")
    t = CustomTraitDef(
        slot_id=slot_id,
        name=_read_utf16z(raw, 0x10, 12),
        pinyin=_read_utf16z(raw, 0x1C, 32),
        description=_read_utf16z(raw, 0x3C, 102),
        cond1=raw[0xA4],
        cond1_param=struct.unpack_from("<I", raw, 0xA8)[0],
        cond2=raw[0xAC],
        cond2_param=struct.unpack_from("<I", raw, 0xB0)[0],
        eff1_id=raw[0xB6],
        eff1_val=struct.unpack_from("<h", raw, 0xB8)[0],
        eff2_id=raw[0xBA],
        eff2_val=struct.unpack_from("<h", raw, 0xBC)[0],
        color=raw[0xBE],
    )
    if not t.description and t.eff1_id:
        t.description = auto_generate_description(t)
    return t


def list_live_custom_traits(mem: ProcessMemory) -> List[CustomTraitDef]:
    """Return all active custom traits currently populated in CPersonalityData[256..355]."""
    ptable = _get_personality_table(mem)
    count = CUSTOM_TRAIT_MAX_ID - CUSTOM_TRAIT_MIN_ID + 1
    raw_all = mem.read_bytes(ptable + CUSTOM_TRAIT_MIN_ID * PERSONALITY_STRIDE, count * PERSONALITY_STRIDE)
    vt = mem.base_address + PERSONALITY_RECORD_TYPE
    out: List[CustomTraitDef] = []
    for idx in range(count):
        rec = raw_all[idx * PERSONALITY_STRIDE : (idx + 1) * PERSONALITY_STRIDE]
        if struct.unpack_from("<Q", rec, 0)[0] != vt:
            break
        eff1 = rec[0xB6]
        name = _read_utf16z(rec, 0x10, 12)
        if eff1 == 0 and not name:
            continue
        slot_id = CUSTOM_TRAIT_MIN_ID + idx
        t = CustomTraitDef(
            slot_id=slot_id,
            name=name or f"自创#{slot_id}",
            pinyin=_read_utf16z(rec, 0x1C, 32),
            description=_read_utf16z(rec, 0x3C, 102),
            cond1=rec[0xA4],
            cond1_param=struct.unpack_from("<I", rec, 0xA8)[0],
            cond2=rec[0xAC],
            cond2_param=struct.unpack_from("<I", rec, 0xB0)[0],
            eff1_id=eff1,
            eff1_val=struct.unpack_from("<h", rec, 0xB8)[0],
            eff2_id=rec[0xBA],
            eff2_val=struct.unpack_from("<h", rec, 0xBC)[0],
            color=rec[0xBE],
        )
        if not t.description and t.eff1_id:
            t.description = auto_generate_description(t)
        out.append(t)
    return out


def find_next_free_custom_slot(mem: ProcessMemory, used_ids: Sequence[int] = ()) -> int:
    """Return the first unused custom trait slot ID in 256..355."""
    occupied = set(int(x) for x in used_ids)
    try:
        for existing in list_live_custom_traits(mem):
            occupied.add(existing.slot_id)
    except MemoryErrorWin32:
        pass
    for sid in range(CUSTOM_TRAIT_MIN_ID, CUSTOM_TRAIT_MAX_ID + 1):
        if sid not in occupied:
            return sid
    raise ValueError("100 个自创个性槽位 (256~355) 已全满，请覆盖或清空一个旧槽位。")


def normalize_custom_trait(trait: CustomTraitDef) -> CustomTraitDef:
    """Validate and normalize a custom trait definition before writing to memory or disk."""
    if not CUSTOM_TRAIT_MIN_ID <= trait.slot_id <= CUSTOM_TRAIT_MAX_ID:
        raise ValueError(f"自创个性槽位必须在 {CUSTOM_TRAIT_MIN_ID}~{CUSTOM_TRAIT_MAX_ID} 之间。")
    name = trait.name.strip()
    if not name:
        raise ValueError("请输入自创个性名称（最多 5 个字）。")
    if not 1 <= trait.eff1_id <= 126:
        raise ValueError("效果 1 不能为空，请选择一个有效效果（ID 1~126）。")
    if not 0 <= trait.eff2_id <= 126:
        raise ValueError("效果 2 ID 超出范围（0~126）。")

    eff1_val = max(MIN_EFFECT_VAL, min(MAX_EFFECT_VAL, int(trait.eff1_val)))
    eff2_val = max(MIN_EFFECT_VAL, min(MAX_EFFECT_VAL, int(trait.eff2_val))) if trait.eff2_id else 0
    color = int(trait.color) if int(trait.color) in (0, 1, 2, 3, 5) else 3
    cond1 = max(0, min(100, int(trait.cond1)))
    cond2 = max(0, min(100, int(trait.cond2)))
    cond1_param = max(0, min(99999, int(trait.cond1_param)))
    if cond1 in AURA_COND1_IDS and cond1_param == 0:
        cond1_param = 3
    cond2_param = max(0, min(99999, int(trait.cond2_param)))
    desc = (trait.description.strip() or auto_generate_description(trait))[:50]
    pinyin = trait.pinyin.strip()[:15]

    return CustomTraitDef(
        slot_id=int(trait.slot_id),
        name=name[:5],
        description=desc,
        color=color,
        eff1_id=int(trait.eff1_id),
        eff1_val=eff1_val,
        eff2_id=int(trait.eff2_id),
        eff2_val=eff2_val,
        cond1=cond1,
        cond2=cond2,
        pinyin=pinyin,
        cond1_param=cond1_param,
        cond2_param=cond2_param,
    )


def expand_dual_self_aura_traits(trait: CustomTraitDef) -> List[CustomTraitDef]:
    """Expand a trait into [self_trait, aura_trait] when cond1 == DUAL_SELF_AURA_COND1 (99).

    In SAN14's engine, cond1=0 targets the officer's own unit (自部队) whereas cond1=9 targets
    surrounding friendly units within `cond1_param` hexes excluding self (除自部队之外，特定范围内自势力部队).
    Generating the linked pair (Slot N for self + Slot N+1 for friendly aura) lets both the officer's
    own unit and all friendly units within range receive the exact same buffs simultaneously.
    """
    norm = normalize_custom_trait(trait)
    if norm.cond1 != DUAL_SELF_AURA_COND1:
        return [norm]

    radius = norm.cond1_param if norm.cond1_param > 0 else 3
    clean_base = norm.name.strip()
    if clean_base.endswith("光环") and len(clean_base) > 2:
        clean_base = clean_base[:-2]
    elif clean_base.endswith("环") and len(clean_base) > 1:
        clean_base = clean_base[:-1]
    clean_base = (clean_base or norm.name)[:4]

    self_raw = CustomTraitDef(
        slot_id=norm.slot_id,
        name=clean_base[:5],
        description="",
        color=norm.color,
        eff1_id=norm.eff1_id,
        eff1_val=norm.eff1_val,
        eff2_id=norm.eff2_id,
        eff2_val=norm.eff2_val,
        cond1=0,
        cond2=0,
        pinyin=norm.pinyin,
        cond1_param=0,
        cond2_param=0,
    )
    self_trait = normalize_custom_trait(self_raw)

    aura_slot = norm.slot_id + 1 if norm.slot_id < CUSTOM_TRAIT_MAX_ID else norm.slot_id - 1
    aura_name = (clean_base[:4] + "环")[:5]
    aura_raw = CustomTraitDef(
        slot_id=aura_slot,
        name=aura_name[:5],
        description="",
        color=norm.color,
        eff1_id=norm.eff1_id,
        eff1_val=norm.eff1_val,
        eff2_id=norm.eff2_id,
        eff2_val=norm.eff2_val,
        cond1=9,
        cond2=norm.cond2,
        pinyin=norm.pinyin,
        cond1_param=radius,
        cond2_param=norm.cond2_param,
    )
    aura_trait = normalize_custom_trait(aura_raw)
    return [self_trait, aura_trait]


def write_live_custom_trait(mem: ProcessMemory, trait: CustomTraitDef) -> CustomTraitDef:
    """Write an unrestricted custom trait directly into live CPersonalityData[slot_id]."""
    normalized = normalize_custom_trait(trait)
    ptable = _get_personality_table(mem)
    addr = ptable + normalized.slot_id * PERSONALITY_STRIDE
    if int(mem.read_value(addr, "u64")) != mem.base_address + PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32(f"个性槽位 #{normalized.slot_id} 内存校验失败。")

    range_flag, flags = _compute_engine_flags(normalized)

    # Preserve vtable (+0x00..+0x0F) and trailing std::vector (+0xC8..+0xE8)!
    text_block = (
        _encode_utf16_fixed(normalized.name, 12)
        + _encode_utf16_fixed(normalized.pinyin, 32)
        + _encode_utf16_fixed(normalized.description, 102)
    )  # 12 + 32 + 102 = 146 bytes (+0x10..+0xA1)
    assert len(text_block) == 0x92

    logic_block = struct.pack(
        "<B3xIB3xIhHhHhBB",
        normalized.cond1,
        normalized.cond1_param,
        normalized.cond2,
        normalized.cond2_param,
        range_flag,
        normalized.eff1_id,
        normalized.eff1_val,
        normalized.eff2_id,
        normalized.eff2_val,
        normalized.color,
        flags,
    )
    assert len(logic_block) == 0x1C

    label = f"自创个性#{normalized.slot_id}.{normalized.name}"
    mem.write_bytes(addr + 0x10, text_block, record_patch=True, label=f"{label}.文本")
    mem.write_bytes(addr + 0xA4, logic_block, record_patch=True, label=f"{label}.效果")

    if mem.read_bytes(addr + 0xA4, len(logic_block)) != logic_block:
        raise MemoryErrorWin32("自创个性效果数据读回校验不一致。")
    return normalized


def clear_live_custom_trait(mem: ProcessMemory, slot_id: int) -> None:
    """Clear a custom trait slot (256..355) back to empty/invalid state."""
    if not CUSTOM_TRAIT_MIN_ID <= slot_id <= CUSTOM_TRAIT_MAX_ID:
        raise ValueError(f"只能清空自创个性槽位 ({CUSTOM_TRAIT_MIN_ID}~{CUSTOM_TRAIT_MAX_ID})。")
    ptable = _get_personality_table(mem)
    addr = ptable + slot_id * PERSONALITY_STRIDE
    if int(mem.read_value(addr, "u64")) != mem.base_address + PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32(f"个性槽位 #{slot_id} 内存校验失败。")
    mem.write_bytes(addr + 0x10, b"\x00" * 0x92, record_patch=True, label=f"清空自创个性#{slot_id}.文本")
    mem.write_bytes(addr + 0xA4, b"\x00" * 0x1C, record_patch=True, label=f"清空自创个性#{slot_id}.效果")


# ---------------- Global Pre-Scenario Edit Personality File & Live Memory ----------------


def discover_edit_personality_file() -> Optional[Path]:
    """Locate Steam's editpersonalityexceSC.s14 (or return the default target path in Steam remote)."""
    dirs = discover_save_directories()
    for d in dirs:
        candidate = d / EDIT_PERSONALITY_FILENAME
        if candidate.is_file():
            return candidate
    if dirs:
        return dirs[0] / EDIT_PERSONALITY_FILENAME
    return None


def pack_edit_personality_record(trait: Optional[CustomTraitDef]) -> bytes:
    """Pack one CEditPersonalityData record into the exact 63-byte editpersonalityexceSC.s14 format."""
    if trait is None:
        return bytes((EDIT_PERSONALITY_VERSION,)) + b"\x00" * (EDIT_PERSONALITY_FILE_RECORD_SIZE - 1)
    t = normalize_custom_trait(trait)
    # CopyFromEdit clamps to -100..+2000 on load; keep signed i16 within safe range for file storage.
    v1 = max(EDIT_FILE_MIN_VAL, min(EDIT_FILE_MAX_VAL, t.eff1_val))
    v2 = max(EDIT_FILE_MIN_VAL, min(EDIT_FILE_MAX_VAL, t.eff2_val)) if t.eff2_id else 0
    rec = (
        struct.pack("<BH", EDIT_PERSONALITY_VERSION, t.slot_id)
        + _encode_utf16_fixed(t.name, 12)
        + _encode_utf16_fixed(t.pinyin, 32)
        + struct.pack(
            "<BBBhBhII",
            t.cond1,
            t.cond2,
            t.eff1_id,
            v1,
            t.eff2_id,
            v2,
            t.cond1_param,
            t.cond2_param,
        )
    )
    assert len(rec) == EDIT_PERSONALITY_FILE_RECORD_SIZE
    return rec


def unpack_edit_personality_record(record: bytes, slot_index: int) -> Optional[CustomTraitDef]:
    """Unpack one 63-byte record from editpersonalityexceSC.s14."""
    if len(record) != EDIT_PERSONALITY_FILE_RECORD_SIZE:
        raise ValueError(f"单条自创个性记录长度异常：{len(record)}（期望 {EDIT_PERSONALITY_FILE_RECORD_SIZE}）")
    _ver, stored_id = struct.unpack_from("<BH", record, 0)
    if stored_id == 0:
        return None
    name = _read_utf16z(record, 0x03, 12)
    pinyin = _read_utf16z(record, 0x0F, 32)
    cond1, cond2, eff1_id, eff1_val, eff2_id, eff2_val, cond1_param, cond2_param = struct.unpack_from(
        "<BBBhBhII", record, 0x2F
    )
    if eff1_id == 0 and not name:
        return None
    slot_id = stored_id if CUSTOM_TRAIT_MIN_ID <= stored_id <= CUSTOM_TRAIT_MAX_ID else CUSTOM_TRAIT_MIN_ID + slot_index
    t = CustomTraitDef(
        slot_id=slot_id,
        name=name or f"自创#{slot_id}",
        description="",
        color=5,
        eff1_id=eff1_id,
        eff1_val=eff1_val,
        eff2_id=eff2_id,
        eff2_val=eff2_val,
        cond1=cond1,
        cond2=cond2,
        pinyin=pinyin,
        cond1_param=cond1_param,
        cond2_param=cond2_param,
    )
    t.description = auto_generate_description(t)
    return t


def load_edit_personality_file(path: Path) -> Dict[int, CustomTraitDef]:
    """Read all populated custom traits from editpersonalityexceSC.s14 (6,300 bytes)."""
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    if len(raw) != EDIT_PERSONALITY_FILE_SIZE:
        raise ValueError(
            f"{path.name} 文件大小为 {len(raw)} 字节（标准应为 {EDIT_PERSONALITY_FILE_SIZE} 字节），已拒绝解析。"
        )
    out: Dict[int, CustomTraitDef] = {}
    for idx in range(EDIT_PERSONALITY_SLOT_COUNT):
        rec = raw[idx * EDIT_PERSONALITY_FILE_RECORD_SIZE : (idx + 1) * EDIT_PERSONALITY_FILE_RECORD_SIZE]
        parsed = unpack_edit_personality_record(rec, idx)
        if parsed is not None:
            out[parsed.slot_id] = parsed
    return out


def save_edit_personality_file(
    path: Path,
    traits: Dict[int, CustomTraitDef],
    *,
    create_backup: bool = True,
) -> Path:
    """Write all 100 custom trait slots to editpersonalityexceSC.s14 (6,300 bytes) with automatic .bak backup."""
    if create_backup and path.is_file():
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            bak.write_bytes(path.read_bytes())

    chunks = []
    for idx in range(EDIT_PERSONALITY_SLOT_COUNT):
        sid = CUSTOM_TRAIT_MIN_ID + idx
        chunks.append(pack_edit_personality_record(traits.get(sid)))
    payload = b"".join(chunks)
    assert len(payload) == EDIT_PERSONALITY_FILE_SIZE

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _get_live_edit_personality_table(mem: ProcessMemory) -> int:
    if not mem.is_attached or not mem.base_address or not supported_executable(mem):
        raise MemoryErrorWin32("未连接简体中文传承版 SAN14CE_SC.exe。")
    g_edit = int(mem.read_value(mem.base_address + EDIT_MGR_PTR_RVA, "u64"))
    if g_edit < 0x10000:
        raise MemoryErrorWin32("编辑管理器尚未初始化。")
    arr = int(mem.read_value(g_edit + EDIT_MGR_PERSONALITY_ARR, "u64"))
    if arr < 0x10000 or int(mem.read_value(arr, "u64")) != mem.base_address + EDIT_PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32("CEditPersonalityData 内存结构校验失败。")
    return arr


def write_live_edit_personality(mem: ProcessMemory, trait: CustomTraitDef) -> CustomTraitDef:
    """Write one custom trait into live CEditPersonalityData[slot_id - 256] (works on Title Menu!)."""
    t = normalize_custom_trait(trait)
    arr = _get_live_edit_personality_table(mem)
    idx = t.slot_id - CUSTOM_TRAIT_MIN_ID
    addr = arr + idx * EDIT_PERSONALITY_STRIDE
    if int(mem.read_value(addr, "u64")) != mem.base_address + EDIT_PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32(f"CEditPersonalityData[{idx}] 结构校验失败。")

    v1 = max(EDIT_FILE_MIN_VAL, min(EDIT_FILE_MAX_VAL, t.eff1_val))
    v2 = max(EDIT_FILE_MIN_VAL, min(EDIT_FILE_MAX_VAL, t.eff2_val)) if t.eff2_id else 0
    # Layout (+0x08..+0x4F, 0x48 = 72 bytes):
    # +0x08: u8 ver(2), 1B pad | +0x0A: u16 slot_id | +0x0C: 12B name | +0x18: 32B pinyin
    # +0x38: u8 cond1, 3B pad | +0x3C: u32 cond1_param | +0x40: u8 cond2, 3B pad | +0x44: u32 cond2_param
    # +0x48: u8 eff1_id, 1B pad | +0x4A: i16 eff1_val | +0x4C: u8 eff2_id, 1B pad | +0x4E: i16 eff2_val
    body = (
        struct.pack("<BxH", EDIT_PERSONALITY_VERSION, t.slot_id)
        + _encode_utf16_fixed(t.name, 12)
        + _encode_utf16_fixed(t.pinyin, 32)
        + struct.pack(
            "<B3xIB3xIBxhBxh",
            t.cond1,
            t.cond1_param,
            t.cond2,
            t.cond2_param,
            t.eff1_id,
            v1,
            t.eff2_id,
            v2,
        )
    )
    assert len(body) == EDIT_PERSONALITY_STRIDE - 8
    mem.write_bytes(addr + 8, body, record_patch=True, label=f"全局自创个性#{t.slot_id}.{t.name}")
    if mem.read_bytes(addr + 8, len(body)) != body:
        raise MemoryErrorWin32("CEditPersonalityData 写入后读回校验失败。")
    return t


def clear_live_edit_personality(mem: ProcessMemory, slot_id: int) -> None:
    """Clear one live CEditPersonalityData slot on the title menu / in-game."""
    if not CUSTOM_TRAIT_MIN_ID <= slot_id <= CUSTOM_TRAIT_MAX_ID:
        raise ValueError(f"槽位超出范围 ({CUSTOM_TRAIT_MIN_ID}~{CUSTOM_TRAIT_MAX_ID})。")
    arr = _get_live_edit_personality_table(mem)
    idx = slot_id - CUSTOM_TRAIT_MIN_ID
    addr = arr + idx * EDIT_PERSONALITY_STRIDE
    if int(mem.read_value(addr, "u64")) != mem.base_address + EDIT_PERSONALITY_RECORD_TYPE:
        raise MemoryErrorWin32(f"CEditPersonalityData[{idx}] 结构校验失败。")
    empty_body = bytes((EDIT_PERSONALITY_VERSION,)) + b"\x00" * (EDIT_PERSONALITY_STRIDE - 9)
    mem.write_bytes(addr + 8, empty_body, record_patch=True, label=f"清空全局自创个性#{slot_id}")


def load_custom_traits_file(path: Path) -> Dict[int, CustomTraitDef]:
    """Load saved custom trait definitions from profiles/custom_traits.json."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, list):
        return {}
    out: Dict[int, CustomTraitDef] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            t = CustomTraitDef.from_dict(entry)
        except (TypeError, ValueError):
            continue
        if CUSTOM_TRAIT_MIN_ID <= t.slot_id <= CUSTOM_TRAIT_MAX_ID and t.eff1_id > 0:
            out[t.slot_id] = t
    return out


def save_custom_traits_file(path: Path, traits: Dict[int, CustomTraitDef]) -> None:
    """Persist custom trait definitions to profiles/custom_traits.json."""
    if not traits:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [traits[k].to_dict() for k in sorted(traits)]
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
