This folder contains MILP formulations of various puzzle games
 ## 1. Nurikabe
 The goal of Nurikabe is to divide the grid into several orthogonally connected islands by choosing cells to form a single orthogonoally connected wall
Rules of Nurikabe
> 1. Some cells have numbers. Each numbered cell is an island and the value of the cell tells you how many orthogonally connected cells form that island. Every island must have exactly 1 clue and every clue must have an island
> 2. No islands can be touch each other orthogonally, diagonal touching is allowed
> 3. All wall tiles must form a single orthogonally connected region with no breaks
> 4. No 2x2 region may be entirely wall

The formulation of nurikabe relies on flow constraints to ensure connectivity. Each clue serves as a root and every other cell must contribute +1 to the total flow so the only way for the root cells to have the correct flow is for every single cell to be somewhere connected.

To run the solver, conver the puzzle in a .CSV file where blank cells become empty strings and save it into the nurikabe folder. Then go into nurikabe.py and change the path in the first line of the main method to the puzzle. 



## 2. Scramble Squares
This is a puzzle that I have a lot of nostalgia for from growing up. The premise
is that you have 9 square tiles and each edge has either the front half or the
back half of one of four objects. (The one I had growing up and in the
example in the folder is 4 different whales folder was four different whales).
The goal is to arrange the 9 squares in a 3x3 grid so that the edges line up. 

To run open the scramble squares input excel sheet. Arrange your puzzle in a
random 3x3 grid. Decide on a number for each object and sign for each half. (Eg
the red bird is 1, the blue bird is 2, and yellow bird is 3 and the multicolor
bird is 4. Heads are positive and tails and negative). Then transcribe the
puzzle into the excel sheet at the top. Running scramble_square.py output 

> NOTE: The bird example puzzle is infeasable and is meant to show how to input
> data. This is a known issue and I'm working on finding a better example to use
> better example. I'm also working on making the output more readable