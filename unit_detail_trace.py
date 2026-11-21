import json
import re
import struct
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from memory_engine import MemoryErrorWin32, ProcessMemory, find_game_processes
from officer_auto_locator import UI_HEAP_POINTER, UI_HEAP_RADIUS

report = Path(sys.executable).parent / "unit_detail_trace.jsonl"
TROOPS = 12000


def scan():
    mem = ProcessMemory()
    try:
        game, = find_game_processes(("SAN14CE_SC.exe",))
        mem.attach(game.pid, game.name, read_only=True)
        root = int(mem.read_value(mem.base_address + UI_HEAP_POINTER, "u64"))
        # Module-vtable addresses share their high five bytes at these RVAs.
        suffixes = {(mem.base_address + rva).to_bytes(8, "little")[3:]
                    for rva in (0x1300000, 0x1600000)}
        signature = re.compile(b"|".join(re.escape(suffix) for suffix in suffixes))
        with report.open("w", encoding="utf-8", buffering=1) as log:
            log.write(json.dumps({"pid": mem.pid, "base": mem.base_address, "heap": root}) + "\n")
            start = monotonic()
            previous = None
            while monotonic() - start < 120:
                references = defaultdict(list)
                candidates = set()
                for base, length in mem.iter_writable_regions(
                    start=root - UI_HEAP_RADIUS, stop=root + UI_HEAP_RADIUS
                ):
                    data = mem.read_bytes(base, length)
                    for match in signature.finditer(data):
                        pos = match.start() - 3
                        if pos < 0 or pos % 8 or pos + 0x180 > len(data):
                            continue
                        rva = struct.unpack_from("<Q", data, pos)[0] - mem.base_address
                        if not 0x1300000 <= rva < 0x1600000:
                            continue
                        for offset in (0x148, 0x178, 0x180, 0x170, 0x150, 0x158, 0x160, 0x168):
                            pointer = struct.unpack_from("<Q", data, pos + offset)[0]
                            # Only consider record-like aligned heap pointers.
                            if not 0x10000000000 <= pointer < 0x60000000000 or pointer % 8:
                                continue
                            references[pointer].append((base + pos, rva, offset))
                            if rva == 0x1471ED0 or 0x14A0000 <= rva <= 0x14D0000:
                                candidates.add(pointer)
                results = []
                for candidate in candidates:
                    try:
                        data = mem.read_bytes(candidate, 0x320)
                    except MemoryErrorWin32:
                        continue
                    hits = [offset for offset in range(0, len(data) - 1, 2)
                            if struct.unpack_from("<H", data, offset)[0] == TROOPS]
                    if hits:
                        results.append({"record": candidate, "hits": hits, "raw": data.hex(),
                                        "refs": references[candidate]})
                results.sort(key=lambda entry: entry["record"])
                key = [(r["record"], r["hits"], r["refs"]) for r in results]
                if key != previous:
                    log.write(json.dumps({"time": round(monotonic() - start, 1), "results": results}) + "\n")
                    previous = key
                sleep(0.5)
    except Exception as exc:
        with report.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"error": str(exc)}) + "\n")
    finally:
        mem.detach()


app = tk.Tk()
app.title("SAN14CE Unit Detail Test")
tk.Label(app, text="Reading the currently open unit detail for up to two minutes.", padx=20, pady=20).pack()
Thread(target=scan, daemon=True).start()
app.after(122000, app.destroy)
app.mainloop()

