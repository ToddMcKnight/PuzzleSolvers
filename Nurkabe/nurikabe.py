"""
Nurikabe as a MILP in Pyomo, following the flow-based formulation.

Connectivity is enforced by choosing a root for each region including the wall
The root has no outflow and must have inflow equal to the size of the region minus 1 (s
ize is known and determined during presolve)
All other regions have net +1 outflow and may only flow to cells of the same region
This enforces that regions are connected and the correct size


     +--------+   1    +--------+
     |  root  | <----- |   A    |   A: 0 in, 1 out  -> net +1
     +--------+        +--------+
         ^
         | 2
     +--------+
     |   B    |                     B: 1 in, 2 out  -> net +1
     +--------+
         ^
         | 1
     +--------+
     |   C    |                     C: 0 in, 1 out  -> net +1
     +--------+

     root: 0 out, 1 + 2 = 3 in  ->  net -3 = -(4 - 1)



Cells are 1-indexed (row i, column j). Clues are given as a list of
((row, col), value) pairs.
"""
import pyomo.environ as pyo
import pandas as pd
import numpy as np

def build_model(num_rows, num_cols, clues_df):
    m = pyo.ConcreteModel()

    # ---------- Index sets ----------
    m.C = pyo.RangeSet(0,len(clues_df)-1) # {0,1,2,3, len-1} matches the index in the df
    m.I = pyo.RangeSet(1, num_rows)
    m.J = pyo.RangeSet(1, num_cols)
    m.IJ = m.I * m.J

    # O_(i,j): orthogonal neighbours that exist on the grid
    def neighbours(i, j):
        cand = [(i, j + 1), (i, j - 1), (i + 1, j), (i - 1, j)]
        return [(p, q) for p, q in cand if 1 <= p <= num_rows and 1 <= q <= num_cols]

    # Directed edges (i,j) -> (p,q), used to index the flow variables
    m.E = pyo.Set(dimen=4, initialize=[(i, j, p, q)
                                       for i in m.I for j in m.J
                                       for p, q in neighbours(i, j)])

    # ---------- Parameters ----------
    L = clues_df["coords"].to_dict()   # clue coordinates
    V = clues_df["values"].to_dict()   # clue values
    #clue_cells is used to check if (i,j) is a clue while L is used to say where is clue c
    clue_cells = set(L.values())
    num_walls = num_rows * num_cols - sum(V.values())
    # Big-M: no flow on an edge can exceed the size of the largest region
    M = max([num_walls] + list(V.values()))

    # ---------- Variables ----------
    m.x = pyo.Var(m.IJ, domain=pyo.Binary)                 # 1 if wall
    m.g = pyo.Var(m.IJ, m.C, domain=pyo.Binary)            # 1 if cell in garden c
    m.y = pyo.Var(m.IJ, domain=pyo.Binary)                 # 1 if wall root
    m.f = pyo.Var(m.E, domain=pyo.Integers, bounds=(-M, M))  # flow (i,j)->(p,q)

    # Pure feasibility problem
    m.obj = pyo.Objective(expr=0)

    # 1. Each cell is in exactly one garden or is a wall
    # For each cell sum over the tags for inclusion in each clue and wall and set it to 1
    m.c1 = pyo.Constraint(m.IJ, rule=lambda m, i, j:
                          sum(m.g[i, j, c] for c in m.C) + m.x[i, j] == 1)

    # 2. Each garden has the right number of cells
    # For each clue sum over the tags for cells being that clue and set equal to the value
    m.c2 = pyo.Constraint(m.C, rule=lambda m, c:
                          sum(m.g[i, j, c] for i, j in m.IJ) == V[c])

    # 3. Each clue is in its own garden
    # For each clue look up where it is and set the inclusion tag for that cell and clue to be 1
    m.c3 = pyo.Constraint(m.C, rule=lambda m, c:
                          m.g[L[c][0], L[c][1], c] == 1)

    # 4. Antisymmetric flow (only one direction per edge, to avoid duplicates)
    # Flow in one direction is negative the other direction
    # From from (i,j)-> (p,q) = -1 * flow from (p,q)->(i,j)
    # Only defined for edges going top to bottom or left to right to prevent double defining
    m.c4 = pyo.Constraint(
        [e for e in m.E if (e[0], e[1]) < (e[2], e[3])],
        rule=lambda m, i, j, p, q: m.f[i, j, p, q] == -m.f[p, q, i, j])

    # 5. Exactly one wall root
    # Sum the root tag over all cells and set to 1
    m.c5 = pyo.Constraint(expr=sum(m.y[i, j] for i, j in m.IJ) == 1)

    # 6. The wall root is a wall:  y*(1-x) = 0  ->  y <= x
    # y=1 implies that x=1, otherwise x is free
    m.c6 = pyo.Constraint(m.IJ, rule=lambda m, i, j: m.y[i, j] <= m.x[i, j])

    # 7a. Clue cells have no outflow
    # flow (i,j) -> (p,q) <=0 means that either there is no flow or the direction is from (p,q) to (i,j)
    m.c7a = pyo.Constraint(
        [e for e in m.E if (e[0], e[1]) in clue_cells],
        rule=lambda m, i, j, p, q: m.f[i, j, p, q] <= 0)

    # 7b. Wall root has no outflow:  f*y <= 0  ->  f <= M(1-y)
    # If y is 0 then RHS equal to M and LHS is effectively free, otherwise LHS is 0 or neg same as 7a
    m.c7b = pyo.Constraint(m.E, rule=lambda m, i, j, p, q:
                           m.f[i, j, p, q] <= M * (1 - m.y[i, j]))

    # 8. Inflow to each clue equals its value minus 1
    m.c8 = pyo.Constraint(m.C, rule=lambda m, c:
                          sum(m.f[p, q, L[c][0], L[c][1]]
                              for p, q in neighbours(*L[c])) == V[c] - 1)

    # 9. Non-clue cells have net outflow +1; the wall root absorbs the
    #    other num_walls-1 units, so its net outflow is -(num_walls-1).
    #    1 - y*num_walls gives +1 when y=0 and 1-num_walls when y=1.
    #    If the y is 0 then the RHS is 1 other wise is -1* (num_walls-1)
    m.c9 = pyo.Constraint(
        [(i, j) for i, j in m.IJ if (i, j) not in clue_cells],
        rule=lambda m, i, j:
            sum(m.f[i, j, p, q] for p, q in neighbours(i, j))
            == 1 - num_walls * m.y[i, j])

    # 10. No flow between a wall and a garden cell:
    #     (x_ij - x_pq)*f = 0  ->  |f| <= M(1 - |x_ij - x_pq|)
    #     Antisymmetry covers the reverse edge, so -f/+f with both signs of the
    #     difference is enough.\
    #      Case 1: Both walls or Both non-walls, then RHS both goes to M and flow is unconstrainted
    #       Case 2: (i,j) wall and (p,q) non-wall then 10a RHS is 2M but 10b RHS is 0
    #       Case 3: (i,j) non-wall and (p,q) non-well then 10a rhs is 0 and 10b Rhs is 2M
    #       Note that we don't constrain that two different gardens can't flow into each but constraint 12 prevents them from being orthogonal which is stronger
    m.c10a = pyo.Constraint(m.E, rule=lambda m, i, j, p, q:
                            m.f[i, j, p, q] <= M * (1 - m.x[i, j] + m.x[p, q]))
    m.c10b = pyo.Constraint(m.E, rule=lambda m, i, j, p, q:
                            m.f[i, j, p, q] <= M * (1 - m.x[p, q] + m.x[i, j]))

    # 11. No 2x2 block of walls

    m.c11 = pyo.Constraint(
        [(i, j) for i, j in m.IJ if i > 1 and j > 1],
        rule=lambda m, i, j:
            m.x[i, j] + m.x[i - 1, j] + m.x[i, j - 1] + m.x[i - 1, j - 1] <= 3)

    # 12. Different gardens can't touch orthogonally
    # Edges are already calculated so we rely on them. Then for every pair of clues we create the constraint
    # c<d to prevent redundant constraints but we need to keep every edge
    m.c12 = pyo.Constraint(
        [(i, j, p, q, c, d) for i, j, p, q in m.E
         for c in m.C for d in m.C if c < d],
        rule=lambda m, i, j, p, q, c, d: m.g[i, j, c] + m.g[p, q, d] <= 1)

    return m


def print_solution(m, raw_puzzle):
    output_arr = raw_puzzle.astype(str)
    for idx, v in np.ndenumerate(output_arr):
        if v == "nan":
            output_arr[idx] = "#" if pyo.value(m.x[idx[0] + 1, idx[1] + 1]) >= 0.5 else "."
        else:
            output_arr[idx] = str(int(raw_puzzle[idx]))

    # Pad every cell to the width of the longest entry so columns line up
    width = np.char.str_len(output_arr).max()
    output_arr = np.char.rjust(output_arr, width)
    print("\n".join(" ".join(row) for row in output_arr))




if __name__ == "__main__":
    raw_puzzle = np.genfromtxt("Nurkabe/nurikabe_puzzle2.csv", delimiter=",")
    print(raw_puzzle)

    
    clue_df = pd.DataFrame(columns = ["coords","values"])
    num_rows,num_cols = raw_puzzle.shape
    for idx, v in np.ndenumerate(raw_puzzle):
        if not np.isnan(v):
            clue_df.loc[len(clue_df)] = [(idx[0]+1, idx[1]+1), int(v)]
            
    print(clue_df)

    
    model = build_model(num_rows, num_cols, clue_df)
    solver = pyo.SolverFactory("appsi_highs")  # or "gurobi", "cbc", "glpk"
    result = solver.solve(model, tee=True)
    print(result.solver.termination_condition)
    print_solution(model, raw_puzzle)
    