import json
import re
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from memory_engine import MemoryErrorWin32, ProcessMemory, find_game_processes
from officer_auto_locator import UI_HEAP_POINTER, UI_HEAP_RADIUS

folder = Path(sys.executable).parent / "profiles"


def follow():
    mem = ProcessMemory()
    path = folder / "city_region_selectors.jsonl"
    try:
        game, = find_game_processes(("SAN14CE_SC.exe",))
        mem.attach(game.pid, game.name, read_only=True)
        layouts = json.loads((folder / "city_region_layouts.json").read_text(encoding="utf-8"))["matches"]
        records = {family + str(i): seed for family, entries in layouts.items()
                   for i, seed in enumerate(entries[0][1][0])}
        lookup = {}
        for label, seed in records.items():
            for offset in range(-160, 48, 2):
                lookup[(seed + offset).to_bytes(8, "little")] = (label, offset)
        expression = re.compile(b"|".join(re.escape(value) for value in lookup))
        expected_start = int(mem.read_value(mem.base_address + UI_HEAP_POINTER, "u64"))
        with path.open("w", encoding="utf-8", buffering=1) as file:
            file.write(json.dumps({"records": records}) + "\n")
            previous = None
            start = monotonic()
            while monotonic() - start < 150:
                hits = defaultdict(list)
                for base, length in mem.iter_writable_regions(start=expected_start - UI_HEAP_RADIUS,
                                                              stop=expected_start + UI_HEAP_RADIUS):
                    try:
                        data = mem.read_bytes(base, length)
                    except MemoryErrorWin32:
                        continue
                    for match in expression.finditer(data):
                        pos = match.start()
                        if (base + pos) % 8:
                            continue
                        label, offset = lookup[match.group()]
                        types = {}
                        for at in range(max(0, pos - 0x1e0), pos, 8):
                            value = int.from_bytes(data[at:at + 8], "little")
                            if mem.base_address <= value < mem.base_address + mem.module_size:
                                types[hex(pos - at)] = hex(value - mem.base_address)
                        hits[label].append([hex(base + pos), offset, types])
                if hits != previous:
                    file.write(json.dumps({"time": round(monotonic() - start, 1), "hits": hits}) + "\n")
                    previous = hits
                sleep(0.35)
    except Exception as error:
        path.write_text(json.dumps({"error":str(error)}), encoding="utf-8")
    finally:
        mem.detach()


app = tk.Tk()
app.title("SAN14CE City and Region Selection Test")
tk.Label(app, text="Read-only scene comparison is running.", padx=20, pady=20).pack()
Thread(target=follow, daemon=True).start()
app.after(151000, app.destroy)
app.mainloop()

