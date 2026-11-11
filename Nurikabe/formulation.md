# Nurikabe MILP Formulation

This is the mathematical formulation implemented in [`nurikabe.py`](nurikabe_solvers/nurikabe.py).
Constraint numbers match the `m.c1` to `m.c12` names in the code. Rule (13) has no
constraint of its own, because it decides which $g$ variables exist. A *garden* is
an island of unshaded cells, and each garden contains exactly one clue.

## Connectivity approach

Connectivity is enforced with flow. Each region has one root, which acts as its
sink:

- **Gardens:** the root is the clue cell.
- **Wall:** the model chooses the root with the variable $y$.

Every non-root cell produces one unit of flow, which can only travel through
cells in the same region. A root must absorb one unit from every other cell in
its region, so a region of size $n$ has net outflow $-(n - 1)$ at its root.

```text
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
```

## Sets

| Symbol | Definition |
|---|---|
| $I = \{1, \dots, R\}$ | Rows |
| $J = \{1, \dots, K\}$ | Columns |
| $C = \{0, \dots, \lvert C \rvert - 1\}$ | Clues |
| $O_{ij}$ | Orthogonal neighbours of cell $(i, j)$ that are on the grid |
| $E = \{(i, j, p, q) : (i, j) \in I \times J,\ (p, q) \in O_{ij}\}$ | Directed edges between neighbouring cells |

## Parameters

| Symbol | Definition |
|---|---|
| $\ell_c = (\ell_c^1, \ell_c^2)$ | Row and column of clue $c$ |
| $L = \{\ell_c : c \in C\}$ | Set of clue cells |
| $v_c$ | Value of clue $c$, which is the size of its garden |
| $W = RK - \sum_{c \in C} v_c$ | Number of wall cells, known from the clues |
| $M = \max\left(W, \max_{c \in C} v_c\right)$ | Big-M: the size of the largest region, which bounds the flow on any edge |

## Variables

| Variable | Domain | Meaning |
|---|---|---|
| $x_{ij}$ | $\{0, 1\}$ | 1 if cell $(i, j)$ is a wall |
| $g_{ijc}$ | $\{0, 1\}$ | 1 if cell $(i, j)$ is in the garden of clue $c$ |
| $y_{ij}$ | $\{0, 1\}$ | 1 if cell $(i, j)$ is the wall root |
| $f_{ijpq}$ | $\mathbb{Z} \cap [-M, M]$ | Flow from $(i, j)$ to $(p, q)$; negative values mean flow in the reverse direction |

## Objective

This is a pure feasibility problem, so the objective is constant:

$$
\min\ 0
$$

## Constraints

### Region assignment

**(1) Each cell is a wall or in exactly one garden.**

$$
\sum_{c \in C} g_{ijc} + x_{ij} = 1 \qquad \forall (i, j) \in I \times J
$$

**(2) Each garden has the size given by its clue.**

$$
\sum_{(i, j) \in I \times J} g_{ijc} = v_c \qquad \forall c \in C
$$

**(3) Each clue is in its own garden.**

$$
g_{\ell_c^1 \ell_c^2 c} = 1 \qquad \forall c \in C
$$

### Flow

**(4) Flow is antisymmetric.** Each undirected edge needs only one constraint,
so this is applied where $(i, j)$ comes before $(p, q)$ in lexicographic order.

$$
f_{ijpq} = -f_{pqij} \qquad \forall (i, j, p, q) \in E : (i, j) < (p, q)
$$

**(5) There is exactly one wall root.**

$$
\sum_{(i, j) \in I \times J} y_{ij} = 1
$$

**(6) The wall root is a wall.** The condition $y_{ij}(1 - x_{ij}) = 0$
linearizes to:

$$
y_{ij} \le x_{ij} \qquad \forall (i, j) \in I \times J
$$

**(6b) The wall root is chosen ahead of time.** Every wall cell could otherwise be
the root, so this removes duplicate solutions without removing any grid.

A non-clue cell must be a wall if every clue either cannot reach it under (13) or
is not the only clue it touches, because (12) stops gardens from touching. If any
such cell exists, the model takes the first one in reading order, $(a, b)$, as
the root:

$$
y_{ab} = 1
$$

Otherwise the root is the first wall cell in reading order: if a cell is a wall,
the root must be at or before it, where $(i, j) \le (p, q)$ is reading order.

$$
x_{pq} \le \sum_{(i, j) \le (p, q)} y_{ij} \qquad \forall (p, q) \in I \times J
$$

**(7a) Clue cells have no outflow.**

$$
f_{ijpq} \le 0 \qquad \forall (i, j, p, q) \in E : (i, j) \in L
$$

**(7b) The wall root has no outflow.** The condition $f_{ijpq} \, y_{ij} \le 0$
linearizes to:

$$
f_{ijpq} \le M (1 - y_{ij}) \qquad \forall (i, j, p, q) \in E
$$

**(8) Inflow to each clue equals its value minus 1.**

$$
\sum_{(p, q) \in O_{\ell_c}} f_{p q \ell_c^1 \ell_c^2} = v_c - 1 \qquad \forall c \in C
$$

**(9) Non-clue cells have net outflow +1, except the wall root.** The right-hand
side is $+1$ when $y_{ij} = 0$ and $-(W - 1)$ when $y_{ij} = 1$.

$$
\sum_{(p, q) \in O_{ij}} f_{ijpq} = 1 - W y_{ij} \qquad \forall (i, j) \in (I \times J) \setminus L
$$

**(10) No flow between a wall and a garden cell.** The condition
$(x_{ij} - x_{pq}) f_{ijpq} = 0$ linearizes to:

$$
f_{ijpq} \le M (1 - x_{ij} + x_{pq}) \qquad \forall (i, j, p, q) \in E \tag{10a}
$$

$$
f_{ijpq} \le M (1 - x_{pq} + x_{ij}) \qquad \forall (i, j, p, q) \in E \tag{10b}
$$

| $x_{ij}$ | $x_{pq}$ | (10a) right-hand side | (10b) right-hand side | Result |
|---|---|---|---|---|
| Equal | Equal | $M$ | $M$ | Flow is limited only by its bounds |
| 1 | 0 | $0$ | $2M$ | $f_{ijpq} \le 0$ |
| 0 | 1 | $2M$ | $0$ | $f_{ijpq} \le 0$ |

These are upper bounds only, but they apply to both directions of every edge. By
antisymmetry, $f_{pqij} \le 0$ also gives $f_{ijpq} \ge 0$, so the flow on a
wall-garden edge is exactly 0.

### Puzzle rules

**(11) No 2×2 block of walls.**

$$
x_{ij} + x_{i-1,j} + x_{i,j-1} + x_{i-1,j-1} \le 3 \qquad \forall (i, j) \in I \times J : i > 1,\ j > 1
$$

**(12) Different gardens cannot touch orthogonally.**

$$
g_{ijc} + g_{pqd} \le 1 \qquad \forall (i, j, p, q) \in E,\ \forall c, d \in C : c < d
$$

Requiring $c < d$ removes duplicate clue pairs. Every directed edge is kept, so
both $(g_{ijc}, g_{pqd})$ and $(g_{pqc}, g_{ijd})$ are covered.

This constraint also removes the need for a separate rule against flow between
two gardens, because cells from different gardens are never neighbours.

**(13) A garden cannot reach cells too far from its clue.** A connected garden
of size $v_c$ has every cell within $v_c - 1$ steps of its clue, so the model only
creates $g_{ijc}$ when:

$$
\lvert i - \ell_c^1 \rvert + \lvert j - \ell_c^2 \rvert \le v_c - 1
$$

Every other $g_{ijc}$ is treated as 0, so the sums in (1) and (2) skip it, and (12)
is only created when both of its variables exist. This does not change which grids
are valid. It only removes variables the solver would otherwise have to rule out.

## Why this enforces connectivity

Take any connected group $S$ of cells that belong to the same region, with no
same-region neighbours outside $S$.

1. No flow crosses the boundary of $S$. Constraint (10) blocks wall-garden edges,
   and constraint (12) means no edge joins two different gardens.
2. By antisymmetry (4), flow on edges inside $S$ cancels, so the net outflows of
   the cells in $S$ must sum to 0.
3. Every non-root cell has net outflow $+1$, so $S$ must contain a root. Otherwise
   the sum would be $\lvert S \rvert > 0$.
4. Each region has exactly one root, so it can contain only one such group.
   Every garden and the wall are therefore connected.

## Assumption

The model needs at least one wall cell ($W \ge 1$). Constraints (5) and (6)
require a wall root, so a puzzle whose clues cover the whole grid would be
reported as infeasible.
