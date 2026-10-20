#This is a Pyomo model for the Scramble Square puzzle. 

#The puzzle consists of a 3x3 grid of square pieces, each with either front half
#or back half of a picture on each edge. The goal is to arrange the pieces in a
#grid such that all of the edges line up with the other half. Whales ex.jpg
#contains a picture of an example with whalees



import pyomo.environ as pyo



#Each piece is stored as a list of 4 integers representing the edges in order
# The abs value of the number represents which object it is and the sign
# represents where it's the top of the bottom
# During presolve every since edge is compared to every single other edge in
# every single positive and if they don't match a constraint is added to the
# model to block that pairing
# Note that due to the rotational nature of the puzzle, [3,-1,-1,-2] is the same as [-1,-2,3,-1] and [-2,3,-1,-1] and [-1,-1,-2,3]
pieces = [
    [3, -4, -1, -2],
    [4, 1, 2, 3],
    [-3, 3, 1, -4],
    [1, -2, 3, -4],
    [-3, 2, 1, 4],
    [-1, -4, 3, -2],
    [-3, 2, -4, -1],
    [-4, -2, 3, 2],
    [-2, 4, -1, 1],
]

def rotate(sides, o):
    return [sides[(i - o) % 4] for i in range(4)]

m = pyo.ConcreteModel()
m.P = pyo.RangeSet(0, 8)
m.R = pyo.RangeSet(0, 2)
m.C = pyo.RangeSet(0, 2)
m.O = pyo.RangeSet(0, 3)

m.x = pyo.Var(m.P, m.R, m.C, m.O, domain=pyo.Binary)
m.obj = pyo.Objective(expr=0)  # feasibility problem

# Each piece placed exactly once
m.one_place = pyo.Constraint(
    m.P,
    rule=lambda m, i: sum(m.x[i, r, c, o]
                          for r in m.R for c in m.C for o in m.O) == 1,
)

# Each cell gets exactly one piece
m.one_piece = pyo.Constraint(
    m.R, m.C,
    rule=lambda m, r, c: sum(m.x[i, r, c, o]
                             for i in m.P for o in m.O) == 1,
)

# Symmetry breaking: piece 0 stays in orientation 0 Not strictly necessary but
# prevents the solver from exploring symmetric solutions
for r in m.R:
    for c in m.C:
        for o in (1, 2, 3):
            m.x[0, r, c, o].fix(0)

m.conflicts = pyo.ConstraintList()

# Horizontal: right of (r,c) must cancel left of (r,c+1)
for r in range(3):
    for c in range(2):
        for i in range(9):
            for o in range(4):
                right = rotate(pieces[i], o)[1]
                for i2 in range(9):
                    if i2 == i:
                        continue
                    for o2 in range(4):
                        left = rotate(pieces[i2], o2)[3]
                        if right + left != 0:
                            m.conflicts.add(
                                m.x[i, r, c, o] + m.x[i2, r, c + 1, o2] <= 1)

# Vertical: bottom of (r,c) must cancel top of (r+1,c)
for r in range(2):
    for c in range(3):
        for i in range(9):
            for o in range(4):
                bottom = rotate(pieces[i], o)[2]
                for i2 in range(9):
                    if i2 == i:
                        continue
                    for o2 in range(4):
                        top = rotate(pieces[i2], o2)[0]
                        if bottom + top != 0:
                            m.conflicts.add(
                                m.x[i, r, c, o] + m.x[i2, r + 1, c, o2] <= 1)

solver = pyo.SolverFactory("appsi_highs")
result = solver.solve(m, tee=True)
print(result.solver.termination_condition)

for r in m.R:
    for c in m.C:
        for i in m.P:
            for o in m.O:
                if pyo.value(m.x[i, r, c, o]) > 0.5:
                    print(f"Cell ({r},{c}): piece {i}, "
                          f"orientation {o}, sides {rotate(pieces[i], o)}")