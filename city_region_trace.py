import json
import re
import struct
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from threading import Thread
from time import monotonic

from memory_engine import MemoryErrorWin32, ProcessMemory, SCAN_CHUNK_SIZE, find_game_processes

folder = Path(sys.executable).parent / "profiles"
report = folder / "city_region_layouts.json"
samples = {"city_a": [78000, 15000, 39000, 2000, 100],
           "city_b": [81000, 13500, 42000, 4000, 100],
           "region_a": [15310, 70, 17],
           "region_b": [11790, 50, 11]}


def scan():
    mem = ProcessMemory()
    try:
        game, = find_game_processes(("SAN14CE_SC.exe",))
        mem.attach(game.pid, game.name, read_only=True)
        seeds = {(values[0].to_bytes(4 if kind.startswith("city") else 2, "little")): kind
                 for kind, values in samples.items()}
        expression = re.compile(b"|".join(map(re.escape, seeds)))
        hits = {kind: defaultdict(list) for kind in samples}
        max_window = 128
        deadline = monotonic() + 60
        done = True
        for base, size in mem.iter_writable_regions():
            if monotonic() >= deadline:
                done = False
                break
            for off in range(0, size, SCAN_CHUNK_SIZE):
                if monotonic() >= deadline:
                    done = False
                    break
                block = min(size - off, SCAN_CHUNK_SIZE)
                try:
                    data = mem.read_bytes(base + off, min(size - off, block + max_window))
                except MemoryErrorWin32:
                    continue
                for match in expression.finditer(data):
                    kind = seeds[match.group()]
                    city = kind.startswith("city")
                    index = match.start()
                    if index % (4 if city else 2) or index >= block:
                        continue
                    lower, upper = index - max_window, index + max_window
                    if lower < 0 or upper >= len(data):
                        try:
                            window = mem.read_bytes(base + off + index - max_window, max_window * 2)
                        except MemoryErrorWin32:
                            continue
                    else:
                        window = data[lower:upper]
                    values = samples[kind][1:]
                    widths = ((4,), (4,), (2, 4), (1, 2)) if city else ((1, 2), (1, 2))
                    matches = []
                    for value, possible in zip(values, widths):
                        positions = set()
                        for width in possible:
                            pattern = value.to_bytes(width, "little")
                            start = window.find(pattern)
                            while start != -1:
                                if (start < max_window or start >= max_window + len(match.group())) and start % width == 0:
                                    positions.add(start - max_window)
                                start = window.find(pattern, start + 1)
                        if not positions or len(positions) > 64:
                            break
                        matches.append(positions)
                    if len(matches) != len(values):
                        continue
                    if city:
                        from itertools import product
                        for layout in product(*matches):
                            if len(set(layout)) == len(layout):
                                hits[kind][layout].append(base + off + index)
                    else:
                        for order in matches[0]:
                            for construction in matches[1]:
                                if order != construction:
                                    hits[kind][(order, construction)].append(base + off + index)
        result = {"done": done, "matches": {}}
        for family in ("city", "region"):
            first, second = hits[family + "_a"], hits[family + "_b"]
            shared = []
            for layout in first.keys() & second.keys():
                pairs = [(a, b) for a in set(first[layout]) for b in set(second[layout]) if 0 < abs(a - b) <= 0x20000]
                if pairs:
                    shared.append([layout, pairs[:25]])
            result["matches"][family] = sorted(shared, key=lambda item: (len(item[1]), sum(map(abs, item[0]))))[:250]
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as error:
        report.write_text(json.dumps({"error": str(error)}), encoding="utf-8")
    finally:
        mem.detach()


app = tk.Tk()
app.title("SAN14CE City and Region Read Test")
tk.Label(app, text="Read-only layout comparison is running.", padx=20, pady=20).pack()
Thread(target=scan, daemon=True).start()
app.after(65000, app.destroy)
app.mainloop()

