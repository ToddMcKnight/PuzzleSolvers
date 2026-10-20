# Puzzle Solvers with Mixed-Integer Linear Programming

This repository contains mixed-integer linear programming (MILP) formulations of
logic puzzles, built in Python with Pyomo and solved with HiGHS.

| Puzzle | Key modeling technique |
|---|---|
| [Nurikabe](#1-nurikabe) | Network flow constraints to enforce connectivity |
| [Scramble Squares](#2-scramble-squares) | Assignment model with pairwise conflict constraints |

## Requirements

```
pip install pyomo highspy openpyxl
```

---

## 1. Nurikabe

Nurikabe is a grid puzzle in which some cells contain numbers. The goal is to
shade cells as "wall" so that the unshaded cells form islands that satisfy the
clues.

### Rules

1. Each numbered cell belongs to exactly one island, and its number equals the
   number of orthogonally connected cells in that island. Every island contains
   exactly one clue.
2. Islands may not touch each other orthogonally (diagonal contact is allowed).
3. All wall cells must form a single orthogonally connected region.
4. No 2×2 block may consist entirely of wall cells.

### Formulation

Connectivity is the difficult part to express linearly. The model uses flow
constraints: each clue cell acts as a root, and every other cell in its region
must send one unit of flow toward that root through adjacent cells. The root can
only receive the required amount of flow if every cell is connected to it, so
any disconnected cell makes the solution infeasible.

### Usage

1. Save the puzzle as a `.csv` file in the `Nurikabe` folder, with clue cells
   containing their numbers and blank cells left empty.
2. In `nurikabe.py`, set the file path on the first line of the `main` function.
3. Run `python nurikabe.py`.

---

## 2. Scramble Squares

Scramble Squares is a tile-matching puzzle with 9 square pieces. Each edge of a
piece shows either the front half or the back half of one of four objects. The
goal is to arrange the pieces in a 3×3 grid, rotating them as needed, so that
every pair of adjacent edges forms a complete object.

The puzzle has 9! × 4⁹ ≈ 95 billion possible arrangements, which makes it a
natural fit for integer programming rather than trial and error. (I grew up with
the whale version of this puzzle, pictured in `Whales ex.jpg`.)

### Formulation

- **Decision variables:** binary `x[p, r, c, o]` equals 1 if piece `p` is placed
  at row `r`, column `c`, in orientation `o` (one of four 90° rotations).
- **Assignment constraints:** every piece is placed exactly once, and every cell
  holds exactly one piece.
- **Edge matching:** each edge is encoded as an integer, where the absolute value
  identifies the object and the sign identifies the half. Two adjacent edges
  match only if their values sum to zero. For every pair of piece placements
  whose shared edge would not match, the constraint
  `x[p1, ...] + x[p2, ...] ≤ 1` forbids choosing both.
- **Symmetry breaking:** any solution can be rotated as a whole, so piece 1 is
  fixed to orientation 0 to remove the three rotated copies of each solution.

### Usage

1. Open `scramble squares input.xlsx`.
2. Lay your pieces out in any 3×3 arrangement.
3. Assign a number to each object and a sign to each half. For example:

   | Number | Object |
   |---|---|
   | 1 | Red bird |
   | 2 | Blue bird |
   | 3 | Yellow bird |
   | 4 | Multicolor bird |

   Heads are positive and tails are negative.
4. Enter each piece's four edge values around its `Square N` label: top above
   the label, right to the right, bottom below, and left to the left.
5. Run `python scramble_square.py`. The solver prints the position and
   orientation of each piece in the solution, or reports that no solution
   exists.

### Validation

The model was tested against a puzzle transcribed in its already-solved state,
which must be feasible. This test exposed a bug in Pyomo's older `appsi_highs`
interface, which reported a solvable instance as infeasible. The solver now uses
the current `highs` interface.

### Known issues

- The included bird example is infeasible as transcribed. It demonstrates the
  input format, but at least two edges were likely misread from the source
  image. A verified example will replace it.
- The solution output is a plain-text list. A grid-style display is planned.