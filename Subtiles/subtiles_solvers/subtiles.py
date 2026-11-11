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

------------------------------------------------------------------------------------
The rules as this code models them
------------------------------------------------------------------------------------
Place N non-overlapping shapes on the grid, one of each size 1, 2, ..., N. Call the
shape of size K the "K-shape" or "level K".

  * Each K-shape must be the (K-1)-shape plus one extra cell. Before adding the cell
    the (K-1)-shape may be rotated, reflected and moved anywhere, so the two copies
    don't have to sit in the same place on the grid. Only their forms have to match.
  * A clue v in a cell means that cell belongs to the v-shape.
  * Cells may stay empty, and N isn't given: it is at least the largest clue, and at
    most the biggest N with 1 + 2 + ... + N <= H * W.

------------------------------------------------------------------------------------
Two words used everywhere below
------------------------------------------------------------------------------------
  shape      A form with no position: a tuple of (row, col) cells, moved so its top-
             left is at (0, 0) and put in canonical orientation (see canonical). An L
             tetromino is one shape however it is turned or flipped.
  placement  A shape put down on the grid: a frozenset of real grid cells.

The main table is placements_at[K][shape] = {placements}: every way level K could
look, and every place each of those could go. The solver keeps shrinking it until
CP-SAT can choose.
"""
import math
import os
import sys
import time
from collections import defaultdict
from ortools.sat.python import cp_model

# Orthogonal neighbour offsets (row change, col change): down, up, right, left.
NEIGHBOR_OFFSETS = ((1, 0), (-1, 0), (0, 1), (0, -1))


# ======================================================================================
# Shape geometry: rotate, reflect, normalise
# ======================================================================================

def transform(point, symmetry):
    """Apply symmetry 0-7 to point: 0-3 are quarter turns, 4-7 add a mirror."""
    # 0-3 rotate by 0/90/180/270 degrees, using (r, c) -> (c, -r) for each quarter turn.
    # 4-7 are the same rotations followed by a left/right mirror (c -> -c).
    # 4 rotations x 2 mirrors = all 8 symmetries of a square.
    r, c = point
    match symmetry:
        case 0: return r, c        # identity
        case 1: return c, -r       # 90
        case 2: return -r, -c      # 180
        case 3: return -c, r       # 270
        case 4: return r, -c       # mirror
        case 5: return c, r        # 90 + mirror (transpose)
        case 6: return -r, c       # 180 + mirror
        case 7: return -c, -r      # 270 + mirror (anti-transpose)
    raise ValueError(f"symmetry index must be 0-7, got {symmetry}")


def normalize(points):
    """Translate points so the min row and column are 0, returned as a sorted tuple."""
    # Rotation can make coordinates negative, so slide the shape back so its smallest
    # row and column are 0. Sorting gives one fixed tuple for each set of cells, so two
    # copies of the same orientation compare and hash equal.
    min_row = min(r for r, _ in points)
    min_col = min(c for _, c in points)
    return tuple(sorted((r - min_row, c - min_col) for r, c in points))


def orientations(shape):
    """All distinct normalised rotations and reflections of shape."""
    # A set, because symmetric shapes repeat: a square gives 1 orientation, a straight
    # line 2, an L tetromino all 8.
    return {normalize([transform(point, sym) for point in shape]) for sym in range(8)}


def canonical(shape):
    """Canonical form of shape: the smallest of its orientations, so congruent shapes match."""
    # Every orientation of a shape gives the same min, so this is a unique name per
    # shape and works as a dict key.
    return min(orientations(shape))


# ======================================================================================
# Solver
# ======================================================================================

def solve(clues, H, W, verbose=True):
    """Solve a Subtiles puzzle.

    clues maps (row, col) to its clue value, on an H x W grid. Grows the shapes level by
    level with presolve after each one, then tries each candidate N from the largest down
    with CP-SAT. Returns (N, grid, unique), where grid[r][c] is the size of the shape
    covering the cell (0 if none), or None if no N works.
    """
    start_time = time.time()
    log = print if verbose else (lambda *a, **k: None)

    # clue_cells[K] = cells whose clue is K, i.e. cells every K-placement must cover.
    clue_cells = defaultdict(set)
    for cell, value in clues.items():
        clue_cells[value].add(cell)

    # Bounds on N. Every clue level has to exist, so N >= the largest clue. The shapes
    # 1..N use 1 + 2 + ... + N = N(N+1)/2 cells, which must fit in the grid. Solving
    # N(N+1)/2 <= H*W for N with the quadratic formula gives the largest such N:
    #     N = floor((sqrt(8*H*W + 1) - 1) / 2)
    # isqrt is an exact integer square root (rounded down), so there's no float error.
    n_min = max(clues.values())
    n_max = (math.isqrt(8 * H * W + 1) - 1) // 2

    placements_at = {}                       # placements_at[K][shape] = set of placements
    parents = {}                             # parents[K][shape] = set of (K-1)-shapes
    # children[K][shape] = K+1 shapes grown from it (parents with the arrows reversed).
    children = defaultdict(lambda: defaultdict(set))
    # forced[cell] = K means the cell has been proven to belong to the K-shape. Clues
    # are not copied in here; they're checked separately through `clues`.
    forced = {}
    total_grown = 0

    def presolve(top_level, n_known):
        """Run the presolve cycle to a fixpoint over levels 1..top_level.
        Child pruning applies to K < n_known; forcing uses K <= n_known."""
        # n_known matters because we don't know N. Levels 1..n_known are certain to be
        # in the answer; levels above it might not be (N could stop before them).
        # Rules that need a level to exist only use those certain levels:
        #   - "a K-shape needs a (K+1)-child" is only true if level K+1 exists
        #   - "every K-placement covers this cell, so it's forced" is only true if
        #     level K exists
        # Returns (feasible, rounds). False means a contradiction was found.
        rounds = 0
        while True:
            rounds += 1
            changed = False

            # ---- 1. shape pruning: sweep up and down until stable ----
            while True:
                moved = False
                # Upward sweep: drop a shape if it has no placements left, or if none
                # of the (K-1)-shapes it can grow from still survive. Going bottom-up
                # lets one removal cascade to higher levels in the same pass.
                for K in range(1, top_level + 1):
                    for shape in list(placements_at[K]):
                        no_placements = not placements_at[K][shape]
                        no_parent = K > 1 and not any(
                            parent in placements_at[K - 1] for parent in parents[K][shape])
                        if no_placements or no_parent:
                            del placements_at[K][shape]; moved = True
                # Downward sweep: drop a shape if no surviving K+1 shape grows from it.
                # Only for K+1 <= n_known, the levels we know exist. Top-down for the
                # same cascade reason.
                for K in range(min(top_level, n_known) - 1, 0, -1):
                    for shape in list(placements_at[K]):
                        if not any(child in placements_at[K + 1] for child in children[K][shape]):
                            del placements_at[K][shape]; moved = True
                if not moved:
                    break
                changed = True
            # A level with no shapes left can never be filled: contradiction.
            for K in range(1, top_level + 1):
                if not placements_at[K]:
                    return False, rounds

            # ---- 2. forced cells ----
            # For each level that must exist: if a cell is in every remaining placement
            # of every remaining shape, the K-shape covers that cell whatever is chosen.
            new_forced = {}
            for K in range(1, min(top_level, n_known) + 1):
                all_placements = [p for group in placements_at[K].values() for p in group]
                for cell in frozenset.intersection(*all_placements):
                    # Forced to two different levels at once is impossible.
                    if new_forced.get(cell, K) != K or forced.get(cell, K) != K:
                        return False, rounds
                    new_forced[cell] = K
            if any(cell not in forced for cell in new_forced):
                changed = True
            forced.update(new_forced)

            # ---- 3. clash removal ----
            # Remove every placement that uses a cell forced to another level. This
            # can empty shapes, so step 1 runs again on the next round.
            for K in range(1, top_level + 1):
                for shape in placements_at[K]:
                    clashing = {p for p in placements_at[K][shape]
                                if any(forced.get(cell, K) != K for cell in p)}
                    if clashing:
                        placements_at[K][shape] -= clashing; changed = True

            # Stop at a fixpoint, when a full round changes nothing.
            if not changed:
                return True, rounds

    # ---------- interleaved growth, done in placement space ----------
    # Working on placements instead of free-floating shapes means a shape only exists
    # if it fits somewhere on the grid, around the clues and forced cells. That keeps
    # the counts far below the full number of polyominoes of each size.

    # canonical() tries all 8 orientations, and the same form comes up again and again
    # in different positions. Cache it, keyed on the translation-free form.
    canonical_cache = {}
    def shape_of(placement):
        """Canonical shape of a placement, memoised on its normalised form."""
        normalized = normalize(list(placement))
        shape = canonical_cache.get(normalized)
        if shape is None:
            shape = canonical(normalized); canonical_cache[normalized] = shape
        return shape
    def cell_allowed(cell, K):
        """True if cell is on the grid and may belong to a K-shape (no conflicting clue or forced value)."""
        # clues.get(cell, K) != K: the cell holds a different clue.
        # forced.get(cell, K) != K: the cell is proven to belong to another level.
        # (Using K as the default means "no clue / not forced" passes.)
        r, c = cell
        return 0 <= r < H and 0 <= c < W and clues.get(cell, K) == K and forced.get(cell, K) == K

    # Level 1 is the single cell ("monomino"). It has no parent. Its placements are the
    # cells allowed for level 1 that cover every 1-clue: just the clue cell if there's
    # one 1-clue, every free cell if there are none, nothing if there are two or more.
    monomino = ((0, 0),)
    placements_at[1] = {monomino: {frozenset({(r, c)}) for r in range(H) for c in range(W)
                                   if cell_allowed((r, c), 1)
                                   and clue_cells.get(1, set()) <= {(r, c)}}}
    parents[1] = {monomino: set()}
    top_level = 1                            # highest level built so far
    for K in range(2, n_max + 1):
        required = clue_cells.get(K, set())  # cells that every K-placement must cover
        num_required = len(required)
        new_placements = defaultdict(set); new_parents = defaultdict(set)
        # Bounding-box check. A K-placement covers every K-clue, so it must span at
        # least the clues' rows and columns. The parent copy is that placement minus one
        # cell, and adding one neighbouring cell grows the bounding box by at most 1 in
        # one direction only. So an orientation whose height + width falls short of the
        # clues' span by 2 or more can never work, and is skipped before trying shifts.
        if required:
            need_h = max(r for r, _ in required) - min(r for r, _ in required) + 1
            need_w = max(c for _, c in required) - min(c for _, c in required) + 1
        else:
            need_h = need_w = 1
        # A K-placement = a copy of some surviving (K-1)-shape, in any orientation and
        # position, plus one cell next to it. Rather than trying every position, aim the
        # copy at the K-clues:
        #   - The added cell can cover at most one clue, so the copy must already cover
        #     all of them, or all but one.
        #   - 2+ clues: the copy covers at least one clue, so it's enough to try shifts
        #     that put a cell of the copy on a clue.
        #   - 1 clue: either the copy covers the clue, or the clue is the added cell, in
        #     which case the copy sits next to it. Try both kinds of shift.
        #   - 0 clues: no clue to aim at, so try every position on the grid.
        for parent in placements_at[K - 1]:
            for orient in orientations(parent):
                height = max(r for r, _ in orient) + 1
                width = max(c for _, c in orient) + 1
                if max(0, need_h - height) + max(0, need_w - width) > 1:
                    continue                 # too short or too narrow to reach every clue
                if num_required == 0:
                    shifts = {(dr, dc) for dr in range(H - height + 1) for dc in range(W - width + 1)}
                else:
                    # Shifts that put some cell of orient onto some K-clue.
                    shifts = {(clue_r - r, clue_c - c) for clue_r, clue_c in required for r, c in orient}
                    if num_required == 1:
                        # Plus shifts that put some cell of orient next to the single clue.
                        (clue_r, clue_c), = required
                        shifts |= {(clue_r + nr - r, clue_c + nc - c)
                                   for nr, nc in NEIGHBOR_OFFSETS for r, c in orient}
                for dr, dc in shifts:
                    # Cheap position check before building the copy. The copy's bounding
                    # box is rows dr..dr+height-1, cols dc..dc+width-1. It must sit on the
                    # grid, and at most one K-clue may lie outside it; that one must be
                    # directly next to the box (1 step out in one direction only), since
                    # the added cell has to cover it.
                    if dr < 0 or dc < 0 or dr + height > H or dc + width > W:
                        continue
                    outside = 0
                    hopeless = False
                    for clue_r, clue_c in required:
                        # how many rows / columns this clue lies beyond the box (0 = within)
                        out_r = max(dr - clue_r, clue_r - (dr + height - 1), 0)
                        out_c = max(dc - clue_c, clue_c - (dc + width - 1), 0)
                        if out_r or out_c:
                            outside += 1
                            if outside > 1 or out_r + out_c > 1:
                                hopeless = True
                                break
                    if hopeless:
                        continue
                    parent_copy = frozenset((r + dr, c + dc) for r, c in orient)
                    # Every cell of the copy must be legal for level K.
                    if not all(cell_allowed(cell, K) for cell in parent_copy):
                        continue
                    missing = required - parent_copy
                    if len(missing) > 1:
                        continue             # one added cell can't cover two clues
                    if missing:
                        # Exactly one clue missing: the added cell has to be that clue,
                        # so it has to sit next to the copy.
                        clue_cell = next(iter(missing))
                        if not any((clue_cell[0] + nr, clue_cell[1] + nc) in parent_copy
                                   for nr, nc in NEIGHBOR_OFFSETS):
                            continue
                        candidates = (clue_cell,)
                    else:
                        # All clues covered: any cell bordering the copy can be added.
                        candidates = {(r + nr, c + nc) for r, c in parent_copy
                                      for nr, nc in NEIGHBOR_OFFSETS} - parent_copy
                    for added_cell in candidates:
                        if not cell_allowed(added_cell, K):
                            continue
                        placement = parent_copy | {added_cell}   # the K-placement
                        shape = shape_of(placement)             # its shape
                        # Many (parent, orient, shift, added_cell) combinations make the
                        # same placement; the sets remove duplicates. Remember that
                        # shape grows from parent.
                        new_placements[shape].add(placement); new_parents[shape].add(parent)
        total_grown += len(new_placements)
        parents[K] = new_parents
        placements_at[K] = dict(new_placements)
        # Fill in the reverse links for the downward sweep in presolve().
        for shape, shape_parents in new_parents.items():
            for parent in shape_parents:
                children[K - 1][parent].add(shape)
        if not placements_at[K]:
            # Level K can't be built at all, so N is at most K - 1. Stop growing.
            log(f"K={K:2d}: grown {len(new_placements):6d}  none survive -> N <= {K-1}")
            del placements_at[K]
            break
        top_level = K
        # Presolve right away so the next level grows from fewer shapes. Pruning early
        # is what keeps growth small. n_known = n_min: only levels up to the largest
        # clue are known to exist at this point.
        ok, rounds = presolve(top_level, n_min)
        if not ok:
            log(f"K={K:2d}: contradiction during presolve")
            return None
        log(f"K={K:2d}: shapes {len(new_placements):6d}  "
            f"after cycle {len(placements_at[K]):6d}  "
            f"placements {sum(map(len, placements_at[K].values())):6d}  forced {len(forced):3d}  "
            f"({rounds} rnds)  [{time.time()-start_time:.1f}s]", flush=True)

    log(f"total shapes grown: {total_grown}")
    # ---------- try each candidate N, largest first ----------
    # Going from the top down, the first N that works is the largest possible one.
    for N in range(top_level, n_min - 1, -1):
        # Fixing N adds assumptions (levels 1..N all exist, nothing above N), so the
        # presolve can prune much more. Those prunings are only valid for this N, so
        # save the state first and restore it if this N fails.
        saved_placements = {K: {shape: set(group) for shape, group in placements_at[K].items()}
                            for K in placements_at}
        saved_forced = dict(forced)
        for K in list(placements_at):
            if K > N:
                del placements_at[K]
        # n_known = N: every level up to N must exist. Level N needs no child, and
        # presolve()'s downward sweep stops below N.
        ok, rounds = presolve(N, N)
        if ok:
            result = _cpsat(N, placements_at, parents, H, W, log, start_time)
            if result:
                return result
        log(f"N={N}: infeasible")
        placements_at.clear(); placements_at.update(saved_placements)
        forced.clear(); forced.update(saved_forced)
    return None


def _cpsat(N, placements_at, parents, H, W, log, start_time):
    """Pick one placement per level 1..N with CP-SAT.

    Constraints: no cell covered twice, and a K-shape can only be used if one of its
    parent (K-1)-shapes is also used. Returns (N, grid, unique) or None if infeasible.
    Uniqueness is checked by banning the found set of placements and re-solving.
    """
    # The only variables are one boolean per surviving placement: "use this one".
    # Clue rules need no constraint. Every K-placement already covers all K-clues,
    # and no other level's placements can include a clue cell, so picking one
    # placement per level satisfies every clue automatically.
    model = cp_model.CpModel()
    vars_using_cell = defaultdict(list)      # cell -> vars of placements that use it
    variables = {}                           # (K, shape) -> [(var, placement), ...]
    for K in range(1, N + 1):
        level_vars = []
        for shape, group in placements_at[K].items():
            variables[(K, shape)] = [(model.NewBoolVar(""), placement) for placement in group]
            for var, placement in variables[(K, shape)]:
                level_vars.append(var)
                for cell in placement:
                    vars_using_cell[cell].append(var)
        # Exactly one placement per level: one K-shape, in one spot.
        model.AddExactlyOne(level_vars)
    # No overlaps: each cell is used by at most one chosen placement.
    for cell_vars in vars_using_cell.values():
        model.AddAtMostOne(cell_vars)
    # Growth chain. Using shape s at level K requires the level K-1 choice to be one of
    # s's parent shapes, in any of its placements (parents don't have to be in the same
    # spot). Left side: is any placement of s chosen (0 or 1). Right side: is level K-1
    # using one of s's parents (0 or 1).
    for K in range(2, N + 1):
        for shape in placements_at[K]:
            parent_vars = [var for parent in parents[K][shape] if parent in placements_at[K - 1]
                           for var, _ in variables[(K - 1, parent)]]
            model.Add(sum(var for var, _ in variables[(K, shape)]) <= sum(parent_vars))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    num_vars = sum(len(group) for group in variables.values())
    log(f"N={N}: {num_vars} vars, {solver.StatusName(status)} in {solver.WallTime():.2f}s  "
        f"[total {time.time()-start_time:.1f}s]")
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    # Read the answer: write K into every cell of the chosen K-placement.
    chosen = []
    grid = [[0] * W for _ in range(H)]
    for (K, shape), group in variables.items():
        for var, placement in group:
            if solver.Value(var):
                chosen.append(var)
                for r, c in placement:
                    grid[r][c] = K
    # Uniqueness: ban this exact set of placements (at least one must change) and
    # solve again. If nothing else is feasible, the solution is unique.
    model.AddBoolOr([var.Not() for var in chosen])
    unique = cp_model.CpSolver().Solve(model) == cp_model.INFEASIBLE
    log(f"unique: {unique}")
    return N, grid, unique

def main():
    """Read the puzzle file given on the command line, solve it, and print the grid and row sums."""
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "subtiles_puzzles", "subtiles2.txt")
    path = sys.argv[1] if len(sys.argv) > 1 else default
    rows = [line.split() for line in open(path) if line.strip()]
    H, W = len(rows), len(rows[0])
    # clues[(r, c)] = value for each numbered cell; "." cells are blank.
    clues = {(r, c): int(token) for r, row in enumerate(rows)
             for c, token in enumerate(row) if token != "."}
    result = solve(clues, H, W)
    if not result:
        print("no solution")
        return
    N, grid, unique = result
    for row in grid:
        print(" ".join(f"{value:2d}" if value else " ." for value in row))
    # The puzzle's final answer is built from the row sums: min row sum x max row sum.
    row_sums = [sum(row) for row in grid]
    print("row sums", row_sums)
    print("min * max =", min(row_sums) * max(row_sums), "| unique:", unique)


if __name__ == "__main__":
    main()
