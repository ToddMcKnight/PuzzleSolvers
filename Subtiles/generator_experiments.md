# 13x13 generator experiments

Goal: `python Subtiles/subtiles_generator.py --size 13 --n 16` finishes (unique, minimized puzzle) in under 20 minutes.
Stop by 02:30 (no new run starts after 02:10, so nothing is still running at 02:30).
Each round: run with a hard 20-minute limit and a 4 GB memory watcher, review the log, make one change, run again.

| Round | Change tried | Result |
|---|---|---|
| 0 | Random starting clues; a solve over 60 s adds one clue on the least-revealed shape | Stopped by hand after ~6 min: 5 repair rounds, every solve hit 60 s, clues landed on small shapes |
| 1 | When a solve hits 60 s, find the level it was stuck building; add clues there and on the level below (2, then 3, 4... if the same level stays stuck), each at the cell farthest from that shape's clues | **Over 20 min (stopped).** Attempt 1 wasted ~4.5 min (a 17-shape fit in the empty cells, so the grid was dropped). Attempt 2: unique at 49 clues after ~1.5 min of repair (targeting worked). Minimizing reached 37 clues in ~3 min, then every check hit the 30 s limit (19 in a row) until the 20-min stop. A unique 37-clue puzzle was saved. |
| 2 | Stop minimizing after 3 checks in a row time out | **Done in 15 min 39 s.** 35 clues, unique, solver takes 15.7 s on it (saved as `gen_13x13_N16_seed1_round2.txt`). Attempts 1 and 2 were thrown away (a 17-shape fit in the empty cells), ~8 min wasted. Attempt 3: unique at 66 clues, minimized to 35 in 4 min 19 s, stopped after 3 timeouts in a row. |
| 3 | No change: same settings with seed 2, to check round 2 wasn't a lucky grid | **Done in 10 min 54 s.** 38 clues, unique, solver takes 28.2 s on it (`gen_13x13_N16_seed2.txt`). First grid worked (no wasted attempts): unique at 67 clues after 24 repair rounds, minimized to 38 in 5 min 1 s, stopped after 3 timeouts in a row with 24 clues never tried. |

## Where it stands

Two seeds in a row finished under 20 minutes (15:39 and 10:54), so I stopped as planned.

## Next ideas, biggest payoff first

1. **Drop bad grids before solving.** Right after packing, check whether the N-shape plus one cell fits entirely in the empty cells. If it does, the answer would be N+1 and the grid is useless. In round 2 this cost ~8 of the 15.5 minutes.
2. **Don't stop minimizing so early.** Round 3 stopped with 24 clues never tried. Instead of quitting after 3 timeouts in a row, skip ahead to the least-crowded clues, or keep going while there's time left in the 20-minute budget. Fewer clues, same time.
3. **Spread out the starting clues** (farthest-apart cells of each shape) so the first solves don't hit the 60 s limit.

