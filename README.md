This folder contains MILP formulations of various puzzle games
 1. Nurikabe
 The goal of Nurikabe is to divide the grid into several orthogonally connected islands by choosing cells to form a single orthogonoally connected wall
Rules of Nurikabe
> 1. Some cells have numbers. Each numbered cell is an island and the value of the cell tells you how many orthogonally connected cells form that island. Every island must have exactly 1 clue and every clue must have an island
> 2. No islands can be touch each other orthogonally, diagonal touching is allowed
> 3. All wall tiles must form a single orthogonally connected region with no breaks
> 4. No 2x2 region may be entirely wall

The formulation of nurikabe relies on flow constraints to ensure connectivity. Each clue serves as a rook and every other cell must contribute +1 to the total flow so the only way for the root cells to have the correct flow is for every single cell to be somewhere connected.

To run the solver, conver the puzzle in a .CSV file where blank cells become empty strings and save it into the nurikabe folder. Then go into nurikabe.py and change the path in the first line of the main method to the puzzle. 
