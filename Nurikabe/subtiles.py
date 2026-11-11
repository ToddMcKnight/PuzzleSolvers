"""Subtiles solver (Jane Street, Subtiles / Subtiles 2) - final Python version.

Pipeline, per level K = 2, 3, ...:
  1. Grow in placement space: anchor each surviving (K-1)-shape on the K-clues in every
     orientation (covering all clues, or all but one), then add one cell.
  2. Run the presolve cycle to a fixpoint over all levels so far:
     parent/child shape pruning, forced cells, clash removal.
Then, for each candidate N: a final cycle, then CP-SAT on placement variables only,
plus a uniqueness check.

Usage:  python subtiles.py puzzle.txt
puzzle.txt: one grid row per line, "." for blank, numbers for clue values.
Requires: pip install ortools
"""
import sys
import time
from collections import defaultdict
from ortools.sat.python import cp_model

NBR = ((1, 0), (-1, 0), (0, 1), (0, -1))


def tr(p, t):
    """Apply symmetry t (0-7) to point p: t % 4 quarter turns, then a mirror if t >= 4."""
    r, c = p
    for _ in range(t % 4):
        r, c = c, -r
    if t >= 4:
        c = -c
    return r, c


def norm(pts):
    """Translate pts so the min row and column are 0, returned as a sorted tuple."""
    mr = min(r for r, _ in pts)
    mc = min(c for _, c in pts)
    return tuple(sorted((r - mr, c - mc) for r, c in pts))


def orients(s):
    """All distinct normalised rotations and reflections of shape s."""
    return {norm([tr(p, t) for p in s]) for t in range(8)}


def canon(s):
    """Canonical form of shape s: the smallest of its orientations, so congruent shapes match."""
    return min(orients(s))


def grow(s):
    """Every canonical shape made by adding one orthogonally adjacent cell to s."""
    ss = set(s)
    fr = {(r + dr, c + dc) for r, c in ss for dr, dc in NBR} - ss
    return {canon(ss | {x}) for x in fr}


def solve(clues, H, W, verbose=True):
    """Solve a Subtiles puzzle.

    clues maps (row, col) to its clue value, on an H x W grid. Grows the shapes level by
    level with presolve after each one, then tries each candidate N from the largest down
    with CP-SAT. Returns (N, grid, unique), where grid[r][c] is the size of the shape
    covering the cell (0 if none), or None if no N works.
    """
    t0 = time.time()
    log = print if verbose else (lambda *a, **k: None)
    cells_of = defaultdict(set)
    for cell, v in clues.items():
        cells_of[v].add(cell)
    lo = max(clues.values())
    n_cap = max(n for n in range(1, 40) if n * (n + 1) // 2 <= H * W)

    P = {}                                   # P[K][shape] = set of placements
    parents = {}                             # parents[K][shape] = set of (K-1)-shapes
    children = defaultdict(lambda: defaultdict(set))
    forced = {}
    stats = {"grown": 0}

    def placements(shape, K):
        """Grid placements of shape (any orientation) as a K-shape: inside the grid, covering
        every K-clue, and with no cell that holds or is forced to a different value."""
        need = cells_of.get(K, set())
        out = set()
        for o in orients(shape):
            if need:
                ar, ac = next(iter(need))
                offs = {(ar - r, ac - c) for r, c in o}
            else:
                h = max(r for r, _ in o) + 1
                w = max(c for _, c in o) + 1
                offs = {(dr, dc) for dr in range(H - h + 1) for dc in range(W - w + 1)}
            for dr, dc in offs:
                cells = frozenset((r + dr, c + dc) for r, c in o)
                if any(not (0 <= r < H and 0 <= c < W) for r, c in cells):
                    continue
                if not need <= cells:
                    continue
                if any(clues.get(x, K) != K or forced.get(x, K) != K for x in cells):
                    continue
                out.add(cells)
        return out

    def cycle(top, n_known):
        """Run the presolve cycle to a fixpoint over levels 1..top.
        Child pruning applies to K < n_known; forcing uses K <= n_known."""
        rounds = 0
        while True:
            rounds += 1
            changed = False
            # shape pruning: sweep up and down until stable
            while True:
                moved = False
                for K in range(1, top + 1):
                    for s in list(P[K]):
                        if not P[K][s] or (K > 1 and not any(p in P[K - 1] for p in parents[K][s])):
                            del P[K][s]; moved = True
                for K in range(min(top, n_known) - 1, 0, -1):
                    for s in list(P[K]):
                        if not any(c in P[K + 1] for c in children[K][s]):
                            del P[K][s]; moved = True
                if not moved:
                    break
                changed = True
            for K in range(1, top + 1):
                if not P[K]:
                    return False, rounds
            # forced cells
            new_forced = {}
            for K in range(1, min(top, n_known) + 1):
                allp = [pl for ps in P[K].values() for pl in ps]
                for x in frozenset.intersection(*allp):
                    if new_forced.get(x, K) != K or forced.get(x, K) != K:
                        return False, rounds
                    new_forced[x] = K
            if any(x not in forced for x in new_forced):
                changed = True
            forced.update(new_forced)
            # clash removal
            for K in range(1, top + 1):
                for s in P[K]:
                    bad = {pl for pl in P[K][s] if any(forced.get(x, K) != K for x in pl)}
                    if bad:
                        P[K][s] -= bad; changed = True
            if not changed:
                return True, rounds

    # ---------- interleaved growth, done in placement space ----------
    canon_cache = {}
    def canon_of(cells):
        """Canonical shape of a placement, memoised on its normalised form."""
        key = norm(list(cells))
        r = canon_cache.get(key)
        if r is None:
            r = canon(key); canon_cache[key] = r
        return r
    def ok_cell(x, K):
        """True if cell x is on the grid and may belong to a K-shape (no conflicting clue or forced value)."""
        r, c = x
        return 0 <= r < H and 0 <= c < W and clues.get(x, K) == K and forced.get(x, K) == K

    mono = ((0, 0),)
    P[1] = {mono: placements(mono, 1)}
    parents[1] = {mono: set()}
    top = 1
    for K in range(2, n_cap + 1):
        need = cells_of.get(K, set())
        m = len(need)
        newP = defaultdict(set); par = defaultdict(set)
        for s in P[K - 1]:
            for o in orients(s):
                if m == 0:
                    h = max(r for r, _ in o) + 1; w = max(c for _, c in o) + 1
                    offs = {(dr, dc) for dr in range(H - h + 1) for dc in range(W - w + 1)}
                else:
                    offs = {(cr - r, cc - c) for cr, cc in need for r, c in o}
                    if m == 1:
                        (cr, cc), = need
                        offs |= {(cr + dr - r, cc + dc - c) for dr, dc in NBR for r, c in o}
                for dr, dc in offs:
                    copy = frozenset((r + dr, c + dc) for r, c in o)
                    if not all(ok_cell(x, K) for x in copy):
                        continue
                    missing = need - copy
                    if len(missing) > 1:
                        continue
                    if missing:
                        f = next(iter(missing))
                        if not any((f[0] + a, f[1] + b) in copy for a, b in NBR):
                            continue
                        cands = (f,)
                    else:
                        cands = {(r + a, c + b) for r, c in copy for a, b in NBR} - copy
                    for f in cands:
                        if not ok_cell(f, K):
                            continue
                        pl = copy | {f}
                        t = canon_of(pl)
                        newP[t].add(pl); par[t].add(s)
        stats["grown"] += len(newP)
        parents[K] = par
        P[K] = dict(newP)
        for t, ps in par.items():
            for p in ps:
                children[K - 1][p].add(t)
        if not P[K]:
            log(f"K={K:2d}: grown {len(newP):6d}  none survive -> N <= {K-1}")
            del P[K]
            break
        top = K
        before = len(P[K])
        ok, rnds = cycle(top, lo)
        if not ok:
            log(f"K={K:2d}: contradiction during presolve")
            return None
        log(f"K={K:2d}: shapes {len(newP):6d}  placed {before:6d}  after cycle {len(P[K]):6d}  "
            f"placements {sum(map(len, P[K].values())):6d}  forced {len(forced):3d}  "
            f"({rnds} rnds)  [{time.time()-t0:.1f}s]", flush=True)

    log(f"total shapes grown: {stats['grown']}")
    for N in range(top, lo - 1, -1):
        saveP = {K: {s: set(v) for s, v in P[K].items()} for K in P}
        saveF = dict(forced)
        for K in list(P):
            if K > N:
                del P[K]
        ok, rnds = cycle(N, N)
        if ok:
            res = _cpsat(N, P, parents, H, W, log, t0)
            if res:
                return res
        log(f"N={N}: infeasible")
        P.clear(); P.update(saveP); forced.clear(); forced.update(saveF)
    return None


def _cpsat(N, P, parents, H, W, log, t0):
    """Pick one placement per level 1..N with CP-SAT.

    Constraints: no cell covered twice, and a K-shape can only be used if one of its
    parent (K-1)-shapes is also used. Returns (N, grid, unique) or None if infeasible.
    Uniqueness is checked by banning the found set of placements and re-solving.
    """
    m = cp_model.CpModel()
    cover = defaultdict(list)
    xs = {}
    for K in range(1, N + 1):
        allv = []
        for s, pls in P[K].items():
            xs[(K, s)] = [(m.NewBoolVar(""), cells) for cells in pls]
            for v, cells in xs[(K, s)]:
                allv.append(v)
                for x in cells:
                    cover[x].append(v)
        m.AddExactlyOne(allv)
    for vs in cover.values():
        m.AddAtMostOne(vs)
    for K in range(2, N + 1):
        for s in P[K]:
            rhs = [v for p in parents[K][s] if p in P[K - 1] for v, _ in xs[(K - 1, p)]]
            m.Add(sum(v for v, _ in xs[(K, s)]) <= sum(rhs))
    sv = cp_model.CpSolver()
    sv.parameters.num_workers = 8
    st = sv.Solve(m)
    nv = sum(len(l) for l in xs.values())
    log(f"N={N}: {nv} vars, {sv.StatusName(st)} in {sv.WallTime():.2f}s  [total {time.time()-t0:.1f}s]")
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    chosen = [v for l in xs.values() for v, _ in l if sv.Value(v)]
    grid = [[0] * W for _ in range(H)]
    for (K, s), l in xs.items():
        for v, cells in l:
            if sv.Value(v):
                for r, c in cells:
                    grid[r][c] = K
    m.AddBoolOr([v.Not() for v in chosen])
    unique = cp_model.CpSolver().Solve(m) == cp_model.INFEASIBLE
    log(f"unique: {unique}")
    return N, grid, unique

def main():
    """Read the puzzle file given on the command line, solve it, and print the grid and row sums."""
    path = sys.argv[1] if len(sys.argv) > 1 else "Nurikabe//subtiles1.txt"
    rows = [l.split() for l in open(path) if l.strip()]
    H, W = len(rows), len(rows[0])
    clues = {(r, c): int(t) for r, row in enumerate(rows) for c, t in enumerate(row) if t != "."}
    res = solve(clues, H, W)
    if not res:
        print("no solution")
        return
    N, grid, unique = res
    for row in grid:
        print(" ".join(f"{x:2d}" if x else " ." for x in row))
    sums = [sum(r) for r in grid]
    print("row sums", sums)
    print("min * max =", min(sums) * max(sums), "| unique:", unique)


if __name__ == "__main__":
    main()
