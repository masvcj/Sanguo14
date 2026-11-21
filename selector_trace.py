import json
import tkinter as tk
import sys
import time
from pathlib import Path

from memory_engine import MemoryErrorWin32, ProcessMemory, find_game_processes
from officer_auto_locator import read_selected_officer, supported_executable
from officer_probe import OfficerFollowTarget, foreground_process_id, HERITAGE_OFFICER_STRIDE

folder = Path(sys.executable).parent / "profiles"
output = folder / "selector_trace.jsonl"
research = json.loads((folder / "locator_research.json").read_text(encoding="utf-8"))
paths = [(root, offsets) for chains in research["chains"].values() for root, offsets in chains]
mem = ProcessMemory()


def read(callback, *arguments):
    try:
        return callback(*arguments)
    except (MemoryErrorWin32, OSError, ValueError) as error:
        return str(error)


def selector_state(root, offsets):
    address = mem.base_address + root
    for off in offsets:
        value = mem.read_value(address, "u64")
        if value < 0x10000:
            return [address, value]
        address = value + off
    return [address, mem.read_value(address, "u64")]


def run_trace():
    with output.open("w", encoding="utf-8", buffering=1) as log:
        try:
            game, = find_game_processes(("SAN14CE_SC.exe",))
            mem.attach(game.pid, game.name, read_only=True)
            follow = OfficerFollowTarget.load(mem, folder / "officer_follow_session.json")
            log.write(json.dumps({"pid": mem.pid, "base": mem.base_address,
                                  "supported": supported_executable(mem),
                                  "handles": follow.handles if follow else None}) + "\n")
            end = time.monotonic() + 150
            previous = None
            while time.monotonic() < end:
                current = read(read_selected_officer, mem)
                if isinstance(current, tuple):
                    current = [current[0], list(current[1])]
                tracked = [read(mem.read_value, address, "u64") for address, _ in follow.handles] if follow else []
                snapshot = {"front": foreground_process_id(), "preview": current, "original": tracked,
                            "paths": [read(selector_state, root, offsets) for root, offsets in paths]}
                snapshot.update(inspect_detail(follow.anchor if follow else 0))
                if snapshot != previous:
                    log.write(json.dumps({"time": time.time(), **snapshot}, ensure_ascii=False) + "\n")
                    previous = snapshot
                time.sleep(0.17)
        except Exception as error:
            log.write(json.dumps({"error": str(error)}, ensure_ascii=False) + "\n")
        finally:
            mem.detach()
    
    
def inspect_detail(anchor):
    roots = {}
    for relative in (41771840, 28734688, 28987768):
        roots[str(relative)] = read(mem.read_value, mem.base_address + relative, "u64")
    result = {"roots": roots}
    plausible = {}
    for i, (root, offsets) in enumerate(paths):
        node = read(selector_state, root, offsets)
        if not isinstance(node, list):
            continue
        slot, pointer = node
        if anchor and pointer > 0 and abs(anchor - 292 - pointer) < 1550000 and (anchor - 292 - pointer) % HERITAGE_OFFICER_STRIDE == 0:
            data = read(mem.read_bytes, pointer + 292, 5)
            if isinstance(data, bytes):
                plausible[i] = list(data)
        if i == 2 and slot > 0x10000:
            fields = read(mem.read_bytes, slot - 0x190, 0x240)
            if isinstance(fields, bytes):
                qwords = [int.from_bytes(fields[j:j + 8], "little") for j in range(0, len(fields), 8)]
                result["neighbor"] = [slot, {hex(j * 8 - 0x190): number for j, number in enumerate(qwords)
                                                     if mem.base_address <= number < mem.base_address + mem.module_size
                                                     or 0x10000000000 < number < 0x60000000000}]
    result["plausible"] = plausible
    return result


if __name__ == "__main__":
    from threading import Thread
    app = tk.Tk()
    app.title("SAN14CE Selector Trace")
    tk.Label(app, text="Read-only diagnostics are running for 150 seconds.", padx=20, pady=20).pack()
    Thread(target=run_trace, daemon=True).start()
    app.after(151000, app.destroy)
    app.mainloop()
