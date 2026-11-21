"""内存引擎与偏移方案的自动化测试（在本进程内存缓冲区上验证真实 Win32 API 读写与扫描）。"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hotkey_engine import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, parse_hotkey
from memory_engine import ProcessMemory, list_processes
from profile_manager import TrainerProfile


class TestSan14TrainerEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_path = Path(__file__).resolve().parents[1] / "profiles" / "san14pk_heritage.json"
        self.profile = TrainerProfile.load(self.profile_path)
        self.mem = ProcessMemory()
        self.mem.attach(os.getpid(), "python.exe")

    def tearDown(self) -> None:
        self.mem.detach()

    def test_list_processes_includes_self(self) -> None:
        procs = list_processes()
        pids = {p.pid for p in procs}
        self.assertIn(os.getpid(), pids)

    def test_profile_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "test_profile.json"
            self.profile.tables["officer"].base_address = 0x12345678
            self.profile.save(out)
            reloaded = TrainerProfile.load(out)
            self.assertEqual(reloaded.tables["officer"].base_address, 0x12345678)
            self.assertEqual(reloaded.tables["officer"].fields[0].key, "leadership")

    def test_real_memory_scan_read_write_and_undo(self) -> None:
        # 在本进程内存中构造一块模拟武将结构体（含负偏移出生/卒年 + 五维 + 忠诚 + 功绩）
        buf = (ctypes.c_ubyte * 64)()
        # 出生年 155 (-0x8 -> index 8), 卒年 220 (-0x6 -> index 10)
        anchor_idx = 16
        buf[anchor_idx - 8 : anchor_idx - 6] = (155).to_bytes(2, "little")
        buf[anchor_idx - 6 : anchor_idx - 4] = (220).to_bytes(2, "little")
        # 五维：统率96, 武力72, 智力91, 政治94, 魅力96
        stats = (96, 72, 91, 94, 96)
        for i, val in enumerate(stats):
            buf[anchor_idx + i] = val
        # 忠诚 100 (+0x8)
        buf[anchor_idx + 8] = 100
        # 功绩 5000 (+0x10)
        buf[anchor_idx + 16 : anchor_idx + 20] = (5000).to_bytes(4, "little")

        anchor_addr = ctypes.addressof(buf) + anchor_idx

        # 1. 验证 Win32 内存扫描能定位到该五维特征地址
        hits = self.mem.scan_officer_stats(stats, stat_dtype="u8", max_results=50)
        self.assertIn(anchor_addr, hits)

        # 2. 验证结构体读取
        officer_tbl = self.profile.tables["officer"]
        vals = officer_tbl.read_record(self.mem, anchor_addr)
        self.assertEqual(vals["leadership"], 96)
        self.assertEqual(vals["war"], 72)
        self.assertEqual(vals["birth_year"], 155)
        self.assertEqual(vals["loyalty"], 100)

        # 3. 验证写入与数值范围裁剪（max_val=120）
        officer_tbl.write_record(
            self.mem,
            anchor_addr,
            {"leadership": 150, "war": 110, "birth_year": 180},
            record_label="测试武将",
        )
        updated = officer_tbl.read_record(self.mem, anchor_addr)
        self.assertEqual(updated["leadership"], 120)
        self.assertEqual(updated["war"], 110)
        self.assertEqual(updated["birth_year"], 180)

        # 4. 验证全部撤销恢复原状
        restored_count = self.mem.undo_all()
        self.assertEqual(restored_count, 3)
        reverted = officer_tbl.read_record(self.mem, anchor_addr)
        self.assertEqual(reverted["leadership"], 96)
        self.assertEqual(reverted["war"], 72)
        self.assertEqual(reverted["birth_year"], 155)

    def test_all_43_hotkeys_parse_cleanly(self) -> None:
        total_fields = 0
        for table in self.profile.tables.values():
            for f in table.fields:
                total_fields += 1
                self.assertTrue(f.hotkey, f"Missing hotkey on {f.key}")
                mods, vk = parse_hotkey(f.hotkey)
                self.assertGreater(vk, 0)
                self.assertTrue(mods & MOD_NOREPEAT)
        self.assertEqual(total_fields, 43)

        mods_c, vk_c = parse_hotkey("Ctrl + Num 1")
        self.assertEqual(mods_c, MOD_NOREPEAT | MOD_CONTROL)
        self.assertEqual(vk_c, 0x61)

        mods_a, vk_a = parse_hotkey("Alt + F12")
        self.assertEqual(mods_a, MOD_NOREPEAT | MOD_ALT)
        self.assertEqual(vk_a, 0x7B)


if __name__ == "__main__":
    unittest.main()
