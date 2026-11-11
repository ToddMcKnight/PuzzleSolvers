"""
Fast exact Nurikabe solver: a MILP (HiGHS, via highspy) with an optional CP-SAT backend.

Public interface
----------------
    solve(num_rows, num_cols, clues, time_limit=None, *, backend=..., verbose=False, **options) -> dict
    load_csv(path) -> (num_rows, num_cols, clues)

Clues are ((row, col), value) with 1-INDEXED row/col.  See README.md in this folder for the
formulation, the list of improvements over ../nurikabe.py, and benchmark results.

Model outline (all integer-linear, identical for both backends)
---------------------------------------------------------------
* Logical presolve (propagated to a fixed point) fixes many cells before any model is built:
  BFS reachability of every clue that avoids cells touching other gardens, forced walls,
  forced garden cells, 2x2 rule, clues whose reachable set equals their size.
  Fixed cells are folded into constants -> they never become variables.
* g[c,u]  binary, only for cells u that clue c can still reach;  x[u] = wall, only for undecided cells.
* (1) sum_c g[c,u] + x[u] = 1            (2) sum_u g[c,u] = v_c            (+) sum_u x[u] = W
* (3) g[c,u] <= g[c,w] + x[w]  for neighbours u,w   (aggregated "different gardens can't touch")
* (4) no 2x2 walls  (5) wall cell has a wall neighbour (W>=2)  (6) garden cell has a same-garden
  neighbour (v_c>=2)  -- (6) alone already implies connectivity for gardens of size <= 3.
* Connectivity of gardens with v_c >= 4: either a per-garden single-commodity flow on
  non-negative directed arcs with distance-based capacities, or lazy cut-set constraints.
* Connectivity of the walls: lazy cut-set constraints (default) or a single-commodity flow
  rooted at a presolve-forced wall cell with distance-based capacities.
"""
from __future__ import annotations

import csv
import math
import time
from collections import deque

__all__ = ["solve", "load_csv", "format_grid", "DEFAULT_OPTIONS"]

# Defaults chosen from the benchmark in README.md
DEFAULT_OPTIONS = {
    "backend": "highs",        # 'highs' (MILP) or 'cpsat'
    "presolve": "full",        # 'full' (propagated logic) or 'basic' (one BFS pass only)
    "garden_conn": "flow",     # 'flow' or 'lazy'
    "wall_conn": "flow",       # 'flow', 'level' or 'lazy'
    "connectivity": None,      # 'hierarchy' -> one unified parent/level (MTZ) tree for all regions
    "objective": "compact",    # None, 'compact', 'walls_far', 'random' (HiGHS steering objective)
    "shape_limit": 2000,       # garden_conn='shapes': enumerate polyominoes if <= this many
    "root": "far",             # wall-flow root choice among forced walls: 'first', 'central', 'far'
    "neighbour_cuts": True,    # constraints (5) and (6)
    "random_seed": 0,
    "threads": None,           # CP-SAT num_workers (None -> solver default); HiGHS MIP is serial
    "flow_integer": False,     # HiGHS: flow variables continuous (integrality implied); CP-SAT: always int
    "mip_heuristic_effort": None,
    "highs_options": None,     # extra {name: value} passed to Highs.setOptionValue
}

_INF = math.inf


# --------------------------------------------------------------------------------------
# I/O helpers
# --------------------------------------------------------------------------------------
def load_csv(path):
    """Read a puzzle CSV (blank = unknown, integer = clue).  Returns (num_rows, num_cols, clues)
    with clues as a list of ((row, col), value), 1-indexed."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh)]
    while rows and all(not c.strip() for c in rows[-1]) and len(rows[-1]) <= 1:
        rows.pop()  # trailing empty lines
    num_rows = len(rows)
    num_cols = max((len(r) for r in rows), default=0)
    clues = []
    for i, r in enumerate(rows):
        for j, cell in enumerate(r):
            s = cell.strip()
            if s:
                clues.append(((i + 1, j + 1), int(float(s))))
    return num_rows, num_cols, clues


def format_grid(grid):
    if not grid:
        return ""
    w = max(len(s) for row in grid for s in row)
    return "\n".join(" ".join(s.rjust(w) for s in row) for row in grid)


# --------------------------------------------------------------------------------------
# Small linear-model container shared by both backends
# --------------------------------------------------------------------------------------
class _Lin:
    """Variables + rows.  A 'ref' is either an int (variable index) or a float (constant)."""

    def __init__(self):
        self.lb, self.ub = [], []
        self.cost = {}          # optional steering objective (search stops at first feasible)
        self.integer = []       # False -> continuous (flow variables; integrality is implied)
        self.rows = []          # (idx list, coef list, lo, hi)
        self.infeasible = False

    def var(self, lb, ub, integer=True):
        self.integer.append(integer)
        self.lb.append(lb)
        self.ub.append(ub)
        return len(self.lb) - 1

    def add(self, pairs, lo=-_INF, hi=_INF):
        """pairs: iterable of (ref, coef).  Folds constants, merges duplicates, drops rows that
        are implied by variable bounds, flags trivially infeasible rows."""
        acc = {}
        const = 0.0
        for ref, coef in pairs:
            if coef == 0:
                continue
            if isinstance(ref, float):
                const += ref * coef
            else:
                acc[ref] = acc.get(ref, 0) + coef
        lo -= const
        hi -= const
        idx = [k for k, v in acc.items() if v != 0]
        cf = [acc[k] for k in idx]
        amin = sum(c * (self.lb[k] if c > 0 else self.ub[k]) for k, c in zip(idx, cf))
        amax = sum(c * (self.ub[k] if c > 0 else self.lb[k]) for k, c in zip(idx, cf))
        if amin > hi + 1e-9 or amax < lo - 1e-9:
            self.infeasible = True
            return
        if amin >= lo - 1e-9 and amax <= hi + 1e-9:
            return  # redundant
        self.rows.append((idx, cf, lo, hi))


# --------------------------------------------------------------------------------------
# Logical presolve
# --------------------------------------------------------------------------------------
class _Infeasible(Exception):
    pass


def _presolve(R, K, clues, full=True):
    """Returns (state dict).  Cells are ints u = r*K + c (0-based)."""
    N = R * K
    nbrs = []
    for u in range(N):
        r, c = divmod(u, K)
        nb = []
        if c + 1 < K: nb.append(u + 1)
        if c > 0: nb.append(u - 1)
        if r + 1 < R: nb.append(u + K)
        if r > 0: nb.append(u - K)
        nbrs.append(nb)
    C = len(clues)
    loc = [(r - 1) * K + (c - 1) for (r, c), _ in clues]
    val = [v for _, v in clues]
    owner = [-1] * N             # clue index whose clue cell this is
    for k, u in enumerate(loc):
        owner[u] = k
    wall = [False] * N           # forced wall
    gown = [-1] * N              # forced garden owner (includes clue cells)
    for k, u in enumerate(loc):
        gown[u] = k

    reach = [None] * C
    dist = [None] * C

    def bfs(k):
        # cell w may join garden k only if not a wall, not owned by another garden and not
        # adjacent to a cell owned by another garden
        src = loc[k]
        lim = val[k] - 1
        d = {src: 0}
        q = deque([src])
        while q:
            u = q.popleft()
            if d[u] >= lim:
                continue
            for w in nbrs[u]:
                if w in d or wall[w] or (gown[w] != -1 and gown[w] != k):
                    continue
                if any(gown[z] != -1 and gown[z] != k for z in nbrs[w]):
                    continue
                d[w] = d[u] + 1
                q.append(w)
        return d

    changed = True
    first = True
    while changed:
        changed = False
        for k in range(C):
            dist[k] = bfs(k)
            reach[k] = set(dist[k])
            if len(reach[k]) < val[k]:
                raise _Infeasible(f"clue {val[k]} at {clues[k][0]} cannot grow to its size")
            for u in range(N):
                if gown[u] == k and u not in reach[k]:
                    raise _Infeasible("a forced garden cell became unreachable")
        cand = [[] for _ in range(N)]
        for k in range(C):
            for u in reach[k]:
                cand[u].append(k)
        for u in range(N):
            if owner[u] == -1 and not wall[u] and not cand[u]:
                wall[u] = True
                changed = True
        if not (full or first):
            break
        first = False
        if not full:
            break
        # garden whose reachable set is exactly its size -> all forced
        for k in range(C):
            if len(reach[k]) == val[k]:
                for u in reach[k]:
                    if gown[u] == -1:
                        gown[u] = k
                        changed = True
        # cell with a single candidate that must be garden because of the 2x2 rule
        for u in range(N):
            r, c = divmod(u, K)
            if r + 1 < R and c + 1 < K:
                blk = (u, u + 1, u + K, u + K + 1)
                open_ = [w for w in blk if not wall[w]]
                if not open_:
                    raise _Infeasible("2x2 block of forced walls")
                if len(open_) == 1:
                    w = open_[0]
                    if gown[w] == -1:
                        if len(cand[w]) == 1:
                            gown[w] = cand[w][0]
                            changed = True
                        elif not cand[w]:
                            raise _Infeasible("2x2 block of forced walls")
        # a forced garden cell with a single candidate neighbour route is handled by the MILP
        # forced garden cell of k: it is in reach[k] only; also drop other candidates of it
        for u in range(N):
            if gown[u] != -1 and len(cand[u]) > 1:
                changed = True  # next BFS round removes it from other reaches
                cand[u] = [gown[u]]
        # count of forced cells per garden can't exceed its value
        cnt = [0] * C
        for u in range(N):
            if gown[u] != -1:
                cnt[gown[u]] += 1
        for k in range(C):
            if cnt[k] > val[k]:
                raise _Infeasible("garden over-full")
        if sum(wall) > N - sum(val):
            raise _Infeasible("too many forced walls")
    return dict(N=N, nbrs=nbrs, C=C, loc=loc, val=val, owner=owner, wall=wall, gown=gown,
                reach=reach, dist=dist)


# --------------------------------------------------------------------------------------
# Model construction
# --------------------------------------------------------------------------------------
def _enum_shapes(src, allowed, nbrs, size, must, cap):
    """All connected cell sets of `size` containing src inside `allowed` (Redelmeier's
    algorithm, each set generated exactly once).  Returns None if more than `cap` exist."""
    out = []
    cur = [src]

    def rec(frontier, seen):
        if len(cur) == size:
            if must <= set(cur):
                out.append(tuple(cur))
            return len(out) <= cap
        frontier = list(frontier)
        while frontier:
            u = frontier.pop()
            nf = list(frontier)
            ns = seen
            added = [w for w in nbrs[u] if w in allowed and w not in seen]
            if added:
                ns = seen | set(added)
                nf.extend(added)
            cur.append(u)
            ok = rec(nf, ns)
            cur.pop()
            if not ok:
                return False
        return True

    first = [w for w in nbrs[src] if w in allowed]
    if not rec(first, {src} | set(first)):
        return None
    return out


def _bfs_dist(src, nbrs, allowed):
    d = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        for w in nbrs[u]:
            if w not in d and allowed(w):
                d[w] = d[u] + 1
                q.append(w)
    return d


def _build(R, K, P, opt):
    N, nbrs, C, loc, val = P["N"], P["nbrs"], P["C"], P["loc"], P["val"]
    wall, gown, reach, owner = P["wall"], P["gown"], P["reach"], P["owner"]
    W = N - sum(val)
    m = _Lin()

    # ---- variables / constants ----
    g = [dict() for _ in range(C)]          # g[k][u] -> ref
    for k in range(C):
        for u in sorted(reach[k]):
            if gown[u] == k:
                g[k][u] = 1.0
            elif gown[u] != -1:
                continue
            else:
                g[k][u] = m.var(0, 1)
    x = [0.0] * N
    for u in range(N):
        if owner[u] != -1 or gown[u] != -1:
            x[u] = 0.0
        elif wall[u]:
            x[u] = 1.0
        else:
            x[u] = m.var(0, 1)

    def G(k, u):
        return g[k].get(u, 0.0)

    # (1) partition, (2) sizes, (+) wall count
    for u in range(N):
        if owner[u] != -1 or gown[u] != -1 or wall[u]:
            continue
        m.add([(g[k][u], 1) for k in range(C) if u in g[k]] + [(x[u], 1)], 1, 1)
    for k in range(C):
        m.add([(r, 1) for r in g[k].values()], val[k], val[k])
    m.add([(x[u], 1) for u in range(N)], W, W)

    # (3) different gardens can't touch: g[k,u] <= g[k,w] + x[w]
    for k in range(C):
        for u, ref in g[k].items():
            if isinstance(ref, float) and ref == 0.0:
                continue
            for w in nbrs[u]:
                m.add([(ref, 1), (G(k, w), -1), (x[w], -1)], hi=0)

    # (4) no 2x2 walls
    for u in range(N):
        r, c = divmod(u, K)
        if r + 1 < R and c + 1 < K:
            m.add([(x[w], 1) for w in (u, u + 1, u + K, u + K + 1)], hi=3)

    if opt["neighbour_cuts"]:
        # (5) every wall has a wall neighbour when W >= 2
        if W >= 2:
            for u in range(N):
                if not (isinstance(x[u], float) and x[u] == 0.0):
                    m.add([(x[u], 1)] + [(x[w], -1) for w in nbrs[u]], hi=0)
    # (6) every garden cell of a size>=2 garden has a same-garden neighbour.  Always added:
    # for v_c <= 3 it is what guarantees connectivity (grid is bipartite).
    for k in range(C):
        if val[k] >= 2:
            for u, ref in g[k].items():
                m.add([(ref, 1)] + [(G(k, w), -1) for w in nbrs[u]], hi=0)

    info = dict(g=g, x=x, W=W, root=None, garden_flow=set(), wall_flow=False)

    # ---- garden shapes (set partitioning) for gardens with few feasible polyominoes ----
    shaped = set()
    if opt["garden_conn"] in ("shapes", "shapes_level") and opt["connectivity"] != "hierarchy":
        for k in range(C):
            allowed = set(g[k])
            must = {u for u in allowed if gown[u] == k}
            shapes = _enum_shapes(loc[k], allowed, nbrs, val[k], must, opt["shape_limit"])
            if shapes is None:
                continue
            if not shapes:
                m.infeasible = True
                return m, info
            shaped.add(k)
            info["garden_flow"].add(k)
            z = [m.var(0, 1) for _ in shapes]
            m.add([(zz, 1) for zz in z], 1, 1)
            cover = {u: [] for u in allowed}
            bnd = {}
            for zz, sh in zip(z, shapes):
                ss = set(sh)
                for u in sh:
                    cover[u].append(zz)
                for w in {w for u in sh for w in nbrs[u] if w not in ss}:
                    bnd.setdefault(w, []).append(zz)
            for u in allowed:
                m.add([(g[k][u], 1)] + [(zz, -1) for zz in cover[u]], 0, 0)
            # every boundary cell of the chosen shape is a wall
            for w, zs in bnd.items():
                m.add([(zz, 1) for zz in zs] + [(x[w], -1)], hi=0)
    hier = opt["connectivity"] == "hierarchy" and W >= 2 and any(wall)

    # ---- garden connectivity (v >= 4) ----
    if opt["garden_conn"] in ("level", "shapes_level") and not hier:
        for k in range(C):
            if val[k] < 4 or k in shaped:
                continue
            info["garden_flow"].add(k)
            d = P["dist"][k]
            src = loc[k]
            v = val[k]
            lev = {u: (0.0 if u == src else m.var(d[u], v - 1, False)) for u in g[k]}
            for u in g[k]:
                if u == src:
                    continue
                par = []
                for w in nbrs[u]:
                    if w not in g[k] or (w != src and d[w] > v - 2):
                        continue
                    a = m.var(0, 1)
                    par.append(a)
                    m.add([(a, 1), (g[k][w], -1)], hi=0)
                    ubw = 0 if w == src else v - 1
                    M = ubw + 1 - d[u]
                    # l_u >= l_w + 1 - M (1 - a)
                    m.add([(lev[u], 1), (lev[w], -1), (a, -M)], lo=1 - M)
                m.add([(a, 1) for a in par] + [(g[k][u], -1)], 0, 0)
    elif opt["garden_conn"] in ("flow", "shapes") and not hier:
        for k in range(C):
            if val[k] < 4 or k in shaped:
                continue
            info["garden_flow"].add(k)
            d = P["dist"][k]
            src = loc[k]
            cells = list(g[k].keys())
            arcs_out = {u: [] for u in cells}
            arcs_in = {u: [] for u in cells}
            for u in cells:
                if u == src:
                    continue
                for w in nbrs[u]:
                    if w not in g[k]:
                        continue
                    cap = val[k] - 1 - d[w]   # subtree of u excludes the path w..root
                    cap = min(cap, val[k] - d[u])  # and u itself is at depth >= d[u]
                    if cap <= 0:
                        continue
                    f = m.var(0, cap, opt["flow_integer"])
                    arcs_out[u].append(f)
                    arcs_in[w].append(f)
                    m.add([(f, 1), (g[k][u], -cap)], hi=0)
                    m.add([(f, 1), (g[k][w], -cap)], hi=0)
            for u in cells:
                if u == src:
                    m.add([(f, 1) for f in arcs_in[u]], val[k] - 1, val[k] - 1)
                else:
                    m.add([(f, 1) for f in arcs_out[u]] + [(f, -1) for f in arcs_in[u]]
                          + [(g[k][u], -1)], 0, 0)

    # ---- wall connectivity ----
    forced = [u for u in range(N) if wall[u]]
    if W >= 2 and forced:
        open_ok = lambda w: owner[w] == -1 and gown[w] == -1
        if opt["root"] == "first":
            root = forced[0]
        else:
            best = None
            for u in forced:
                d = _bfs_dist(u, nbrs, open_ok)
                ecc = max(d.values())
                key = -ecc if opt["root"] == "far" else ecc
                if best is None or key > best[0]:
                    best = (key, u)
            root = best[1]
        info["root"] = root
    cand = [u for u in range(N) if not (isinstance(x[u], float) and x[u] == 0.0)]
    cset = set(cand)
    if hier:
        # one parent/level hierarchy for every region: roots = clue cells and the wall root
        info["wall_flow"] = True
        info["garden_flow"] = set(range(C))
        root = info["root"]
        droot = _bfs_dist(root, nbrs, lambda w: w in cset)
        lb, ub = {}, {}
        for u in range(N):
            los, his = [], []
            if u in droot and u in cset:
                los.append(droot[u]); his.append(W - 1)
            for k in range(C):
                if u in g[k]:
                    los.append(P["dist"][k][u]); his.append(val[k] - 1)
            if not los:
                continue
            lb[u], ub[u] = min(los), max(his)
        is_root = lambda u: owner[u] != -1 or u == root
        lev = {u: (0.0 if is_root(u) else m.var(lb[u], ub[u], False)) for u in lb}
        for u in cand:
            if u not in droot and u != root:
                m.add([(x[u], 1)], hi=0)
        for u in lb:
            if is_root(u):
                continue
            par = []
            for w in nbrs[u]:
                if w not in lb:
                    continue
                a = m.var(0, 1)
                par.append(a)
                # same region: a <= 1 - |x_u - x_w|
                m.add([(a, 1), (x[u], 1), (x[w], -1)], hi=1)
                m.add([(a, 1), (x[w], 1), (x[u], -1)], hi=1)
                ubw = 0 if is_root(w) else ub[w]
                M = ubw + 1 - lb[u]
                m.add([(lev[u], 1), (lev[w], -1), (a, -M)], lo=1 - M)
            m.add([(a, 1) for a in par], 1, 1)
    elif opt["wall_conn"] == "level" and W >= 2 and info["root"] is not None:
        info["wall_flow"] = True
        root = info["root"]
        droot = _bfs_dist(root, nbrs, lambda w: w in cset)
        lev = {u: (0.0 if u == root else m.var(droot[u], W - 1, False)) for u in droot}
        for u in cand:
            if u == root:
                continue
            if u not in droot:
                m.add([(x[u], 1)], hi=0)
                continue
            par = []
            for w in nbrs[u]:
                if w not in droot:
                    continue
                a = m.var(0, 1)
                par.append(a)
                m.add([(a, 1), (x[w], -1)], hi=0)
                M = (0 if w == root else W - 1) + 1 - droot[u]
                m.add([(lev[u], 1), (lev[w], -1), (a, -M)], lo=1 - M)
            m.add([(a, 1) for a in par] + [(x[u], -1)], 0, 0)
    elif opt["wall_conn"] in ("flow", "level") and W >= 2:
        # ('level' without a forced wall falls back to the flow with root variables)
        info["wall_flow"] = True
        root = info["root"]
        if root is None:
            # no forced wall: choose the root with variables y, first wall in reading order
            y = {u: m.var(0, 1) for u in cand}
            m.add([(y[u], 1) for u in cand], 1, 1)
            for u in cand:
                m.add([(y[u], 1), (x[u], -1)], hi=0)
            pref = []
            for u in cand:
                pref.append(y[u])
                m.add([(x[u], 1)] + [(r, -1) for r in pref], hi=0)
            dist_root = {u: 0 for u in cand}
        else:
            y = None
            dist_root = _bfs_dist(root, nbrs, lambda w: w in cset)
        M = W - 1
        out_a = {u: [] for u in cand}
        in_a = {u: [] for u in cand}
        for u in cand:
            if root is not None and u == root:
                continue
            if u not in dist_root:
                m.add([(x[u], 1)], hi=0)  # cannot reach the root at all
                continue
            for w in nbrs[u]:
                if w not in cset or w not in dist_root:
                    continue
                cap = M - dist_root[w] if root is not None else M
                if cap <= 0:
                    continue
                f = m.var(0, cap, opt["flow_integer"])
                out_a[u].append(f)
                in_a[w].append(f)
                m.add([(f, 1), (x[u], -cap)], hi=0)
                m.add([(f, 1), (x[w], -cap)], hi=0)
                if y is not None:
                    m.add([(f, 1), (y[u], cap)], hi=cap)  # root has no outflow
        for u in cand:
            if root is not None and u == root:
                m.add([(f, 1) for f in in_a[u]], M, M)
            elif y is None:
                m.add([(f, 1) for f in out_a[u]] + [(f, -1) for f in in_a[u]] + [(x[u], -1)], 0, 0)
            else:
                # net outflow = x - W*y
                m.add([(f, 1) for f in out_a[u]] + [(f, -1) for f in in_a[u]]
                      + [(x[u], -1), (y[u], W)], 0, 0)
    if opt["objective"] == "compact":
        # prefer garden cells close to their clue (steers the LP towards compact gardens)
        for k in range(C):
            for u, ref in g[k].items():
                if not isinstance(ref, float):
                    m.cost[ref] = m.cost.get(ref, 0) + P["dist"][k][u]
    elif opt["objective"] == "walls_far":
        for u in range(N):
            if not isinstance(x[u], float):
                m.cost[x[u]] = -1
    elif opt["objective"] == "random":
        import random
        rnd = random.Random(opt["random_seed"])
        for u in range(N):
            if not isinstance(x[u], float):
                m.cost[x[u]] = rnd.random()
    return m, info


# --------------------------------------------------------------------------------------
# Solution checks -> lazy cuts
# --------------------------------------------------------------------------------------
def _components(cells, nbrs):
    seen = set()
    comps = []
    for s in cells:
        if s in seen:
            continue
        comp = [s]
        seen.add(s)
        q = [s]
        while q:
            u = q.pop()
            for w in nbrs[u]:
                if w in cells and w not in seen:
                    seen.add(w)
                    comp.append(w)
                    q.append(w)
        comps.append(comp)
    return comps


def _val(ref, sol):
    return ref if isinstance(ref, float) else sol[ref]


def _separate(P, info, sol, opt):
    """Return list of cut rows (pairs, lo, hi) violated by sol; empty if sol is a valid grid."""
    nbrs, C, loc = P["nbrs"], P["C"], P["loc"]
    g, x, W = info["g"], info["x"], info["W"]
    N = P["N"]
    cuts = []
    # gardens
    for k in range(C):
        if k in info["garden_flow"] or P["val"][k] < 4:
            continue
        cells = {u for u, r in g[k].items() if _val(r, sol) > 0.5}
        comps = _components(cells, nbrs)
        if len(comps) <= 1:
            continue
        for comp in comps:
            if loc[k] in comp:
                continue
            S = set(comp)
            bd = {w for u in comp for w in nbrs[u] if w not in S and w in g[k]}
            for b in comp:
                cuts.append(([(g[k][w], 1) for w in bd] + [(g[k][b], -1)], 0, _INF))
    # walls
    if W >= 2 and not info["wall_flow"]:
        walls = {u for u in range(N) if _val(x[u], sol) > 0.5}
        comps = _components(walls, nbrs)
        if len(comps) > 1:
            root = info["root"]
            for ci, comp in enumerate(comps):
                S = set(comp)
                bd = {w for u in comp for w in nbrs[u] if w not in S}
                bd_pairs = [(x[w], 1) for w in bd]
                if root is not None:
                    if root in S:
                        # cells outside S∪N(S) that are walls must connect through N(S)
                        others = [u for c2 in comps if c2 is not comp for u in c2]
                        for b in others:
                            cuts.append((bd_pairs + [(x[b], -1)], 0, _INF))
                    else:
                        for b in comp:
                            cuts.append((bd_pairs + [(x[b], -1)], 0, _INF))
                else:
                    a = comp[0]
                    for c2 in comps:
                        if c2 is comp:
                            continue
                        b = c2[0]
                        cuts.append((bd_pairs + [(x[a], -1), (x[b], -1)], -1, _INF))
    return cuts


# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------
class _HighsBackend:
    def __init__(self, m, opt, verbose):
        import highspy
        import numpy as np
        self.np = np
        h = highspy.Highs()
        h.setOptionValue("output_flag", bool(verbose))
        h.setOptionValue("random_seed", int(opt["random_seed"]))
        if opt["mip_heuristic_effort"] is not None:
            h.setOptionValue("mip_heuristic_effort", float(opt["mip_heuristic_effort"]))
        for k, v in (opt["highs_options"] or {}).items():
            h.setOptionValue(k, v)
        n = len(m.lb)
        self.n = n
        self.h = h
        self.highspy = highspy
        if n:
            h.addVars(n, np.array(m.lb, dtype=float), np.array(m.ub, dtype=float))
            if m.cost:
                ks = list(m.cost)
                h.changeColsCost(len(ks), np.array(ks, dtype=np.int32),
                                 np.array([float(m.cost[k]) for k in ks]))
                h.setOptionValue("mip_rel_gap", 1e30)   # any feasible point is a solution
                h.setOptionValue("mip_abs_gap", 1e30)
            h.changeColsIntegrality(n, np.arange(n, dtype=np.int32),
                                    np.array([highspy.HighsVarType.kInteger if t else highspy.HighsVarType.kContinuous
                                              for t in m.integer]))
        self.add_rows(m.rows)

    def add_rows(self, rows):
        np = self.np
        if not rows:
            return
        lo = np.array([r[2] for r in rows], dtype=float)
        hi = np.array([r[3] for r in rows], dtype=float)
        starts, idx, val = [], [], []
        for r in rows:
            starts.append(len(idx))
            idx.extend(r[0])
            val.extend(r[1])
        self.h.addRows(len(rows), lo, hi, len(idx), np.array(starts, dtype=np.int32),
                       np.array(idx, dtype=np.int32), np.array(val, dtype=float))

    def run(self, time_left):
        h = self.h
        hs = self.highspy
        if time_left is not None:
            h.setOptionValue("time_limit", max(time_left, 0.01))
        if self.n == 0:
            return "optimal", []
        h.run()
        st = h.getModelStatus()
        if st == hs.HighsModelStatus.kOptimal:
            return "optimal", list(h.getSolution().col_value)
        if st == hs.HighsModelStatus.kInfeasible:
            return "infeasible", None
        if st == hs.HighsModelStatus.kTimeLimit:
            return "time_limit", None
        return "error", h.modelStatusToString(st)


class _CpSatBackend:
    def __init__(self, m, opt, verbose):
        from ortools.sat.python import cp_model
        self.cp = cp_model
        self.model = cp_model.CpModel()
        self.vars = []
        for lb, ub in zip(m.lb, m.ub):
            if lb == 0 and ub == 1:
                self.vars.append(self.model.NewBoolVar(""))
            else:
                self.vars.append(self.model.NewIntVar(int(lb), int(ub), ""))
        self.opt = opt
        self.verbose = verbose
        self.add_rows(m.rows)

    def add_rows(self, rows):
        V = self.vars
        LE = self.cp.LinearExpr
        for idx, cf, lo, hi in rows:
            e = LE.WeightedSum([V[i] for i in idx], [int(c) for c in cf])
            lo_i = int(math.ceil(lo - 1e-9)) if lo > -_INF else None
            hi_i = int(math.floor(hi + 1e-9)) if hi < _INF else None
            if lo_i is not None and hi_i is not None and lo_i == hi_i:
                self.model.Add(e == lo_i)
            else:
                if lo_i is not None:
                    self.model.Add(e >= lo_i)
                if hi_i is not None:
                    self.model.Add(e <= hi_i)

    def run(self, time_left):
        cp = self.cp
        s = cp.CpSolver()
        s.parameters.random_seed = int(self.opt["random_seed"])
        s.parameters.log_search_progress = bool(self.verbose)
        if self.opt["threads"]:
            s.parameters.num_workers = int(self.opt["threads"])
        if time_left is not None:
            s.parameters.max_time_in_seconds = max(time_left, 0.01)
        st = s.Solve(self.model)
        if st in (cp.OPTIMAL, cp.FEASIBLE):
            return "optimal", [s.Value(v) for v in self.vars]
        if st == cp.INFEASIBLE:
            return "infeasible", None
        if st == cp.UNKNOWN:
            return "time_limit", None
        return "error", s.StatusName(st)


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------
def solve(num_rows: int, num_cols: int, clues: list[tuple[tuple[int, int], int]],
          time_limit: float | None = None, *, verbose: bool = False, **options) -> dict:
    """clues: list of ((row, col), value) with 1-INDEXED row/col.
    Returns dict with keys:
      'status': str  -> one of 'optimal' (solution found), 'infeasible', 'time_limit', 'error'
      'grid': list[list[str]] | None -> num_rows x num_cols; '#' wall, '.' garden cell, str(value) for clue cells
      'solve_time': float  (seconds, total including model build)
      'message': str  (human-readable detail)
    Optional keyword options (see DEFAULT_OPTIONS): backend='highs'|'cpsat', garden_conn, wall_conn,
    root, presolve, neighbour_cuts, random_seed, threads, mip_heuristic_effort.
    """
    t0 = time.perf_counter()

    def out(status, grid, msg):
        return {"status": status, "grid": grid, "solve_time": time.perf_counter() - t0,
                "message": msg}

    opt = dict(DEFAULT_OPTIONS)
    unknown = set(options) - set(opt)
    if unknown:
        return out("error", None, f"unknown option(s): {sorted(unknown)}")
    opt.update(options)

    # ---- input validation ----
    try:
        R, K = int(num_rows), int(num_cols)
        clue_list = [((int(r), int(c)), int(v)) for (r, c), v in clues]
    except Exception as exc:  # noqa: BLE001
        return out("error", None, f"malformed input: {exc}")
    if R < 1 or K < 1:
        return out("error", None, "grid must have at least one row and one column")
    seen = set()
    for (r, c), v in clue_list:
        if not (1 <= r <= R and 1 <= c <= K):
            return out("error", None, f"clue at ({r}, {c}) is outside the {R}x{K} grid")
        if (r, c) in seen:
            return out("error", None, f"duplicate clue at ({r}, {c})")
        seen.add((r, c))
        if v < 1:
            return out("error", None, f"clue at ({r}, {c}) has non-positive value {v}")
    total = sum(v for _, v in clue_list)
    if total > R * K:
        return out("infeasible", None,
                   f"clue values sum to {total} but the grid has only {R * K} cells")

    def grid_from(is_wall):
        grid = [["#" if is_wall[r * K + c] else "." for c in range(K)] for r in range(R)]
        for (r, c), v in clue_list:
            grid[r - 1][c - 1] = str(v)
        return grid

    # ---- presolve ----
    try:
        P = _presolve(R, K, clue_list, full=(opt["presolve"] == "full"))
    except _Infeasible as exc:
        return out("infeasible", None, f"presolve proved infeasibility: {exc}")
    m, info = _build(R, K, P, opt)
    if m.infeasible:
        return out("infeasible", None, "a constraint is violated by presolve-fixed cells")

    try:
        if opt["backend"] == "highs":
            be = _HighsBackend(m, opt, verbose)
        elif opt["backend"] == "cpsat":
            be = _CpSatBackend(m, opt, verbose)
        else:
            return out("error", None, f"unknown backend {opt['backend']!r}")
    except ImportError as exc:
        return out("error", None, f"solver backend not available: {exc}")

    rounds = 0
    ncuts = 0
    while True:
        rounds += 1
        left = None if time_limit is None else time_limit - (time.perf_counter() - t0)
        if left is not None and left <= 0:
            return out("time_limit", None, f"time limit reached after {rounds - 1} round(s)")
        status, sol = be.run(left)
        if status != "optimal":
            msg = {"infeasible": "the puzzle has no solution",
                   "time_limit": "time limit reached before a solution was found"}.get(
                status, f"solver error: {sol}")
            return out(status, None, msg + f" (round {rounds}, {ncuts} lazy cuts)")
        cuts = _separate(P, info, sol, opt)
        if not cuts:
            break
        cm = _Lin()
        cm.lb, cm.ub = m.lb, m.ub
        for pairs, lo, hi in cuts:
            cm.add(pairs, lo, hi)
        if cm.infeasible:
            return out("infeasible", None, "the puzzle has no solution")
        be.add_rows(cm.rows)
        ncuts += len(cm.rows)

    is_wall = [_val(info["x"][u], sol) > 0.5 for u in range(P["N"])]
    nvars = len(m.lb)
    return out("optimal", grid_from(is_wall),
               f"solved with {opt['backend']}: {nvars} vars, {len(m.rows)} rows, "
               f"{rounds} solve round(s), {ncuts} lazy cuts, "
               f"{sum(P['wall'])} walls fixed by presolve")


if __name__ == "__main__":
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "nurikabe_puzzle2.csv")
    backend = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OPTIONS["backend"]
    R, K, cl = load_csv(path)
    res = solve(R, K, cl, backend=backend)
    print(f"{os.path.basename(path)}: {R}x{K}, {len(cl)} clues")
    print(f"status: {res['status']}  ({res['message']})")
    if res["grid"]:
        print(format_grid(res["grid"]))
    print(f"total time: {res['solve_time']:.3f} s")
