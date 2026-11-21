# 项目交接与架构上下文（PROJECT_CONTEXT）

> **用途**：跨电脑同步与 AI 协作快速接手文档。在新电脑打开仓库后，可直接让 Antigravity 先阅读本文件与 `README.md`，即可掌握当前进度、核心模块、已验证内存偏移与开发/测试流程。
>
> **最后更新**：2026-11-21（对应初始提交 `e0224b1`）

---

## 1. 项目概览

- **项目名称**：《三国志14 威力加强版（传承版）》实时内存与存档修改器
- **目标游戏进程**：`SAN14CE_SC.exe`（简体中文传承版）
- **已验证游戏主程序 SHA-256**：
  `2e49348aa1b9be5cfca651e71c8d77b2e4bd2d11387edc0b0236f4352458a239`
- **技术栈**：纯 Python 3（Tkinter 桌面 GUI + `ctypes` 调用 `kernel32` / `psapi` / `user32`），运行时**零第三方依赖**；打包 EXE 使用 `PyInstaller`（安装在 `.build-tools/`，不入库）。

---

## 2. 当前已完成功能清单

### ① 基础属性自动定位修改（`app.py` / `officer_auto_locator.py` / `hotkey_engine.py`）
- **自动识别当前游戏详情页**：打开武将、城市、地区、关隘、部队情报后自动绑定实时内存记录，关闭详情或目标切换时自动拒写，写入后回读校验，支持底部「恢复全部修改」一键回滚原始字节。
- **已接入自动定位字段**：
  - **全局（2 项）**：无限命令书（锁定 99）、最高施政等级（主义经验满级 Lv.10）。
  - **武将（10 项）**：统率、武力、智力、政治、魅力、忠诚度、施政等级、突破能力上限（五维上限 120）、出生年份、伤病状态。
  - **城市（9 项）**：耐久、士气、资金、兵粮、士兵、伤兵、挑衅剩余天数、混乱剩余天数、都市规模（1~4）。
  - **地区（6 项）**：商业、农业、兵舍、人口、治安、建设上限。
  - **关隘（6 项）**：耐久、士气、士兵、伤兵、挑衅、混乱。
  - **部队（8 项）**：士气、士兵、伤兵、挑衅、混乱、止步、携带资金、携带兵粮。
- **交互增强**：每行可自定义全局热键并持久化、分组快捷操作（读取当前值 / 一键全满 / 削弱归1 / 一键净化）、窗口置顶与提示音开关。

### ② 武将进阶编辑（`world_panel.py` / `game_world_editor.py`）
- 支持读取当前详情武将，或加载全剧本武将按姓名/ID 搜索。
- 14 项阵型位掩码解锁（含全陆战 / 全水陆 / 含四夷一键按钮）。
- 出生年份、卒年（一键延寿至 350 年）、忠诚度、伤病、主义（6 种）、自带政策与等级（实时读取游戏政策表）、五维上限。

### ③ 势力批量 / 外交异族 / 名品管理（`world_panel.py` / `game_world_editor.py`）
- **己方势力批量操作**：严格限定只修改玩家所属势力下辖资产（全都市补给、全区域满开发满治安、全武将满忠诚/治愈/解锁阵型/延寿）。
- **外交与异族/海外**：势力双向友好度、五异民族友好度、海外四国（天竺/贵霜/安息/大秦）友好度与通商等级。
- **名品管理**：全剧本名品搜索与归属授予（可一键授予当前详情武将）。

### ④ 个性（5 槽位）与战法/战技（10 槽位）编辑（`trait_tactic_panel.py` / `officer_live_editor.py` / `save_editor.py` / `lwc_codec.py`）
- 同时支持 **剧本实时内存修改** 与 **`.s14` 存档文件修改**（纯 Python 实现光荣 `LWC\x1a` / LS11 编解码，修改存档前自动生成 `.bak` 备份）。
- 内置技能方案预设（野战神将 / 谋略攻城 / 内政神辅）及自定义方案持久化。

### ⑤ 无限制自创个性工坊（最新重点模块：`custom_trait_editor.py` / `custom_trait_dialog.py`）
- **双渠道写入**：
  1. **剧本内实时注入**：直写 `CPersonalityData[256..355]`（100 个自创槽位），支持一步装备到当前武将，随原生 `.s14` 存档持久保存，数值范围 `-1000 ~ +30000`。
  2. **全局预选写入（进剧本前给史实/自创武将选用）**：按精确 6300 字节格式写入 Steam 存档目录 `editpersonalityexceSC.s14`（自动 `.bak` 备份，数值范围 `-100 ~ +2000`），且在游戏运行时实时同步主菜单/剧本内存中的 `CEditPersonalityData[0..99]` 与 `CPersonalityData[256..355]`，无需重启游戏。
- **突破官方限制**：支持全部 126 种引擎真实效果（飞将/洞察/火神/卧龙/强运/动员等）任意双效果组合、无点数与禁用限制、默认无条件常驻且支持自定义光环格数；附带原版正常值对照、6 套神技模板及 `profiles/custom_traits.json` 本地库持久化。

---

## 3. 代码结构索引

| 分层 | 文件 | 职责说明 |
| --- | --- | --- |
| **入口与主界面** | `main.py` | 程序入口，启动 `San14TrainerApp` |
| | `app.py` | 主窗口、基础属性分页、快捷键绑定、日志与回滚管理 |
| | `world_panel.py` | 「武将进阶」与「势力/外交/名品」分页 GUI |
| | `trait_tactic_panel.py` | 「战法/战技」与「个性」双模式编辑面板 GUI |
| | `custom_trait_dialog.py` | 无限制自创个性工坊弹窗 GUI |
| **核心引擎** | `memory_engine.py` | Win32 进程枚举、内存扫描/读写与 Patch 回滚引擎 |
| | `officer_auto_locator.py` | 详情窗口实时对象定位器与五类实体字段表 |
| | `game_world_editor.py` | 全局 GameData 管理器解析（势力/都市/区域/武将/外交/海外/名品） |
| | `officer_live_editor.py` | 内存中武将个性（5 槽）与战法（10 槽）动态校准与读写 |
| | `custom_trait_editor.py` | 自创个性内存/全局文件引擎、126 种效果定义与模板库 |
| | `save_editor.py` | `.s14` 存档解析与武将个性/战法修改器 |
| | `lwc_codec.py` | 光荣 `LWC\x1a` (LS11) 压缩容器纯 Python 编解码器 |
| | `game_catalog.py` | 个性与战法目录数据模型及 JSON 加载器 |
| | `profile_manager.py` | 结构体偏移方案（`profiles/*.json`）管理 |
| | `hotkey_engine.py` | Win32 `RegisterHotKey` 全局热键消息循环管理器 |
| **数据配置** | `profiles/san14pk_heritage.json` | 基础修改器偏移与热键默认方案 |
| | `profiles/san14ce_catalog.json` | 传承版完整内置个性与战法目录 |
| **逆向/诊断脚本** | `officer_probe.py`, `locator_research.py`, `record_layout_probe.py`, `*_trace.py` | 早期只读内存探测与 UI 详情选择器追踪研究工具 |
| **自动化测试** | `tests/test_engine.py` | 内存扫描/读写/回滚、配置往返与 43 项热键解析测试 |
| | `tests/test_actions.py` | 进程识别、自动定位防误写、区域/关隘/部队字段写入、热键自定义测试 |
| | `tests/test_traits_tactics.py` | 目录完整性、LWC 编解码、`.s14` 存档编辑、实时与 GUI 技能面板测试 |
| | `tests/test_world_and_enhancements.py` | 势力批量隔离、外交/名品、武将进阶、自创个性与全局文件实时同步测试 |
| **打包脚本** | `build_exe.bat`, `*.spec` | PyInstaller 单文件打包脚本与 Windows Defender 扫描流程 |

---

## 4. 已验证关键内存偏移速查（`SAN14CE_SC.exe`）

- **主游戏数据管理器**：`mgr = u64[base + 0x2783158]`，玩家势力 ID：`base + 0x27833FC`
  - 势力表：`mgr + 0xDF30`（vtable `0x13AB890`） / 军团表：`mgr + 0xE0D0`（vtable `0x13AB900`）
  - 都市表：`mgr + 0xDD38`（stride `0x168`，vtable `0x13AB6E0`）
  - 关隘表：`mgr + 0xDED8`（stride `0xC0`，vtable `0x13ABC40`）
  - 地区表：`mgr + 0x6CAF0`（stride `0x98`，vtable `0x13AB9D0`）
  - 武将表：`mgr + 0x73A50`（stride `0x228`，vtable `0x13ABBD8`，阵型掩码 `+0x164`，能力上限 `+0x130`）
  - 名品表：`mgr + 0x77B18`（stride `0x80`，vtable `0x13AC170`）
  - 政策表：`mgr + 0x78D50`（stride `0xA8`，vtable `0x13AC2A8`）
  - 个性表：`mgr + 0x7DD18`（stride `0xE8`，vtable `0x13AC240`，自创槽位 ID `256..355`）
  - 海外诸国：`mgr + 0x85A20`（stride `0x30`，vtable `0x13AC4B0`）
- **全局编辑个性（进剧本前）**：
  - 管理器：`u64[base + 0x2148880]`，数组偏移 `+0xB4E18`（stride `0x50`，vtable `0x13A9318`，共 100 槽）
  - 文件：`editpersonalityexceSC.s14`（6300 字节 = 100 × 63 字节）
- **详情 UI 选择器**：`officer (0x14B04F8, +0x178)`、`city (0x14AE3E0, +0x178)`、`area (0x14AD2B0, +0x178)`、`gate (0x14AFB08, +0x188)`、`unit (0x14AD970, +0x178)`
- **异常状态换算**：混乱/挑衅/止步均按 `4 ticks = 1 游戏日` 存储。

---

## 5. 换电脑后开发、测试与打包指引

### ① 首次拉取与日常同步
```bash
# 新电脑首次拉取
git clone https://github.com/masvcj/Sanguo14.git

# 每次开工前同步最新代码
git pull

# 改完代码准备换电脑前推送
git add .
git commit -m "描述本次改动"
git push
```

### ② 运行与测试（当前基准：41 项全通过）
```powershell
# 从源码启动 GUI
python main.py

# 运行全套单元测试
python -m unittest discover -s tests -v
```
> 2026-11-21 验证结果：`Ran 41 tests` —— 40 passed，1 skipped（仅沙箱环境下 `RegisterHotKey` 被系统拒绝时自动跳过；在普通 Windows 桌面环境下 41/41 全部执行）。

### ③ 打包成 EXE 注意事项
- `.gitignore` 已排除 `build/`、`dist/`、`.build-tools/`、`__pycache__/`、`.codex/`。
- `build_exe.bat` 第 5 行目前硬编码了原电脑 Python 路径：
  `C:\Users\User\AppData\Local\Programs\Python\Python314\python.exe`
  如果新电脑的 Python 安装路径不同，运行 `build_exe.bat` 前请把这一行改成新电脑的实际 `python.exe` 路径（首次打包会自动安装 `pyinstaller` 到 `.build-tools/`）。
- 主发布包也可通过 `SAN14CE_Full_Trainer.spec` 或 `build_exe.bat` 构建。
