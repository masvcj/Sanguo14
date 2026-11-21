"""Comprehensive tests for LWC codec, catalog, .s14 save editor, live officer editor, and GUI panels."""

from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import San14TrainerApp
from game_catalog import GameCatalog, REFERENCE_OFFICERS
from lwc_codec import LwcFormatError, lwc_compress, lwc_decompress
from memory_engine import MemoryErrorWin32
from officer_live_editor import (
    calibrate_live_layout_from_record,
    list_live_officers,
    read_live_officer,
    scan_live_officer_layout,
    write_live_tactics,
    write_live_traits,
)
from officer_probe import HERITAGE_OFFICER_STRIDE, HERITAGE_STATS_OFFSET
from save_editor import (
    OFF_GIVEN,
    OFF_PINYIN,
    OFF_STATS,
    OFF_STATS_COMP,
    OFF_SURNAME,
    OFF_TACTICS,
    OFF_TRAITS,
    OFF_ZI,
    SAVE_MAGIC,
    SaveDocument,
)


class FakeMemory:
    """In-memory ProcessMemory double backed by a writable bytearray region."""

    def __init__(self, base: int, buf: bytearray, *, pid: int = 4242) -> None:
        self.base = base
        self.buf = buf
        self.pid = pid
        self.process_name = "SAN14CE_SC.exe"
        self.handle = 1
        self.base_address = 0x140000000
        self.is_attached = True
        self.patch_history = []

    def detach(self) -> None:
        self.is_attached = False

    def iter_writable_regions(self, *, start=None, stop=None):
        lo = self.base if start is None else max(self.base, start)
        hi = self.base + len(self.buf) if stop is None else min(self.base + len(self.buf), stop)
        if lo < hi:
            yield (lo, hi - lo)

    def read_bytes(self, address: int, size: int) -> bytes:
        off = address - self.base
        if off < 0 or off + size > len(self.buf):
            raise MemoryErrorWin32(f"out of bounds read {hex(address)}")
        return bytes(self.buf[off : off + size])

    def write_bytes(self, address: int, data: bytes, record_patch: bool = True, label: str = "") -> None:
        off = address - self.base
        if off < 0 or off + len(data) > len(self.buf):
            raise MemoryErrorWin32(f"out of bounds write {hex(address)}")
        orig = bytes(self.buf[off : off + len(data)])
        self.buf[off : off + len(data)] = data
        if record_patch:
            self.patch_history.append((address, orig, bytes(data), label))

    def scan_pattern(self, pattern: bytes, max_results: int = 200, align: int = 1):
        hits = []
        pos = 0
        while len(hits) < max_results:
            pos = self.buf.find(pattern, pos)
            if pos < 0:
                break
            addr = self.base + pos
            if addr % align == 0:
                hits.append(addr)
            pos += 1
        return hits


def _put_utf16(buf: bytearray, offset: int, text: str, max_bytes: int) -> None:
    enc = text.encode("utf-16le")[: max_bytes - 2] + b"\x00\x00"
    buf[offset : offset + len(enc)] = enc


def _build_synthetic_save(version: int = 0x66, stride: int = 0x1CA, officer_count: int = 600) -> bytes:
    header = bytearray(0x12F if version == 0x66 else 0x126)
    struct.pack_into("<I", header, 0, version)
    header[4:20] = SAVE_MAGIC
    _put_utf16(header, 0x14, "曹操", 36)
    _put_utf16(header, 0x38, "河北平定战", 60)
    struct.pack_into("<8H", header, 0x74, 2026, 11, 0, 19, 15, 0, 0, 0)
    _put_utf16(header, 0x9A, "曹操军", 34)

    prefix = bytearray(0x100)
    struct.pack_into("<II", prefix, 0xF8, 0x0A, officer_count)
    table = bytearray(officer_count * stride)
    # Officer 0 = 无效
    _put_utf16(table, OFF_SURNAME, "无效", 18)

    # Populate our reference officers at indices 1..len(REFERENCE_OFFICERS)
    for idx, ref in enumerate(REFERENCE_OFFICERS, start=1):
        base = idx * stride
        _put_utf16(table, base + OFF_SURNAME, ref["surname"], 18)
        _put_utf16(table, base + OFF_GIVEN, ref["given"], 18)
        _put_utf16(table, base + OFF_ZI, ref["zi"], 18)
        _put_utf16(table, base + OFF_PINYIN, f"Ref{idx}", 36)
        stats = (95, 90, 85, 80, 75)
        table[base + OFF_STATS : base + OFF_STATS + 5] = bytes(stats)
        table[base + OFF_STATS_COMP : base + OFF_STATS_COMP + 5] = bytes(100 - s for s in stats)
        struct.pack_into("<5H", table, base + OFF_TRAITS, *ref["traits"])
        tacs = list(ref["tactics"]) + [0] * (10 - len(ref["tactics"]))
        table[base + OFF_TACTICS : base + OFF_TACTICS + 10] = bytes(tacs)

    suffix = struct.pack("<IIII", 0x0B, 0x15, 0x0C, 0xC9) + b"\x00" * 64
    raw = bytes(prefix) + bytes(table) + suffix
    return bytes(header) + lwc_compress(raw)


def _build_live_officer_memory(
    *,
    traits_off: int = 0x132,
    tactics_off: int = 0x148,
    tactic_dtype: str = "u8",
    surname_off: int = 0x18,
    given_off: int = 0x2A,
    zi_off: int = 0x3C,
    num_officers: int = 40,
) -> tuple[FakeMemory, int]:
    base = 0x24000000000
    table_off = 0x1000
    buf = bytearray(table_off + num_officers * HERITAGE_OFFICER_STRIDE + 0x1000)

    for idx in range(num_officers):
        rec_off = table_off + idx * HERITAGE_OFFICER_STRIDE
        if idx == 0:
            _put_utf16(buf, rec_off + surname_off, "无效", 18)
            continue
        if idx <= len(REFERENCE_OFFICERS):
            ref = REFERENCE_OFFICERS[idx - 1]
            _put_utf16(buf, rec_off + surname_off, ref["surname"], 18)
            _put_utf16(buf, rec_off + given_off, ref["given"], 18)
            _put_utf16(buf, rec_off + zi_off, ref["zi"], 18)
            buf[rec_off + HERITAGE_STATS_OFFSET : rec_off + HERITAGE_STATS_OFFSET + 5] = bytes((96, 88, 84, 79, 91))
            struct.pack_into("<5H", buf, rec_off + traits_off, *ref["traits"])
            tacs = list(ref["tactics"]) + [0] * (10 - len(ref["tactics"]))
            if tactic_dtype == "u8":
                buf[rec_off + tactics_off : rec_off + tactics_off + 10] = bytes(tacs)
            else:
                struct.pack_into("<10H", buf, rec_off + tactics_off, *tacs)
        else:
            _put_utf16(buf, rec_off + surname_off, "张", 18)
            _put_utf16(buf, rec_off + given_off, f"甲{idx}", 18)
            buf[rec_off + HERITAGE_STATS_OFFSET : rec_off + HERITAGE_STATS_OFFSET + 5] = bytes((70, 70, 70, 70, 70))
            struct.pack_into("<5H", buf, rec_off + traits_off, 4, 5, 0, 0, 0)
            if tactic_dtype == "u8":
                buf[rec_off + tactics_off : rec_off + tactics_off + 2] = bytes((1, 2))
            else:
                struct.pack_into("<2H", buf, rec_off + tactics_off, 1, 2)

    return FakeMemory(base, buf), base + table_off


class LwcCodecTests(unittest.TestCase):
    def test_roundtrip_mixed_and_repetitive_payload(self):
        hdr = b"HEADER_TEST_1234"
        payload = (b"\x00" * 500) + ("曹操吕布诸葛亮" * 40).encode("utf-16le") + bytes(range(256)) * 5
        packed = hdr + lwc_compress(payload)
        got_hdr, got_payload = lwc_decompress(packed)
        self.assertEqual(hdr, got_hdr)
        self.assertEqual(payload, got_payload)

    def test_missing_magic_raises(self):
        with self.assertRaises(LwcFormatError):
            lwc_decompress(b"not an s14 file")


class CatalogTests(unittest.TestCase):
    def test_bundled_catalog_has_all_heritage_traits_and_tactics(self):
        cat = GameCatalog.load_default()
        self.assertEqual(218, len(cat.traits))
        self.assertEqual(178, len(cat.tactics))
        self.assertEqual("奸雄", cat.trait_name(14))
        self.assertEqual("金色", cat.trait_by_id[14].color_label)
        self.assertEqual("火神", cat.trait_name(53))
        self.assertEqual("飞将", cat.trait_name(67))
        self.assertEqual("神将", cat.trait_name(150))
        self.assertEqual("卧龙", cat.trait_name(151))
        self.assertEqual("天下无双", cat.tactic_name(93))
        self.assertEqual("魏武之强", cat.tactic_name(94))
        self.assertEqual("战车", cat.tactic_name(133))
        self.assertEqual("战象突击", cat.tactic_name(136))
        self.assertTrue(any(t.id == 14 for t in cat.search_traits("Jianxiong", color_filter=3)))
        self.assertTrue(any(k.id == 93 for k in cat.search_tactics("无双", tier_filter="固有")))


class SaveEditorTests(unittest.TestCase):
    def test_load_edit_save_and_backup_ce_and_pk(self):
        for version, stride in ((0x66, 0x1CA), (0x5C, 0x1BC)):
            with tempfile.TemporaryDirectory() as tmpdir:
                save_path = Path(tmpdir) / f"test_{hex(version)}.s14"
                original_blob = _build_synthetic_save(version=version, stride=stride)
                save_path.write_bytes(original_blob)

                doc = SaveDocument.load(save_path)
                self.assertEqual(stride, doc.stride)
                self.assertEqual("曹操", doc.metadata.ruler)
                self.assertEqual("河北平定战", doc.metadata.scenario)

                caocao = doc.search_officers("曹操")[0]
                self.assertEqual((14, 59, 27, 32, 49), caocao.traits)
                self.assertEqual((94, 3, 10, 15, 18, 23, 0, 0, 0, 0), caocao.tactics)

                doc.set_officer_traits(caocao.index, [14, 67, 150, 151, 53])
                doc.set_officer_tactics(caocao.index, [94, 93, 97, 99, 91, 2, 0, 0, 0, 0])
                doc.save()

                bak_path = save_path.with_suffix(".s14.bak")
                self.assertTrue(bak_path.is_file())
                self.assertEqual(original_blob, bak_path.read_bytes())

                reloaded = SaveDocument.load(save_path)
                cc2 = reloaded.search_officers("曹操")[0]
                self.assertEqual((14, 67, 150, 151, 53), cc2.traits)
                self.assertEqual((94, 93, 97, 99, 91, 2, 0, 0, 0, 0), cc2.tactics)


class LiveOfficerEditorTests(unittest.TestCase):
    def test_calibrate_read_write_u16_traits_and_u8_tactics(self):
        mem, table_start = _build_live_officer_memory(traits_off=0x132, tactics_off=0x148, tactic_dtype="u8")
        caocao_addr = table_start + HERITAGE_OFFICER_STRIDE
        layout = calibrate_live_layout_from_record(mem, caocao_addr)

        self.assertEqual(0x132, layout.traits_offset)
        self.assertEqual("u16", layout.trait_dtype)
        self.assertEqual(0x148, layout.tactics_offset)
        self.assertEqual("u8", layout.tactic_dtype)
        self.assertEqual(0x18, layout.surname_offset)
        self.assertEqual(0x2A, layout.given_offset)
        self.assertGreaterEqual(layout.verified_matches, 10)

        rec = read_live_officer(mem, layout, caocao_addr)
        self.assertEqual("曹操", rec.full_name)
        self.assertEqual((14, 59, 27, 32, 49), rec.traits)
        self.assertEqual((94, 3, 10, 15, 18, 23, 0, 0, 0, 0), rec.tactics)

        write_live_traits(mem, layout, caocao_addr, [14, 67, 150, 151, 53])
        write_live_tactics(mem, layout, caocao_addr, [94, 93, 97, 99, 91, 0, 0, 0, 0, 0])

        rec2 = read_live_officer(mem, layout, caocao_addr)
        self.assertEqual((14, 67, 150, 151, 53), rec2.traits)
        self.assertEqual((94, 93, 97, 99, 91, 0, 0, 0, 0, 0), rec2.tactics)
        self.assertEqual(2, len(mem.patch_history))

        all_officers = list_live_officers(mem, layout)
        self.assertGreaterEqual(len(all_officers), len(REFERENCE_OFFICERS))

    def test_scan_full_table_handles_u16_tactics_too(self):
        mem, table_start = _build_live_officer_memory(traits_off=0x12E, tactics_off=0x150, tactic_dtype="u16")
        with patch("officer_live_editor.supported_executable", return_value=False):
            layout = scan_live_officer_layout(mem)
        self.assertEqual(0x12E, layout.traits_offset)
        self.assertEqual(0x150, layout.tactics_offset)
        self.assertEqual("u16", layout.tactic_dtype)
        lubu_addr = table_start + 2 * HERITAGE_OFFICER_STRIDE
        lubu = read_live_officer(mem, layout, lubu_addr)
        self.assertEqual("吕布", lubu.full_name)
        self.assertEqual((67, 62, 1, 60, 41), lubu.traits)


class SkillPanelGuiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(San14TrainerApp, "_register_all_hotkeys"), \
             patch.object(San14TrainerApp, "_auto_find_game"), \
             patch.object(San14TrainerApp, "_schedule_freeze_loop"):
            self.app = San14TrainerApp()
        self.app.sound_enabled.set(False)

    def tearDown(self):
        self.app._on_close()

    def test_traits_and_tactics_save_mode_workflow(self):
        ctrl = self.app.skill_controller
        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = Path(tmpdir) / "svdexceccSC00.s14"
            save_path.write_bytes(_build_synthetic_save())
            doc = SaveDocument.load(save_path)

            ctrl.save_doc = doc
            ctrl.mode_var.set("save")
            ctrl._on_mode_changed()
            caocao = doc.search_officers("曹操")[0]
            ctrl.current_save_officer = caocao
            ctrl._load_officer_into_draft(caocao)

            # Replace slot 1 (index 1) trait with 飞将 (67)
            ctrl.trait_slot_tree.selection_set("1")
            ctrl.trait_catalog_tree.selection_set("67")
            ctrl._assign_selected_trait(add_to_empty=False)
            ctrl.apply_traits()
            self.assertEqual(67, ctrl.current_save_officer.traits[1])

            # Add 天下无双 (93) to first empty tactic slot (index 6)
            ctrl.tactic_catalog_tree.selection_set("93")
            ctrl._assign_selected_tactic(add_to_empty=True)
            ctrl.apply_tactics()
            self.assertEqual(93, ctrl.current_save_officer.tactics[6])

            # Save and reload
            doc.save()
            reloaded = SaveDocument.load(save_path)
            cc2 = reloaded.search_officers("曹操")[0]
            self.assertEqual(67, cc2.traits[1])
            self.assertEqual(93, cc2.tactics[6])

    def test_live_mode_apply_writes_to_memory(self):
        ctrl = self.app.skill_controller
        mem, table_start = _build_live_officer_memory()
        self.app.mem = mem
        caocao_addr = table_start + HERITAGE_OFFICER_STRIDE
        layout = calibrate_live_layout_from_record(mem, caocao_addr)
        officer = read_live_officer(mem, layout, caocao_addr)

        ctrl.mode_var.set("live")
        ctrl.live_layout = layout
        ctrl.live_officers = [officer]
        ctrl.current_live_officer = officer
        ctrl._load_officer_into_draft(officer)

        ctrl.trait_slot_tree.selection_set("0")
        ctrl.trait_catalog_tree.selection_set("151")
        ctrl._assign_selected_trait(add_to_empty=False)
        ctrl.apply_traits()

        ctrl.tactic_slot_tree.selection_set("0")
        ctrl.tactic_catalog_tree.selection_set("93")
        ctrl._assign_selected_tactic(add_to_empty=False)
        ctrl.apply_tactics()

        updated = read_live_officer(mem, layout, caocao_addr)
        self.assertEqual(151, updated.traits[0])
        self.assertEqual(93, updated.tactics[0])


if __name__ == "__main__":
    unittest.main()
