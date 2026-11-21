import json
import re
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from memory_engine import MemoryErrorWin32, ProcessMemory, find_game_processes
from officer_probe import OfficerFollowTarget, foreground_process_id, HERITAGE_OFFICER_STRIDE

folder = Path(sys.executable).parent / "profiles"


def scan():
    mem = ProcessMemory()
    try:
        game, = find_game_processes(("SAN14CE_SC.exe",))
        mem.attach(game.pid, game.name, read_only=True)
        follow = OfficerFollowTarget.load(mem, folder / "officer_follow_session.json")
        expected = {b"\x69\x4d\x48\x39\x38": "A", b"\x3a\x2b\x39\x40\x42": "B", b"\x50\x54\x35\x33\x41": "C"}
        addresses = {}
        for base, size in mem.iter_writable_regions():
            if not base <= follow.anchor < base + size:
                continue
            start = max(base, follow.anchor - 600000)
            end = min(base + size, follow.anchor + 600000)
            data = mem.read_bytes(start, end - start)
            for values, label in expected.items():
                for match in re.finditer(re.escape(values), data):
                    addr = start + match.start()
                    if (addr - follow.anchor) % HERITAGE_OFFICER_STRIDE == 0:
                        addresses[label] = addr - 292
        patterns = {address.to_bytes(8, "little"): label for label, address in addresses.items()}
        matches = re.compile(b"|".join(map(re.escape, patterns)))
        start_heap = min(slot for slot, off in follow.handles) - 0x300000
        end_heap = max(slot for slot, off in follow.handles) + 0x300000
        regions = [(max(base, start_heap), min(base + size, end_heap)) for base, size in mem.iter_writable_regions()
                   if max(base, start_heap) < min(base + size, end_heap)]
        with (folder / "detail_heap_trace.jsonl").open("w", encoding="utf-8", buffering=1) as file:
            file.write(json.dumps({"addresses": addresses, "regions": regions, "base": mem.base_address}) + "\n")
            previous = {}
            deadline = monotonic() + 110
            start = monotonic()
            while monotonic() < deadline and patterns:
                active = defaultdict(list)
                for lower, higher in regions:
                    try:
                        data = mem.read_bytes(lower, higher - lower)
                    except MemoryErrorWin32:
                        continue
                    for found in matches.finditer(data):
                        pos = found.start()
                        if (lower + pos) % 8:
                            continue
                        types = {}
                        for j in range(max(0, pos - 0x400), pos, 8):
                            value = int.from_bytes(data[j:j + 8], "little")
                            if mem.base_address < value < mem.base_address + mem.module_size:
                                types[hex(pos - j)] = hex(value - mem.base_address)
                        active[patterns[found.group()]].append([lower + pos, types])
                if active != previous:
                    file.write(json.dumps({"time": round(monotonic() - start, 1), "front": foreground_process_id(),
                                           "matches": active}) + "\n")
                    previous = active
                sleep(0.35)
    except Exception as error:
        (folder / "detail_heap_error.txt").write_text(str(error), encoding="utf-8")
    finally:
        mem.detach()


app = tk.Tk()
app.title("SAN14CE Officer Detail Test")
tk.Label(app, text="Read-only diagnostics are running for 110 seconds.", padx=20, pady=20).pack()
Thread(target=scan, daemon=True).start()
app.after(111000, app.destroy)
app.mainloop()

