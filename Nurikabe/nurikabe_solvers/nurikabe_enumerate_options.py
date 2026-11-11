"""Nurikabe solver by enumerating every island option, in the style of subtiles.py.

Pipeline:
  1. Grow, for each clue n, every connected set of cells of size V[n] that contains the
     clue. Growth never steps onto another clue or next to one, so islands can't merge.
  2. Presolve cycle to a fixpoint:
       - cells no option covers, or that every option of some clue borders, are walls
       - cells every option of a clue covers are that clue's island
       - drop options that cover a wall, overlap/touch another clue's forced island,
         make a 2x2 block of walls, or split the walls they force into separate pieces
  3. CP-SAT on option variables X[n,t] only:
       - exactly one option per clue
       - y[n,c] = sum of the X[n,t] that cover cell c, at most one clue per cell
       - different islands can't touch: y[n,a] + sum_{m!=n} y[m,b] <= 1 for neighbours a,b
       - no 2x2 wall: the 4 cells of every block hold at least one island cell
  4. Wall connectivity with cuts: solve, flood fill the walls; for every wall piece S that
     isn't the biggest, with B its (all-island) border, u in S and v in the biggest piece:
          wall_u + wall_v - 1 <= sum_{w in B} wall_w
     Add, re-solve, repeat. Islands are connected by construction.
  5. Uniqueness: ban the found set of options and keep looping.

Sets of cells are Python ints used as bitmasks: cell (r, c) is bit r*W + c.

Usage:  python nurikabe_enumerate_options.py [puzzle.csv]
"""
import csv
import os
import sys
import time
from ortools.sat.python import cp_model


def read_puzzle(path):
    with open(path, newline="") as f:
        rows = [row for row in csv.reader(f) if row]
    H, W = len(rows), len(rows[0])
    clues = [((r, c), int(float(v))) for r, row in enumerate(rows)
             for c, v in enumerate(row) if v.strip()]
    return H, W, clues


class Grid:
    def __init__(self, H, W):
        self.H, self.W = H, W
        self.full = (1 << (H * W)) - 1
        left = sum(1 << (r * W) for r in range(H))
        self.not_left = self.full & ~left
        self.not_right = self.full & ~(left << (W - 1))
        # every 2x2 block as a mask
        self.blocks = [(1 | 2 | (1 << W) | (2 << W)) << (r * W + c)
                       for r in range(H - 1) for c in range(W - 1)]

    def nb(self, m):
        """Cells orthogonally next to m (may include m itself)."""
        W = self.W
        return (((m << 1) & self.not_left) | ((m >> 1) & self.not_right)
                | (m << W) | (m >> W)) & self.full

    def reach(self, start, within):
        """Flood fill from start, moving only through cells in within."""
        r = start & within
        while True:
            n = (r | self.nb(r)) & within
            if n == r:
                return r
            r = n

    def components(self, m):
        out = []
        while m:
            comp = self.reach(m & -m, m)
            out.append(comp)
            m &= ~comp
        return out


def bits(m):
    while m:
        low = m & -m
        yield low.bit_length() - 1
        m ^= low


def solve(H, W, clues, verbose=True):
    t0 = time.time()
    log = print if verbose else (lambda *a, **k: None)
    G = Grid(H, W)
    N = len(clues)
    cell = [1 << (r * W + c) for (r, c), _ in clues]
    V = [v for _, v in clues]
    all_clues = sum(cell)
    num_walls = H * W - sum(V)

    # ---------- 1. growth ----------
    opts = []
    for n in range(N):
        others = all_clues & ~cell[n]
        allowed = G.full & ~(others | G.nb(others))
        level = {cell[n]}
        for _ in range(V[n] - 1):
            nxt = set()
            for p in level:
                for b in bits(G.nb(p) & ~p & allowed):
                    nxt.add(p | (1 << b))
            level = nxt
        opts.append(level)
    log(f"grown {sum(map(len, opts))} options  [{time.time()-t0:.1f}s]")

    # ---------- 2. presolve cycle ----------
    rounds = 0
    while True:
        rounds += 1
        cover = [0] * N
        core = [G.full] * N
        border = [G.full] * N
        for n in range(N):
            for p in opts[n]:
                cover[n] |= p
                core[n] &= p
                border[n] &= G.nb(p) & ~p
        wall = G.full & ~sum_or(cover)
        for n in range(N):
            wall |= border[n]
        block = [core[n] | G.nb(core[n]) for n in range(N)]
        changed = False
        for n in range(N):
            other_block = 0
            other_core = 0
            for m in range(N):
                if m != n:
                    other_block |= block[m]
                    other_core |= core[m]
            keep = set()
            for p in opts[n]:
                if p & (wall | other_block):
                    continue
                walls_p = (wall | G.nb(p)) & ~p
                if any(b & walls_p == b for b in G.blocks):
                    continue
                # every wall this option forces must sit in one piece of what's left
                rest = G.full & ~p & ~other_core
                if walls_p and G.reach(walls_p & -walls_p, rest) & walls_p != walls_p:
                    continue
                keep.add(p)
            if len(keep) != len(opts[n]):
                changed = True
                opts[n] = keep
            if not keep:
                log(f"clue {clues[n]} has no options left: no solution")
                return None
        if not changed:
            break
    log(f"presolve: {sum(map(len, opts))} options, {bin(wall).count('1')} forced walls "
        f"({rounds} rounds)  [{time.time()-t0:.1f}s]")

    # ---------- 3. CP-SAT model ----------
    m = cp_model.CpModel()

    def add(con):
        """Add a constraint; all-constant ones come back as plain bools."""
        if con is True:
            return
        if con is False:
            m.AddBoolOr([])          # infeasible
            return
        m.Add(con)

    X = [[(m.NewBoolVar(""), p) for p in opts[n]] for n in range(N)]
    for n in range(N):
        m.AddExactlyOne([x for x, _ in X[n]])
    covering = [[[] for _ in range(H * W)] for _ in range(N)]
    for n in range(N):
        for x, p in X[n]:
            for b in bits(p):
                covering[n][b].append(x)
    y = [{} for _ in range(H * W)]          # y[c][n] = 1 if clue n's island covers c
    for n in range(N):
        for c in range(H * W):
            xs = covering[n][c]
            if len(xs) == len(X[n]):
                y[c][n] = 1                 # every option covers it
            elif len(xs) == 1:
                y[c][n] = xs[0]
            elif xs:
                v = m.NewBoolVar("")
                m.Add(v == sum(xs))
                y[c][n] = v
    for c in range(H * W):
        if len(y[c]) > 1:
            add(sum(y[c].values()) <= 1)

    def wall_of(c):
        return 1 - sum(y[c].values())

    # different islands can't touch
    for a in range(H * W):
        for b in bits(G.nb(1 << a) & ~(1 << a)):
            if b < a:
                continue
            for n, ya in y[a].items():
                rest = [yb for k, yb in y[b].items() if k != n]
                if rest:
                    add(ya + sum(rest) <= 1)
    # no 2x2 wall
    for blk in G.blocks:
        terms = [v for c in bits(blk) for v in y[c].values()]
        add(sum(terms) >= 1)

    # ---------- 4./5. cut loop ----------
    solutions = []
    cuts = 0
    it = 0
    while True:
        it += 1
        sv = cp_model.CpSolver()
        sv.parameters.num_workers = 8
        st = sv.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        chosen = [(x, p) for n in range(N) for x, p in X[n] if sv.Value(x)]
        islands = sum_or(p for _, p in chosen)
        walls = G.full & ~islands
        pieces = sorted(G.components(walls), key=lambda s: -bin(s).count("1"))
        log(f"solve {it}: {sv.StatusName(st)} in {sv.WallTime():.2f}s, "
            f"{len(pieces)} wall piece(s)")
        if len(pieces) <= 1:
            solutions.append(islands)
            if len(solutions) == 2:
                break
            m.AddBoolOr([x.Not() for x, _ in chosen])
            continue
        v = next(bits(pieces[0]))
        for S in pieces[1:]:
            u = next(bits(S))
            B = G.nb(S) & ~S
            add(wall_of(u) + wall_of(v) - 1 <= sum(wall_of(w) for w in bits(B)))
            cuts += 1
    log(f"{cuts} connectivity cuts, {it} solves  [total {time.time()-t0:.1f}s]")
    if not solutions:
        return None
    return solutions[0], len(solutions) == 1


def sum_or(masks):
    out = 0
    for x in masks:
        out |= x
    return out


def print_solution(H, W, clues, islands):
    val = {r * W + c: v for (r, c), v in clues}
    width = max(len(str(v)) for _, v in clues)
    for r in range(H):
        row = []
        for c in range(W):
            i = r * W + c
            s = str(val[i]) if i in val else ("." if islands >> i & 1 else "#")
            row.append(s.rjust(width))
        print(" ".join(row))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "nurikabe_puzzles", "nurikabe_puzzle1.csv")
    H, W, clues = read_puzzle(path)
    res = solve(H, W, clues)
    if not res:
        print("no solution")
        return
    islands, unique = res
    print_solution(H, W, clues, islands)
    print("unique:", unique)


if __name__ == "__main__":
    main()
