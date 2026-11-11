"""Independent Nurikabe solution checker (shares no code with the solver).

check(num_rows, num_cols, clues, grid) -> list of error strings (empty == valid).
grid uses '#' for wall, anything else for garden; clues are ((row, col), value), 1-indexed.
"""


def check(num_rows, num_cols, clues, grid):
    errs = []
    if grid is None or len(grid) != num_rows or any(len(r) != num_cols for r in grid):
        return ["grid has wrong shape"]
    clue_at = {(r - 1, c - 1): v for (r, c), v in clues}
    for (r, c), v in clue_at.items():
        if grid[r][c] != str(v):
            errs.append(f"clue cell {(r + 1, c + 1)} shows {grid[r][c]!r}, expected {v}")
    for r in range(num_rows):
        for c in range(num_cols):
            s = grid[r][c]
            if (r, c) not in clue_at and s not in ("#", "."):
                errs.append(f"cell {(r + 1, c + 1)} has invalid symbol {s!r}")

    def wall(r, c):
        return grid[r][c] == "#"

    def nb(r, c):
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p, q = r + dr, c + dc
            if 0 <= p < num_rows and 0 <= q < num_cols:
                yield p, q

    def flood(start, same):
        seen = {start}
        st = [start]
        while st:
            u = st.pop()
            for w in nb(*u):
                if w not in seen and same(w):
                    seen.add(w)
                    st.append(w)
        return seen

    # gardens: each orthogonally connected garden component contains exactly one clue of matching size
    done = set()
    for r in range(num_rows):
        for c in range(num_cols):
            if wall(r, c) or (r, c) in done:
                continue
            comp = flood((r, c), lambda w: not wall(*w))
            done |= comp
            cl = [clue_at[u] for u in comp if u in clue_at]
            if len(cl) != 1:
                errs.append(f"garden containing {(r + 1, c + 1)} has {len(cl)} clues")
            elif cl[0] != len(comp):
                errs.append(f"garden of clue {cl[0]} has size {len(comp)}")
    # walls connected
    walls = [(r, c) for r in range(num_rows) for c in range(num_cols) if wall(r, c)]
    if walls and len(flood(walls[0], lambda w: wall(*w))) != len(walls):
        errs.append("walls are not connected")
    # no 2x2 walls
    for r in range(num_rows - 1):
        for c in range(num_cols - 1):
            if wall(r, c) and wall(r + 1, c) and wall(r, c + 1) and wall(r + 1, c + 1):
                errs.append(f"2x2 wall block at {(r + 1, c + 1)}")
    return errs
