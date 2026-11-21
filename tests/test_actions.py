"""Regression checks for hotkey dispatch and failed writes; no game process is changed."""
import ctypes
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app import San14TrainerApp
from hotkey_engine import (
    GlobalHotkeyManager,
    WM_HOTKEY,
    event_to_hotkey,
    format_hotkey_display,
    normalize_hotkey,
    parse_hotkey,
    user32,
)
from memory_engine import MemoryErrorWin32, ProcessInfo, find_game_processes


class ProcessDetectionTests(unittest.TestCase):
    def test_never_detect_trainer_or_launcher_as_game(self):
        with patch('memory_engine.list_processes', return_value=[
            ProcessInfo(1, 'SAN14PK_Heritage_Plus43_Trainer.exe'),
            ProcessInfo(2, 'SAN14CE_Trainer_Diagnostic.exe'),
            ProcessInfo(3, 'SAN14_Launcher.exe'),
        ]):
            self.assertEqual([], find_game_processes())

    def test_heritage_preferred_over_old_pk(self):
        with patch('memory_engine.list_processes', return_value=[
            ProcessInfo(1, 'SAN14PK.exe'), ProcessInfo(2, 'SAN14CE_SC.exe')
        ]):
            self.assertEqual(2, find_game_processes()[0].pid)


class HotkeyTests(unittest.TestCase):
    def test_real_message_loop_dispatch_and_conflict(self):
        first, second = GlobalHotkeyManager(), GlobalHotkeyManager()
        received = threading.Event()
        combo = 'Ctrl + Alt + Shift + F11'
        key = first.register(combo, received.set)
        second.register(combo, lambda: None)
        try:
            first.start()
            if first.failed_hotkeys.get(combo) == 5:
                self.skipTest("Current execution environment denies RegisterHotKey (Windows error 5)")
            self.assertFalse(first.failed_hotkeys)
            second.start()
            self.assertIn(combo, second.failed_hotkeys)
            self.assertTrue(user32.PostThreadMessageW(first._thread_id, WM_HOTKEY, key, 0))
            self.assertTrue(received.wait(1.0))
        finally:
            second.stop()
            first.stop()
        self.assertFalse(first._thread)

    def test_extended_hotkey_parsing_and_normalization(self):
        self.assertEqual("Ctrl + Q", normalize_hotkey("ctrl+q"))
        self.assertEqual("Alt + Shift + 1", normalize_hotkey("shift + alt + 1"))
        self.assertEqual("Ctrl + Num +", normalize_hotkey("ctrl + num +"))
        self.assertEqual("Alt + Num 2", normalize_hotkey("alt + 小键盘 2"))
        self.assertEqual("Ctrl + Insert", normalize_hotkey("ctrl+ins"))
        self.assertEqual("", normalize_hotkey("   "))
        self.assertEqual("Alt + 小键盘 2", format_hotkey_display("Alt + Num 2"))
        self.assertEqual("点击设置", format_hotkey_display("", empty="点击设置"))
        mods, vk = parse_hotkey("Ctrl + Num +")
        self.assertEqual(vk, 0x6B)
        self.assertTrue(mods)

    def test_event_to_hotkey_ignores_modifiers_and_numlock(self):
        self.assertIsNone(event_to_hotkey("Control_L", 0x11, 0x0004))
        self.assertIsNone(event_to_hotkey("Alt_L", 0x12, 0x20000))
        # State bit 0x0008 is NumLock on Windows Tk and must not be treated as Alt.
        self.assertEqual("Num 1", event_to_hotkey("KP_1", 0x61, 0x0008))
        self.assertEqual("Ctrl + Alt + Q", event_to_hotkey("q", 0x51, 0x20004))
        self.assertEqual("Ctrl + Shift + F7", event_to_hotkey("F7", 0x76, 0x0005))


class ActionTests(unittest.TestCase):
    def setUp(self):
        with patch.object(San14TrainerApp, '_register_all_hotkeys'), \
             patch.object(San14TrainerApp, '_auto_find_game'), \
             patch.object(San14TrainerApp, '_schedule_freeze_loop'):
            self.app = San14TrainerApp()
        self.app.sound_enabled.set(False)
        self.app.mem = Mock(is_attached=True)
        self.table = self.app.profile.tables['officer']

    def tearDown(self):
        self.app._on_close()

    def test_unbound_action_does_not_write(self):
        self.app._trigger_field_action('officer', self.table.fields[0])
        self.app.mem.write_value.assert_not_called()
        self.assertIn('未执行修改', self.app.status_var.get())

    def test_failed_toggle_remains_off(self):
        self.table.base_address = 0x10000
        field = next(f for f in self.table.fields if f.mode == 'toggle')
        self.app.mem.write_value.side_effect = MemoryErrorWin32('denied')
        self.app._trigger_field_action('officer', field)
        self.assertFalse(self.app.active_toggles[('officer', field.key)])
        self.assertEqual('关', self.app.trainer_buttons[('officer', field.key)].cget('text'))

    def test_readback_mismatch_is_not_success(self):
        self.table.base_address = 0x10000
        self.app.mem.read_value.return_value = 7
        self.app._trigger_field_action('officer', self.table.fields[0])
        self.assertIn('校验失败', self.app.status_var.get())

    def test_hotkey_event_reaches_action_on_main_thread(self):
        field = self.table.fields[0]
        thread = threading.Thread(target=lambda: self.app._hotkey_events.put(('officer', field)))
        thread.start()
        thread.join()
        self.app._drain_hotkey_events()
        self.assertIn(field.hotkey, self.app.status_var.get())

    def test_automatic_officer_action_rejects_a_changed_target(self):
        self.app.officer_auto_enabled = True
        values = bytes((58, 43, 57, 64, 66))
        self.app.officer_auto_snapshot = (0x10124, values)
        with (
            patch('app.foreground_process_id', return_value=812),
            patch('app.os.getpid', return_value=812),
            patch('app.read_selected_officer', return_value=(0x1034c, values)),
        ):
            self.app._trigger_field_action('officer', self.table.fields[0])
        self.app.mem.write_value.assert_not_called()

    def test_automatic_officer_hotkey_writes_only_the_verified_current_field(self):
        self.app.officer_auto_enabled = True
        self.app.mem.pid = 1234
        self.app.mem.read_value.return_value = 100
        self.app.officer_auto_snapshot = (0x10124, bytes((58, 43, 57, 64, 66)))
        with (
            patch('app.foreground_process_id', return_value=1234),
            patch('app.read_selected_officer', return_value=self.app.officer_auto_snapshot) as locate,
        ):
            self.app._trigger_field_action('officer', self.table.fields[0], from_hotkey=True)
        locate.assert_called_once_with(self.app.mem, reject_hover=True)
        self.assertEqual(self.app.mem.write_value.call_args.args, (0x10124, 'u8', 100))
        self.assertEqual(self.app.officer_auto_snapshot[1], bytes((100, 43, 57, 64, 66)))

    def test_unit_write_uses_the_live_record_and_preserves_adjacent_fields(self):
        self.app.officer_auto_enabled = True
        field = self.app.profile.tables["unit"].fields[1]
        self.app.mem.read_value.return_value = 20000
        selection = (0x20000, bytes((0xE0, 0x2E, 0, 0, 100)))
        self.app.location_snapshots["unit"] = selection
        with (
            patch('app.foreground_process_id', return_value=812),
            patch('app.os.getpid', return_value=812),
            patch('app.read_live_targets', return_value={"unit": selection}),
        ):
            self.app._trigger_field_action("unit", field)
        self.assertEqual(self.app.mem.write_value.call_args.args, (0x20016, "u16", 20000))

    def test_unit_write_is_rejected_after_the_details_close(self):
        self.app.officer_auto_enabled = True
        self.app.location_snapshots["unit"] = (0x20000, bytes((0xE0, 0x2E, 0, 0, 100)))
        field = self.app.profile.tables["unit"].fields[0]
        with (
            patch('app.foreground_process_id', return_value=812),
            patch('app.os.getpid', return_value=812),
            patch('app.read_live_targets', return_value={}),
        ):
            self.app._trigger_field_action("unit", field)
        self.app.mem.write_value.assert_not_called()

    def test_unit_status_days_and_cargo_write_to_verified_live_offsets(self):
        self.app.officer_auto_enabled = True
        unit_fields = {f.key: f for f in self.app.profile.tables["unit"].fields}
        snap = (0x20000, bytes((0xE8, 0x03, 0, 0, 100, 60, 40, 20)) + (6500).to_bytes(4, "little") + (19133).to_bytes(4, "little"))
        self.app.location_snapshots["unit"] = snap

        cases = [
            ("unit_provoked", "0", 0x20025, "u8", 0),
            ("unit_disorder", "15", 0x20023, "u8", 60),
            ("unit_restrained", "90", 0x20024, "u8", 252),  # clamped to 63 days * 4 ticks
            ("unit_gold", "50000", 0x2005C, "u32", 50000),
            ("unit_food", "80000", 0x20060, "u32", 80000),
        ]
        for key, input_text, expected_addr, expected_dtype, expected_raw in cases:
            self.app.mem.write_value.reset_mock()
            self.app.trainer_inputs[("unit", key)].set(input_text)
            self.app.mem.read_value.return_value = expected_raw
            with (
                patch('app.foreground_process_id', return_value=812),
                patch('app.os.getpid', return_value=812),
                patch('app.read_live_targets', return_value={"unit": snap}),
            ):
                self.app._trigger_field_action("unit", unit_fields[key])
            self.assertEqual(self.app.mem.write_value.call_args.args, (expected_addr, expected_dtype, expected_raw))

    def test_area_all_six_fields_write_to_verified_live_offsets(self):
        import struct
        self.app.officer_auto_enabled = True
        area_fields = {f.key: f for f in self.app.profile.tables["area"].fields}
        raw = struct.pack("<HBHHHB", 19290, 80, 120, 350, 610, 11)
        snap = (0x40000, raw)
        self.app.location_snapshots["area"] = snap
        self.assertEqual(
            self.app._format_location_status("area", raw),
            "商120/农350/兵610 · 人19290/治80/建11",
        )

        cases = [
            ("area_commerce", "1000", 0x4003C, "u16", 1000),
            ("area_agri", "1500", 0x4003E, "u16", 1000),      # clamped to game max 1000 (Lv.5 MAX)
            ("area_barracks", "600", 0x40040, "u16", 600),
            ("area_pop", "50000", 0x40052, "u16", 50000),
            ("area_order", "100", 0x40054, "u8", 100),
            ("area_build_cap", "30", 0x4006E, "u8", 30),
        ]
        for key, input_text, expected_addr, expected_dtype, expected_raw in cases:
            self.app.mem.write_value.reset_mock()
            self.app.trainer_inputs[("area", key)].set(input_text)
            self.app.mem.read_value.return_value = expected_raw
            with (
                patch('app.foreground_process_id', return_value=812),
                patch('app.os.getpid', return_value=812),
                patch('app.read_live_targets', return_value={"area": snap}),
            ):
                self.app._trigger_field_action("area", area_fields[key])
            self.assertEqual(self.app.mem.write_value.call_args.args, (expected_addr, expected_dtype, expected_raw))

    def test_gate_all_six_fields_write_to_verified_live_offsets(self):
        import struct
        self.app.officer_auto_enabled = True
        gate_fields = {f.key: f for f in self.app.profile.tables["gate"].fields}
        raw = struct.pack("<IIBBHBBBB", 3000, 120, 100, 0, 6000, 14, 12, 0, 8)
        snap = (0x30000, raw)
        self.app.location_snapshots["gate"] = snap
        self.assertEqual(
            self.app._format_location_status("gate", raw),
            "耐久 6000 / 士气 100 / 士兵 3000 / 伤兵 120 · 混乱3天 挑衅2天",
        )

        cases = [
            ("gate_durability", "10000", 0x30046, "u16", 10000),
            ("gate_morale", "150", 0x30044, "u8", 150),
            ("gate_troops", "50000", 0x3003C, "u32", 50000),
            ("gate_wounded", "2500", 0x30040, "u32", 2500),
            ("gate_provoked", "10", 0x3004B, "u8", 40),
            ("gate_disorder", "90", 0x30049, "u8", 252),  # clamped to 63 days * 4 ticks
        ]
        for key, input_text, expected_addr, expected_dtype, expected_raw in cases:
            self.app.mem.write_value.reset_mock()
            self.app.trainer_inputs[("gate", key)].set(input_text)
            self.app.mem.read_value.return_value = expected_raw
            with (
                patch('app.foreground_process_id', return_value=812),
                patch('app.os.getpid', return_value=812),
                patch('app.read_live_targets', return_value={"gate": snap}),
            ):
                self.app._trigger_field_action("gate", gate_fields[key])
            self.assertEqual(self.app.mem.write_value.call_args.args, (expected_addr, expected_dtype, expected_raw))

    def test_gate_write_is_rejected_after_details_close(self):
        import struct
        self.app.officer_auto_enabled = True
        self.app.location_snapshots["gate"] = (0x30000, struct.pack("<IIBBHBBBB", 3000, 0, 100, 0, 6000, 14, 0, 0, 0))
        field = self.app.profile.tables["gate"].fields[0]
        with (
            patch('app.foreground_process_id', return_value=812),
            patch('app.os.getpid', return_value=812),
            patch('app.read_live_targets', return_value={}),
        ):
            self.app._trigger_field_action("gate", field)
        self.app.mem.write_value.assert_not_called()

    def test_custom_hotkey_edit_conflict_persistence_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.app.hotkey_config_path = Path(tmp) / "custom_hotkeys.json"
            gate_dur = self.app.profile.tables["gate"].fields[0]
            gate_morale = self.app.profile.tables["gate"].fields[1]
            orig_dur = gate_dur.hotkey
            orig_morale = gate_morale.hotkey

            with patch.object(self.app, "_register_all_hotkeys") as reg:
                self.app._set_field_hotkey("gate", gate_dur, "ctrl+q")
                reg.assert_called_once()
            self.assertEqual(gate_dur.hotkey, "Ctrl + Q")
            self.assertEqual(self.app.hotkey_buttons[("gate", gate_dur.key)].cget("text"), "Ctrl + Q")
            self.assertTrue(self.app.hotkey_config_path.is_file())

            # Assigning gate_morale's hotkey to gate_dur clears gate_morale automatically.
            with patch.object(self.app, "_register_all_hotkeys"):
                cleared = self.app._set_field_hotkey("gate", gate_dur, orig_morale)
            self.assertIsNotNone(cleared)
            self.assertEqual(cleared[1].key, gate_morale.key)
            self.assertEqual(gate_dur.hotkey, orig_morale)
            self.assertEqual(gate_morale.hotkey, "")
            self.assertEqual(self.app.hotkey_buttons[("gate", gate_morale.key)].cget("text"), "点击设置")

            # Reloading from disk reproduces both customized and cleared entries.
            gate_dur.hotkey = orig_dur
            gate_morale.hotkey = orig_morale
            self.app._load_custom_hotkeys()
            self.assertEqual(gate_dur.hotkey, orig_morale)
            self.assertEqual(gate_morale.hotkey, "")

            # Resetting all hotkeys restores defaults and cleans up the custom file.
            with patch.object(self.app, "_register_all_hotkeys"):
                self.app._reset_all_hotkeys(confirm=False)
            self.assertEqual(gate_dur.hotkey, orig_dur)
            self.assertEqual(gate_morale.hotkey, orig_morale)
            self.assertFalse(self.app.hotkey_config_path.exists())


if __name__ == '__main__':
    unittest.main()
