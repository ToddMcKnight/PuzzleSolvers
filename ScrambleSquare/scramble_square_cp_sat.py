#This is a Google OR-Tools CP-SAT model for the Scramble Square puzzle, translated
#from the Pyomo model in scramble_square.py.

#The puzzle consists of a 3x3 grid of square pieces, each with either front half
#or back half of a picture on each edge. The goal is to arrange the pieces in a
#grid such that all of the edges line up with the other half. Whales ex.jpg
#contains a picture of an example with whales

import re

from openpyxl import load_workbook
from ortools.sat.python import cp_model


#Each piece is stored as a list of 4 integers representing the edges in order
# The abs value of the number represents which object it is and the sign
# represents whether it's the top or the bottom
# Every edge is compared to every other edge in every position and if they don't
# match a constraint is added to the model to block that pairing
# Note that due to the rotational nature of the puzzle, [3,-1,-1,-2] is the same as [-1,-2,3,-1] and [-2,3,-1,-1] and [-1,-1,-2,3]


def rotate(sides, o):
    return [sides[(i - o) % 4] for i in range(4)]


def load_pieces(path='ScrambleSquare\\scramble squares input.xlsx', sheet=None):
    """Read scramble-square pieces from an Excel layout.

    Each piece is a 'Square N' label cell with its side values
    directly above, right of, below, and left of it.
    Returns a list of [top, right, bottom, left], ordered by N.
    """
    wb = load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active

    found = {}
    for row in ws.iter_rows():
        for cell in row:
            if not isinstance(cell.value, str):
                continue
            match = re.fullmatch(r"\s*Square\s*(\d+)\s*", cell.value)
            if not match:
                continue

            n = int(match.group(1))
            r, c = cell.row, cell.column
            neighbors = {
                "top": (r - 1, c),
                "right": (r, c + 1),
                "bottom": (r + 1, c),
                "left": (r, c - 1),
            }

            sides = []
            for name, (rr, cc) in neighbors.items():
                v = ws.cell(row=rr, column=cc).value
                if v is None or str(v).strip() == "":
                    raise ValueError(f"Square {n} is missing its {name} value "
                                     f"(cell {ws.cell(row=rr, column=cc).coordinate})")
                sides.append(int(v))
            found[n] = sides

    if not found:
        raise ValueError("No 'Square N' cells found")
    return [found[n] for n in sorted(found)]


def build_model(pieces):
    """
    Build a CP-SAT model for the Scramble Square puzzle.

    Args:
        pieces (list): A list of pieces, where each piece is represented as a list of 4 integers

    Returns the model and x, where x[i, r, c, o] is 1 if piece i is in cell (r,c) with orientation o
    """
    m = cp_model.CpModel()
    P = range(9)
    R = range(3)
    C = range(3)
    O = range(4)

    x = {(i, r, c, o): m.NewBoolVar(f"x_{i}_{r}_{c}_{o}")
         for i in P for r in R for c in C for o in O}

    # Each piece placed exactly once
    for i in P:
        m.AddExactlyOne(x[i, r, c, o] for r in R for c in C for o in O)

    # Each cell gets exactly one piece
    for r in R:
        for c in C:
            m.AddExactlyOne(x[i, r, c, o] for i in P for o in O)

    # Symmetry breaking: piece 0 stays in orientation 0 Not strictly necessary but
    # prevents the solver from exploring symmetric solutions
    for r in R:
        for c in C:
            for o in (1, 2, 3):
                m.Add(x[0, r, c, o] == 0)

    # Horizontal: right of (r,c) must cancel left of (r,c+1)
    for r in range(3):
        for c in range(2):
            for i in P:
                for o in O:
                    right = rotate(pieces[i], o)[1]
                    for i2 in P:
                        if i2 == i:
                            continue
                        for o2 in O:
                            left = rotate(pieces[i2], o2)[3]
                            if right + left != 0:
                                m.AddBoolOr(x[i, r, c, o].Not(), x[i2, r, c + 1, o2].Not())

    # Vertical: bottom of (r,c) must cancel top of (r+1,c)
    for r in range(2):
        for c in range(3):
            for i in P:
                for o in O:
                    bottom = rotate(pieces[i], o)[2]
                    for i2 in P:
                        if i2 == i:
                            continue
                        for o2 in O:
                            top = rotate(pieces[i2], o2)[0]
                            if bottom + top != 0:
                                m.AddBoolOr(x[i, r, c, o].Not(), x[i2, r + 1, c, o2].Not())
    return m, x


if __name__ == "__main__":
    pieces = load_pieces()
    print(pieces)

    model, x = build_model(pieces)
    solver = cp_model.CpSolver()
    solver.parameters.log_search_progress = True
    status = solver.Solve(model)
    print(solver.StatusName(status))

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (i, r, c, o), var in sorted(x.items(), key=lambda kv: (kv[0][1], kv[0][2])):
            if solver.Value(var):
                print(f"Cell ({r},{c}): piece {i}, "
                      f"orientation {o}, sides {rotate(pieces[i], o)}")
