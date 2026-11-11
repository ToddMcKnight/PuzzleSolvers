"""Generate Subtiles puzzles that have exactly one solution.

Steps for each puzzle:
  1. Grow a chain of shapes: start from one cell and add a random neighbouring cell
     N - 1 times. Each shape is the previous one plus a cell, so the subtile rule holds
     by construction.
  2. Pack the shapes onto the grid, largest first, each in a random orientation. Among
     the spots that fit, prefer ones that hug the border and earlier shapes, so the grid
     doesn't get chopped into gaps nothing fits in. Retry from scratch if a shape won't
     fit anywhere.
  3. Reveal a few cells of each shape as clues: about one per `--clue-rate` cells, and at
     least one per level, like the published puzzles.
  4. Repair until unique. Solve; if the solver finds a different answer (a second
     solution, or a bigger N than the one generated), reveal the true value of a cell
     where that answer is wrong. That answer can never come back, so this always ends.
  5. Try removing each clue (skip with --no-minimize), keeping a removal only if the
     puzzle stays unique with the same N.

Every puzzle is re-solved at the end to confirm: the solver must return the generated
grid, with that N, and report it as unique.

Usage:
  python subtiles_generator.py --size 13 --n 16 --count 5
  python subtiles_generator.py --size 9 --n 10 --count 3 --seed 7 --no-minimize
Writes NAME.txt (the puzzle, same format as subtiles1.txt) and NAME_solution.txt to
subtiles_puzzles/generated/ unless --out says otherwise.
"""
import argparse
import multiprocessing as mp
import os
import queue
import re
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "subtiles_solvers"))
import subtiles as st  # noqa: E402

NBR = st.NEIGHBOR_OFFSETS


# ---------- 1. the chain of shapes ----------

def grow_chain(N, rng):
    """shapes[K-1] = the K-cell shape, as normalised (row, col) cells."""
    cells = {(0, 0)}
    shapes = [st.normalize(list(cells))]
    for _ in range(2, N + 1):
        frontier = sorted({(r + a, c + b) for r, c in cells for a, b in NBR} - cells)
        # Weight each candidate by how many shape cells it touches (squared), so shapes
        # stay fairly compact. Sprawling shapes are very hard to pack densely.
        weights = [sum((r + a, c + b) in cells for a, b in NBR) ** 2 for r, c in frontier]
        cells = cells | {rng.choices(frontier, weights)[0]}
        shapes.append(st.normalize(list(cells)))
    return shapes


# ---------- 2. packing ----------

def pack(shapes, H, W, rng, tries=300, top=4):
    """Place every shape without overlap. Returns grid[r][c] = level (0 = empty) or None."""
    N = len(shapes)
    for _ in range(tries):
        grid = [[0] * W for _ in range(H)]
        for K in range(N, 0, -1):
            options = []
            for orient in st.orientations(shapes[K - 1]):
                h = max(r for r, _ in orient) + 1
                w = max(c for _, c in orient) + 1
                for dr in range(H - h + 1):
                    for dc in range(W - w + 1):
                        cells = [(r + dr, c + dc) for r, c in orient]
                        if any(grid[r][c] for r, c in cells):
                            continue
                        # contact = neighbouring cell sides that are the border or taken
                        contact = sum(1 for r, c in cells for a, b in NBR
                                      if not (0 <= r + a < H and 0 <= c + b < W) or grid[r + a][c + b])
                        options.append((contact, cells))
            if not options:
                break
            options.sort(key=lambda o: -o[0])
            _, cells = rng.choice(options[:top])
            for r, c in cells:
                grid[r][c] = K
        else:
            return grid
    return None


# ---------- 3. starting clues ----------

def starting_clues(grid, N, rng, clue_rate):
    by_level = {K: [] for K in range(1, N + 1)}
    for r, row in enumerate(grid):
        for c, K in enumerate(row):
            if K:
                by_level[K].append((r, c))
    clues = {}
    for K, cells in by_level.items():
        for cell in rng.sample(cells, max(1, round(K / clue_rate))):
            clues[cell] = K
    return clues


# ---------- 4./5. uniqueness ----------

def is_unique_answer(clues, H, W, N, truth, log=None):
    """Returns (ok, wrong, reason). ok: the solver's answer is exactly `truth`, with this
    N, and unique. Otherwise `wrong` is an answer that isn't the truth (or None)."""
    res = st.solve(clues, H, W, verbose=False, want_alt=True, log=log)
    if res is None:
        return False, None, "no solution"
    n_found, grid, unique, alt = res
    if n_found == N and unique and grid == truth:
        return True, None, "unique"
    if n_found != N:
        # the solver always takes the largest N that works
        return False, grid, f"a bigger chain (N={n_found}) also fits"
    # Same N: the solver's own grid if it isn't the truth, otherwise its second solution.
    return False, (grid if grid != truth else alt), "a second solution exists"


LEVEL_LINE = re.compile(r"K=\s*(\d+): shapes\s+\d+\s+after cycle\s+\d+\s+placements\s+(\d+)")


def _check_worker(results, progress, clues, H, W, N, truth):
    # Pass the solver's per-level progress lines back, so a slow solve can be diagnosed.
    def report(*parts, **_):
        progress.put(" ".join(str(x) for x in parts))
    results.put(is_unique_answer(clues, H, W, N, truth, log=report))


def process_memory(pid):
    """Memory in use by a process, in bytes (Windows working set), or 0 if unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # limited query + VM read
        if not handle:
            return 0
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        kernel32.CloseHandle(handle)
        return counters.WorkingSetSize if ok else 0
    except Exception:
        return 0


def timed_check(clues, H, W, N, truth, limit, mem_limit_gb=3.0):
    """is_unique_answer in its own process, stopped after `limit` seconds or once it uses
    more than `mem_limit_gb` GB of memory. Returns (ok, wrong, reason, seconds, levels);
    reason contains "timed out" or "ran out of memory" if it was stopped. levels =
    {K: placements left after presolve} for every level the solver finished building.
    Starting the process adds about a second."""
    t = time.time()
    results, progress = mp.Queue(), mp.Queue()
    proc = mp.Process(target=_check_worker, args=(results, progress, clues, H, W, N, truth), daemon=True)
    proc.start()
    stopped = None
    peak = 0
    while True:
        try:
            ok, wrong, reason = results.get(timeout=1)
            break
        except queue.Empty:
            pass
        peak = max(peak, process_memory(proc.pid))
        if peak > mem_limit_gb * 2 ** 30:
            stopped = f"the solve ran out of memory (over {mem_limit_gb:g} GB)"
        elif time.time() - t > limit:
            stopped = f"the check timed out after {limit:.0f}s"
        elif not proc.is_alive() and results.empty():
            stopped = "the solver process crashed"
        if stopped:
            proc.terminate()
            break
    proc.join()
    levels = {}
    while True:
        try:
            line = progress.get_nowait()
        except (queue.Empty, EOFError, OSError):
            break
        m = LEVEL_LINE.search(line)
        if m:
            levels[int(m.group(1))] = int(m.group(2))
    if stopped:
        return False, None, stopped, time.time() - t, levels
    return ok, wrong, reason, time.time() - t, levels


def extreme_hidden_cell(K, clues, truth, H, W, rng):
    """A clue that pins down the K-shape the most: a cell on one of the shape's extremes
    (its topmost row, bottommost row, leftmost or rightmost column) on a side that has no
    clue yet, so the shape's bounding box gets fixed one side at a time. Among those, the
    one farthest from the shape's existing clues. If every side already has a clue, falls
    back to the hidden cell farthest from them."""
    cells = [(r, c) for r in range(H) for c in range(W) if truth[r][c] == K]
    hidden = [x for x in cells if x not in clues]
    if not hidden:
        return None
    own = [x for x in cells if x in clues]
    top, bottom = min(r for r, _ in cells), max(r for r, _ in cells)
    left, right = min(c for _, c in cells), max(c for _, c in cells)
    sides = [lambda x: x[0] == top, lambda x: x[0] == bottom,
             lambda x: x[1] == left, lambda x: x[1] == right]
    open_sides = [side for side in sides if not any(side(x) for x in own)]
    pool = [x for x in hidden if any(side(x) for side in open_sides)] or hidden
    def distance(x):
        return min((abs(x[0] - r) + abs(x[1] - c) for r, c in own), default=0)
    return max(pool, key=lambda x: (distance(x), rng.random()))


def farthest_hidden_cell(K, clues, truth, H, W, rng):
    """The hidden cell of the K-shape farthest from that shape's revealed clues (or a
    random one if none are revealed). Spread-out clues pin a shape's size and position
    down the most, which is what the bounding-box checks in the solver feed on."""
    hidden = [(r, c) for r in range(H) for c in range(W) if truth[r][c] == K and (r, c) not in clues]
    if not hidden:
        return None
    own = [cell for cell, v in clues.items() if v == K]
    def distance(x):
        return min((abs(x[0] - r) + abs(x[1] - c) for r, c in own), default=0)
    return max(hidden, key=lambda x: (distance(x), rng.random()))


def stuck_level(levels, N):
    """Which level a stopped solve was struggling with. Levels are built in order, so if
    it stopped mid-build that's the level after the last finished one. If every level
    was built (it stopped in the final presolve or CP-SAT), blame the level with the
    most placements left."""
    if not levels:
        return 2
    last = max(levels)
    if last < N:
        return last + 1
    return max(levels, key=lambda k: levels[k])


def repair(clues, H, W, N, truth, rng, log, limit):
    rounds = 0
    streak_level, streak = None, 0
    while True:
        ok, wrong, reason, took, levels = timed_check(clues, H, W, N, truth, limit)
        if ok:
            log(f"    unique with {len(clues)} clues (solve {took:.1f}s)")
            return clues, rounds
        if "timed out" in reason or "out of memory" in reason or "crashed" in reason:
            # Too slow (or too big) means too little information where the solver is
            # stuck. Add clues on that level (anchors its build) and the level below (its
            # parent pool), on their extremes, more of them each time the same level is
            # still the bottleneck.
            K = stuck_level(levels, N)
            streak = streak + 1 if K == streak_level else 1
            streak_level = K
            want = streak + 1
            targets = [k for k in (K, K - 1) if k >= 1]
            added = []
            for i in range(want * 3):
                if len(added) == want:
                    break
                k = targets[i % len(targets)]
                cell = extreme_hidden_cell(k, clues, truth, H, W, rng)
                if cell is not None:
                    clues[cell] = k
                    added.append((k, cell))
            if not added:
                return None, rounds
            rounds += 1
            built = max(levels) if levels else 0
            where = (f"stopped while building level {K}" if K == built + 1
                     else f"every level was built; level {K} had the most placements ({levels.get(K, 0):,})")
            parent = f", level {K - 1} had {levels[K - 1]:,} placements" if K - 1 in levels and K == built + 1 else ""
            shown = ", ".join(f"a {k} at row {r + 1}, col {c + 1}" for k, (r, c) in added)
            why = reason.replace("the check timed out after", "solve took over").replace("the solve ", "solve ")
            log(f"    round {rounds}: {why} ({where}{parent}); "
                f"revealed {len(added)}: {shown} -> {len(clues)} clues")
            continue
        streak_level, streak = None, 0
        if wrong is None:
            return None, rounds
        # Cells where that answer disagrees with the truth, and the truth has a shape.
        # Revealing one of them as a clue rules that answer out for good.
        cand = [(r, c) for r in range(H) for c in range(W)
                if truth[r][c] and wrong[r][c] != truth[r][c] and (r, c) not in clues]
        if not cand:
            return None, rounds
        # Spread clues out: pick from the shape with the smallest share of its cells
        # already revealed, so no single shape gets given away.
        def revealed_share(x):
            K = truth[x[0]][x[1]]
            return sum(1 for v in clues.values() if v == K) / K
        cell = min(cand, key=lambda x: (revealed_share(x), rng.random()))
        clues[cell] = truth[cell[0]][cell[1]]
        rounds += 1
        log(f"    round {rounds}: {reason}, wrong in {len(cand)} cells (solve {took:.1f}s); "
            f"revealed a {clues[cell]} at row {cell[0] + 1}, col {cell[1] + 1} -> {len(clues)} clues")


def crowding(cell, clues):
    """How crowded a clue is: sum of 1 / distance^2 over the other clues (Manhattan
    distance). Two clues side by side add 1, two cells apart 0.25, and so on."""
    r, c = cell
    return sum(1 / (abs(r - r2) + abs(c - c2)) ** 2 for r2, c2 in clues if (r2, c2) != cell)


def minimize(clues, H, W, N, truth, rng, log, save, limit, max_timeouts=3):
    """Try removing each clue; keep the removal if the puzzle stays unique with the same N.

    Clues are tried most-crowded first. Clues tried late are the likeliest to be kept
    (most of the others are gone by then), so trying crowded ones early thins out
    clusters and leaves the surviving clues spread across the grid. The crowding is
    re-measured after every removal; ties are broken at random."""
    untried = set(clues)
    total = len(untried)
    t0 = time.time()
    tally = {"removed": 0, "needed": 0, "timed out": 0}
    in_a_row = 0
    log(f"  minimizing: trying each of {total} clues, most crowded first, "
        f"each check stops after {limit:.0f}s")
    for i in range(1, total + 1):
        cell = max(untried, key=lambda x: (crowding(x, clues), rng.random()))
        untried.discard(cell)
        crowd = crowding(cell, clues)
        value = clues.pop(cell)
        where = (f"[{i:>{len(str(total))}}/{total}] the {value} at row {cell[0] + 1}, col {cell[1] + 1}"
                 f" (crowding {crowd:.2f})")
        ok, _, reason, took, _ = timed_check(clues, H, W, N, truth, limit)
        if ok:
            tally["removed"] += 1
            save(clues)
            log(f"    {where}: removed, still unique ({took:.1f}s) -> {len(clues)} clues, saved")
        else:
            clues[cell] = value
            tally["timed out" if "timed out" in reason else "needed"] += 1
            log(f"    {where}: kept, without it {reason} ({took:.1f}s)")
        # Several timeouts in a row means the puzzle is now too sparse to solve within
        # the limit, so the remaining checks would time out too: stop here.
        in_a_row = in_a_row + 1 if (not ok and "timed out" in reason) else 0
        if in_a_row >= max_timeouts and untried:
            log(f"    {max_timeouts} checks in a row timed out, so stopping with {len(untried)} clues untried")
            break
    mins, secs = divmod(int(time.time() - t0), 60)
    log(f"  minimized {total} -> {len(clues)} clues in {mins}m{secs:02d}s "
        f"(removed {tally['removed']}; kept {tally['needed']} needed, "
        f"{tally['timed out']} timed out)")
    return clues


# ---------- output ----------

def write_grid(path, H, W, value_at):
    width = max(len(str(value_at(r, c) or ".")) for r in range(H) for c in range(W))
    with open(path, "w") as f:
        for r in range(H):
            f.write(" ".join(str(value_at(r, c) or ".").rjust(width) for c in range(W)) + "\n")


def generate(H, W, N, seed, clue_rate, do_minimize, out_dir, log, check_limit, repair_limit, max_timeouts):
    rng = random.Random(seed)
    t0 = time.time()
    name = f"gen_{H}x{W}_N{N}_seed{seed}"
    puzzle_path = os.path.join(out_dir, name + ".txt")

    def save(clues):
        write_grid(puzzle_path, H, W, lambda r, c: clues.get((r, c), 0))

    for attempt in range(1, 51):
        shapes = grow_chain(N, rng)
        truth = pack(shapes, H, W, rng)
        if truth is None:
            log(f"  attempt {attempt}: couldn't pack the shapes, new chain")
            continue
        clues = starting_clues(truth, N, rng, clue_rate)
        log(f"  attempt {attempt}: packed, {len(clues)} starting clues")
        clues, rounds = repair(clues, H, W, N, truth, rng, log, repair_limit)
        if clues is None:
            log(f"  the empty cells can hold a bigger chain than N={N}, so the answer would be a "
                "larger N; clues can't block that, so try a new grid")
            continue
        # Save right away: from here on the puzzle on disk is always a valid, unique one.
        write_grid(os.path.join(out_dir, name + "_solution.txt"), H, W, lambda r, c: truth[r][c])
        save(clues)
        log(f"  saved {name}.txt ({len(clues)} clues, not minimized yet)")
        if do_minimize:
            clues = minimize(clues, H, W, N, truth, rng, log, save, check_limit, max_timeouts)
        # final check, independent of everything above
        t = time.time()
        res = st.solve(clues, H, W, verbose=False)
        solve_time = time.time() - t
        assert res is not None and res[0] == N and res[1] == truth and res[2], "final check failed"
        save(clues)
        filled = sum(1 for row in truth for v in row if v)
        return {"name": name, "clues": len(clues), "repair_rounds": rounds, "filled": filled,
                "solve_time": solve_time, "gen_time": time.time() - t0}
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, default=13, help="grid is SIZE x SIZE (default 13)")
    ap.add_argument("--min-size", type=int, default=None,
                    help="with --max-size: pick each puzzle's rows and columns at random in this range")
    ap.add_argument("--max-size", type=int, default=None)
    ap.add_argument("--n", type=int, default=None,
                    help="number of shapes (default: fills about 80%% of the grid)")
    ap.add_argument("--count", type=int, default=1, help="how many puzzles to make")
    ap.add_argument("--seed", type=int, default=1, help="first random seed; puzzle i uses seed + i")
    ap.add_argument("--clue-rate", type=float, default=3.5,
                    help="about one starting clue per this many cells of a shape (default 3.5)")
    ap.add_argument("--no-minimize", dest="minimize", action="store_false",
                    help="keep every clue the repair step added (default: remove the ones not needed)")
    ap.add_argument("--check-timeout", type=float, default=30,
                    help="while minimizing, give up on a check after this many seconds and keep "
                         "that clue (default 30)")
    ap.add_argument("--repair-timeout", type=float, default=60,
                    help="while repairing, a solve slower than this many seconds is stopped and "
                         "a clue is added to narrow the search (default 60)")
    ap.add_argument("--max-timeouts", type=int, default=3,
                    help="stop minimizing after this many checks in a row time out (default 3)")
    ap.add_argument("--out", default=os.path.join(HERE, "subtiles_puzzles", "generated"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, flush=True))
    for i in range(args.count):
        seed = args.seed + i
        if args.min_size and args.max_size:
            dims = random.Random(f"dims{seed}")
            H = dims.randint(args.min_size, args.max_size)
            W = dims.randint(args.min_size, args.max_size)
        else:
            H = W = args.size
        N = args.n or max(n for n in range(1, 100) if n * (n + 1) // 2 <= 0.8 * H * W)
        print(f"{H}x{W}, N={N}, seed {seed}", flush=True)
        info = generate(H, W, N, seed, args.clue_rate, args.minimize, args.out, log, args.check_timeout,
                        args.repair_timeout, args.max_timeouts)
        if info is None:
            print("  gave up after 50 grids")
            continue
        print(f"  -> {info['name']}.txt: {info['clues']} clues, {info['filled']}/{H * W} cells filled, "
              f"{info['repair_rounds']} repair rounds, solves in {info['solve_time']:.1f}s "
              f"(generated in {info['gen_time']:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
