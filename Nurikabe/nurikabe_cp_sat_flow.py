"""
Nurikabe in Google OR-Tools CP-SAT, translated constraint for constraint from the
flow-based MILP in nurikabe.py. Constraint numbers match that file.

Connectivity is enforced by choosing a root for each region including the wall
The root has no outflow and must have inflow equal to the size of the region minus 1
All other cells have net +1 outflow and may only flow to cells of the same region
This enforces that regions are connected and the correct size

The only difference is that flow is an integer here because CP-SAT has no continuous
variables. This loses nothing: with the binaries fixed the flow is a network flow
problem with integer supplies, so if any flow is feasible an integer one is too.

Cells are 1-indexed (row i, column j). Clues are given as a dataframe of
((row, col), value) pairs.
"""
from ortools.sat.python import cp_model
import pandas as pd
import numpy as np


def build_model(num_rows, num_cols, clues_df):
    m = cp_model.CpModel()

    # ---------- Index sets ----------
    C = list(clues_df.index)
    IJ = [(i, j) for i in range(1, num_rows + 1) for j in range(1, num_cols + 1)]

    # O_(i,j): orthogonal neighbours that exist on the grid
    def neighbours(i, j):
        cand = [(i, j + 1), (i, j - 1), (i + 1, j), (i - 1, j)]
        return [(p, q) for p, q in cand if 1 <= p <= num_rows and 1 <= q <= num_cols]

    # Directed edges (i,j) -> (p,q), used to index the flow variables
    E = [(i, j, p, q) for i, j in IJ for p, q in neighbours(i, j)]

    # ---------- Parameters ----------
    L = clues_df["coords"].to_dict()   # clue coordinates
    V = clues_df["values"].to_dict()   # clue values
    clue_cells = set(L.values())
    num_walls = num_rows * num_cols - sum(V.values())
    # Big-M: no flow on an edge can exceed the size of the largest region
    M = max([num_walls] + list(V.values()))

    # ---------- Variables ----------
    x = {(i, j): m.NewBoolVar(f"x_{i}_{j}") for i, j in IJ}               # 1 if wall
    # 1 if cell in garden c
    # 13. A garden can't reach cells too far from its clue
    # A connected garden of size V[c] has every cell within V[c]-1 steps of its clue,
    # so cells further away don't get a variable for that clue at all
    g = {(i, j, c): m.NewBoolVar(f"g_{i}_{j}_{c}") for i, j in IJ for c in C
         if abs(i - L[c][0]) + abs(j - L[c][1]) <= V[c] - 1}
    # Cells that can't be in any garden must be walls. Garden c can't use a cell it can't
    # reach, or one touching a different clue since different gardens can't touch
    forced_walls = [(i, j) for i, j in IJ if (i, j) not in clue_cells
                    and not any((i, j, c) in g
                                and not any(L[d] in neighbours(i, j) for d in C if d != c)
                                for c in C)]
    y ={(i, j): m.NewBoolVar(f"y_{i}_{j}") for i, j in IJ}               # 1 if wall root
    f = {e: m.NewIntVar(-M, M, "f_%d_%d_%d_%d" % e) for e in E}           # flow (i,j)->(p,q)

    # 1. Each cell is in exactly one garden or is a wall
    for i, j in IJ:
        m.AddExactlyOne([g[i, j, c] for c in C if (i, j, c) in g] + [x[i, j]])

    # 2. Each garden has the right number of cells
    for c in C:
        m.Add(sum(g[i, j, c] for i, j in IJ if (i, j, c) in g) == V[c])

    # 3. Each clue is in its own garden
    for c in C:
        m.Add(g[L[c][0], L[c][1], c] == 1)

    # 4. Antisymmetric flow, defined once per undirected edge
    for i, j, p, q in E:
        if (i, j) < (p, q):
            m.Add(f[i, j, p, q] == -f[p, q, i, j])

    # 5. Exactly one wall root
    m.AddExactlyOne([y[i, j] for i, j in IJ])

    # 6. The wall root is a wall:  y*(1-x) = 0  ->  y <= x
    for i, j in IJ:
        m.Add(y[i, j] <= x[i, j])

    # 6b. Pick the wall root ahead of time so each grid only has one valid root
    # Any wall cell can be the root, so if we know a cell is a wall then use it
    # Otherwise the root is the first wall cell in reading order: if (p,q) is a wall
    # then the root must be at or before it
    if forced_walls:
        m.Add(y[forced_walls[0]] == 1)
    else:
        for p, q in IJ:
            m.Add(x[p, q] <= sum(y[i, j] for i, j in IJ if (i, j) <= (p, q)))

    # 7a. Clue cells have no outflow
    for i, j, p, q in E:
        if (i, j) in clue_cells:
            m.Add(f[i, j, p, q] <= 0)

    # 7b. Wall root has no outflow:  f*y <= 0  ->  f <= M(1-y)
    for i, j, p, q in E:
        m.Add(f[i, j, p, q] <= M * (1 - y[i, j]))

    # 8. Inflow to each clue equals its value minus 1
    for c in C:
        m.Add(sum(f[p, q, L[c][0], L[c][1]] for p, q in neighbours(*L[c])) == V[c] - 1)

    # 9. Non-clue cells have net outflow +1; the wall root absorbs the
    #    other num_walls-1 units, so its net outflow is -(num_walls-1).
    for i, j in IJ:
        if (i, j) not in clue_cells:
            m.Add(sum(f[i, j, p, q] for p, q in neighbours(i, j)) == 1 - num_walls * y[i, j])

    # 10. No flow between a wall and a garden cell:
    #     (x_ij - x_pq)*f = 0  ->  |f| <= M(1 - |x_ij - x_pq|)
    #     Antisymmetry covers the reverse edge, so both signs of the difference are enough
    for i, j, p, q in E:
        m.Add(f[i, j, p, q] <= M * (1 - x[i, j] + x[p, q]))  # 10a
        m.Add(f[i, j, p, q] <= M * (1 - x[p, q] + x[i, j]))  # 10b

    # 11. No 2x2 block of walls
    for i, j in IJ:
        if i > 1 and j > 1:
            m.Add(x[i, j] + x[i - 1, j] + x[i, j - 1] + x[i - 1, j - 1] <= 3)

    # 12. Different gardens can't touch orthogonally
    for i, j, p, q in E:
        for c in C:
            for d in C:
                if c < d and (i, j, c) in g and (p, q, d) in g:
                    m.Add(g[i, j, c] + g[p, q, d] <= 1)

    return m, x


def print_solution(solver, x, raw_puzzle):
    output_arr = raw_puzzle.astype(str)
    for idx, v in np.ndenumerate(output_arr):
        if v == "nan":
            output_arr[idx] = "#" if solver.Value(x[idx[0] + 1, idx[1] + 1]) else "."
        else:
            output_arr[idx] = str(int(raw_puzzle[idx]))

    # Pad every cell to the width of the longest entry so columns line up
    width = np.char.str_len(output_arr).max()
    output_arr = np.char.rjust(output_arr, width)
    print("\n".join(" ".join(row) for row in output_arr))


if __name__ == "__main__":
    raw_puzzle = np.genfromtxt("Nurikabe/nurikabe_puzzle2.csv", delimiter=",")

    clue_df = pd.DataFrame(columns=["coords", "values"])
    num_rows, num_cols = raw_puzzle.shape
    for idx, v in np.ndenumerate(raw_puzzle):
        if not np.isnan(v):
            clue_df.loc[len(clue_df)] = [(idx[0] + 1, idx[1] + 1), int(v)]

    model, x = build_model(num_rows, num_cols, clue_df)
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = 1
    solver.parameters.log_search_progress = True
    status = solver.Solve(model)
    print(solver.StatusName(status))
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print_solution(solver, x, raw_puzzle)
