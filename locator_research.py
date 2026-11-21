"""Read-only search for game-module paths to the validated officer selector."""

import json
from pathlib import Path
import re
import sys
from time import monotonic

from memory_engine import MemoryErrorWin32, ProcessMemory, SCAN_CHUNK_SIZE, find_game_processes
from officer_probe import OfficerFollowTarget


def incoming_references(mem, regions, targets, radius, deadline):
    suffix_map = {}
    for target in targets:
        first = max(0, target - radius) >> 16
        last = (target + radius) >> 16
        for high in range(first, last + 1):
            suffix_map.setdefault(high.to_bytes(6, "little"), []).append(target)
    pattern = re.compile(b"|".join(re.escape(key) for key in suffix_map))
    matches = {target: [] for target in targets}
    for base, size in regions:
        for relative in range(0, size, SCAN_CHUNK_SIZE):
            if monotonic() > deadline:
                return matches, False
            chunk_base = base + relative
            block_size = min(SCAN_CHUNK_SIZE, size - relative)
            try:
                data = mem.read_bytes(chunk_base, min(block_size + 7, size - relative))
            except MemoryErrorWin32:
                continue
            cursor = 2
            while match := pattern.search(data, cursor):
                index = match.start() - 2
                cursor = match.start() + 1
                address = chunk_base + index
                if index >= block_size or address % 8:
                    continue
                value = int.from_bytes(data[index:index + 8], "little")
                for target in suffix_map[match.group()]:
                    delta = target - value
                    if abs(delta) <= radius and len(matches[target]) < 5000:
                        matches[target].append((address, delta))
    return matches, True


def find_module_chains(mem, selector_slots, progress=lambda *_: None):
    start, end = mem.base_address, mem.base_address + mem.module_size
    regions = list(mem.iter_writable_regions())
    module = [(max(base, start), min(base + size, end) - max(base, start))
              for base, size in regions if max(base, start) < min(base + size, end)]
    if not module:
        raise MemoryErrorWin32("No writable regions were found in the main game module.")

    # At each level, prefer the closest parent references if the search branches widely.
    frontier = {slot: [(slot, ())] for slot in selector_slots}
    found = {slot: [] for slot in selector_slots}
    for depth in range(1, 4):
        roots, _ = incoming_references(mem, module, frontier, 0x2000, monotonic() + 20)
        for target, root_links in roots.items():
            for source, delta in root_links:
                for slot, path in frontier[target]:
                    offsets = (delta, *path)
                    try:
                        verified = mem.resolve_pointer(source - start, offsets) == slot
                    except MemoryErrorWin32:
                        verified = False
                    if verified and len(found[slot]) < 50:
                        found[slot].append((source - start, offsets))
        progress(depth, found, len(frontier))
        if all(found.values()) or depth == 3:
            break

        parents, complete = incoming_references(mem, regions, frontier, 0x1000, monotonic() + 55)
        next_frontier = {}
        for target, links in parents.items():
            for source, delta in sorted(links, key=lambda link: abs(link[1]))[:16]:
                if source in frontier or start <= source < end:
                    continue
                for slot, path in frontier[target]:
                    if not found[slot]:
                        next_frontier.setdefault(source, []).append((slot, (delta, *path)))
        if not next_frontier:
            progress("no_more_parents", found, complete)
            break
        frontier = dict(list(next_frontier.items())[:48])
    return found


def main():
    directory = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent / "dist"
    report = directory / "profiles" / "locator_research.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    mem = ProcessMemory()

    def checkpoint(stage, chains=None, candidates=None):
        payload = {"stage": stage, "pid": mem.pid, "module": mem.base_address,
                   "chains": chains, "candidates": candidates}
        staging = report.with_suffix(".tmp")
        staging.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        staging.replace(report)

    try:
        checkpoint("starting")
        games = find_game_processes(("SAN14CE_SC.exe",))
        if len(games) != 1:
            raise MemoryErrorWin32("Expected exactly one running simplified Heritage Edition game.")
        game = games[0]
        mem.attach(game.pid, game.name, read_only=True)
        follow = OfficerFollowTarget.load(mem, report.with_name("officer_follow_session.json"))
        if follow is None:
            raise MemoryErrorWin32("No verified calibration for this game session.")
        checkpoint("attached")
        result = find_module_chains(mem, [slot for slot, _ in follow.handles], checkpoint)
        checkpoint("done", result)
    except (MemoryErrorWin32, OSError, ValueError) as error:
        checkpoint("error", str(error))
    finally:
        mem.detach()


if __name__ == "__main__":
    main()
