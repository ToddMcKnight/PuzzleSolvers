"""
Desktop GUI for the Nurikabe MILP solvers.

Launch from the repository root with:

    python Nurikabe/nurikabe_gui.py

Two solvers can be selected:

* "Original (nurikabe.py)"  - the flow-based Pyomo model in nurikabe.py, solved with HiGHS
* "Claude improved"         - claude_nurikabe_model/claude_nurikabe_model.py (loaded lazily)

Both are wrapped into the same interface:

    solve(num_rows, num_cols, clues, time_limit=None) -> dict
        clues: list of ((row, col), value), 1-indexed
        returns {'status': 'optimal'|'infeasible'|'time_limit'|'error',
                 'grid': list[list[str]] | None,   # '#' wall, '.' garden, str(v) clue
                 'solve_time': float, 'message': str}
"""
import csv
import importlib.util
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
IMPROVED_PATH = os.path.join(HERE, "claude_nurikabe_model", "claude_nurikabe_model.py")

SOLVER_ORIGINAL = "Original (nurikabe.py)"
SOLVER_IMPROVED = "Claude improved"

MIN_SIZE, MAX_SIZE = 1, 30
MAX_CLUE_DIGITS = 3


# ---------------------------------------------------------------------------
# Solver adapters
# ---------------------------------------------------------------------------

def _result(status, grid=None, solve_time=0.0, message=""):
    return {"status": status, "grid": grid, "solve_time": solve_time, "message": message}


def solve_original(num_rows, num_cols, clues, time_limit=None):
    """Wrap nurikabe.build_model + HiGHS into the common solve() dict interface."""
    start = time.perf_counter()
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import pandas as pd
        import pyomo.environ as pyo
        from pyomo.opt import TerminationCondition as TC
        import nurikabe

        clues_df = pd.DataFrame({"coords": [tuple(rc) for rc, _ in clues],
                                 "values": [int(v) for _, v in clues]})
        model = nurikabe.build_model(num_rows, num_cols, clues_df)

        solver = pyo.SolverFactory("highs")
        if time_limit is not None:
            solver.options["time_limit"] = float(time_limit)
        results = solver.solve(model, load_solutions=False)
        term = results.solver.termination_condition
    except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
        return _result("error", solve_time=time.perf_counter() - start,
                       message=f"{type(exc).__name__}: {exc}")

    elapsed = time.perf_counter() - start

    def load_grid():
        try:
            model.solutions.load_from(results)
        except Exception:  # no incumbent available
            return None
        clue_map = {tuple(rc): int(v) for rc, v in clues}
        grid = []
        for i in range(1, num_rows + 1):
            row = []
            for j in range(1, num_cols + 1):
                if (i, j) in clue_map:
                    row.append(str(clue_map[(i, j)]))
                else:
                    x = pyo.value(model.x[i, j], exception=False)
                    row.append("#" if x is not None and x >= 0.5 else ".")
            grid.append(row)
        return grid

    if term in (TC.optimal, TC.feasible, TC.locallyOptimal, TC.globallyOptimal):
        grid = load_grid()
        if grid is None:
            return _result("error", solve_time=elapsed, message="Solver reported success "
                           "but no solution could be loaded.")
        return _result("optimal", grid, elapsed, f"Solved ({term}).")
    if term in (TC.infeasible, TC.infeasibleOrUnbounded, TC.invalidProblem):
        return _result("infeasible", solve_time=elapsed,
                       message="The puzzle has no solution (model is infeasible).")
    if term in (TC.maxTimeLimit, TC.maxIterations, TC.maxEvaluations):
        grid = load_grid()
        msg = "Time limit reached" + (" (showing incumbent)." if grid else
                                      " before a solution was found.")
        return _result("time_limit", grid, elapsed, msg)
    return _result("error", solve_time=elapsed, message=f"Solver terminated: {term}")


def load_improved_module():
    """Import the improved model lazily. Returns (module, None) or (None, reason)."""
    if not os.path.isfile(IMPROVED_PATH):
        return None, f"File not found:\n{IMPROVED_PATH}"
    folder = os.path.dirname(IMPROVED_PATH)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        spec = importlib.util.spec_from_file_location("claude_nurikabe_model", IMPROVED_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        return None, f"Import failed: {type(exc).__name__}: {exc}"
    if not callable(getattr(module, "solve", None)):
        return None, "Module has no solve() function."
    sys.modules["claude_nurikabe_model"] = module
    return module, None


def make_improved_solver(module):
    """Wrap the improved module so exceptions become an 'error' result."""
    def solve_improved(num_rows, num_cols, clues, time_limit=None):
        start = time.perf_counter()
        try:
            res = module.solve(num_rows, num_cols, clues, time_limit=time_limit)
        except Exception as exc:  # noqa: BLE001
            return _result("error", solve_time=time.perf_counter() - start,
                           message=f"{type(exc).__name__}: {exc}")
        if not isinstance(res, dict) or "status" not in res:
            return _result("error", solve_time=time.perf_counter() - start,
                           message="Improved solver returned an unexpected value.")
        res.setdefault("grid", None)
        res.setdefault("solve_time", time.perf_counter() - start)
        res.setdefault("message", "")
        return res
    return solve_improved


# ---------------------------------------------------------------------------
# Pure helpers (CSV and validation), independent of tkinter
# ---------------------------------------------------------------------------

def read_puzzle_csv(path):
    """Return (num_rows, num_cols, {(r, c): value}) with 1-indexed coordinates."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [row for row in csv.reader(fh)]
    # Drop trailing empty lines; a line of bare commas is a real (clue-free) row
    while rows and not "".join(rows[-1]).strip() and len(rows[-1]) <= 1:
        rows.pop()
    if not rows:
        raise ValueError("CSV file is empty.")
    num_rows, num_cols = len(rows), max(len(r) for r in rows)
    if num_rows > MAX_SIZE or num_cols > MAX_SIZE:
        raise ValueError(f"Puzzle is {num_rows}x{num_cols}; the maximum is {MAX_SIZE}x{MAX_SIZE}.")
    clues = {}
    for i, row in enumerate(rows, start=1):
        for j, cell in enumerate(row, start=1):
            text = cell.strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                raise ValueError(f"Invalid value {text!r} at row {i}, column {j}.") from None
            if value != int(value) or value < 1:
                raise ValueError(f"Clue {text!r} at row {i}, column {j} is not a positive integer.")
            clues[(i, j)] = int(value)
    return num_rows, num_cols, clues


def write_puzzle_csv(path, num_rows, num_cols, clues):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        for i in range(1, num_rows + 1):
            fh.write(",".join(str(clues[(i, j)]) if (i, j) in clues else ""
                              for j in range(1, num_cols + 1)) + "\n")


def validate_puzzle(num_rows, num_cols, clues):
    """Return an error message, or None if the puzzle may be sent to a solver."""
    if not clues:
        return "Enter at least one clue before solving."
    total = sum(clues.values())
    if total > num_rows * num_cols:
        return (f"The clues sum to {total}, which exceeds the {num_rows * num_cols} "
                f"cells of a {num_rows}x{num_cols} grid.")
    return None


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class NurikabeApp:
    COLOR_EMPTY = "#ffffff"
    COLOR_WALL = "#2b2b2b"
    COLOR_GARDEN = "#eef5e9"
    COLOR_CLUE_SOLVED = "#dce9d3"
    COLOR_FOCUS = "#3d7bd9"

    def __init__(self, root, num_rows=10, num_cols=10):
        self.root = root
        root.title("Nurikabe Solver")
        root.minsize(560, 420)

        self.rows_var = tk.IntVar(value=num_rows)
        self.cols_var = tk.IntVar(value=num_cols)
        self.time_limit_var = tk.StringVar()
        self.solver_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready.")

        self.num_rows = 0
        self.num_cols = 0
        self.cell_vars = {}      # (r, c) -> StringVar, 1-indexed
        self.cell_entries = {}   # (r, c) -> Entry
        self.cell_frames = {}    # (r, c) -> Frame (fixed pixel size)
        self.solution = None     # currently displayed grid, or None
        self._suppress_trace = False

        self.improved_module, self.improved_error = load_improved_module()

        self._solve_thread = None
        self._solve_result = None
        self._solve_start = 0.0

        self.font_bold = tkfont.Font(family="Segoe UI", size=-16, weight="bold")
        self._vcmd = (root.register(self._validate_cell_text), "%P")

        self._build_controls()
        self._build_grid_area()
        self._build_status_bar()
        self.create_grid(num_rows, num_cols)

        root.bind("<Control-o>", lambda e: self.load_csv())
        root.bind("<Control-s>", lambda e: self.save_csv())
        root.bind("<F5>", lambda e: self.solve())

    # ----- layout -----------------------------------------------------------

    def _build_controls(self):
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(side=tk.TOP, fill=tk.X)

        size = ttk.LabelFrame(bar, text="Grid size", padding=6)
        size.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(size, text="Rows").grid(row=0, column=0, padx=(0, 4))
        ttk.Spinbox(size, from_=MIN_SIZE, to=MAX_SIZE, width=4,
                    textvariable=self.rows_var).grid(row=0, column=1)
        ttk.Label(size, text="Cols").grid(row=0, column=2, padx=(8, 4))
        ttk.Spinbox(size, from_=MIN_SIZE, to=MAX_SIZE, width=4,
                    textvariable=self.cols_var).grid(row=0, column=3)
        ttk.Button(size, text="Create / resize", command=self.on_resize).grid(
            row=0, column=4, padx=(8, 0))

        solver = ttk.LabelFrame(bar, text="Solver", padding=6)
        solver.pack(side=tk.LEFT, padx=(0, 8))
        self.solver_combo = ttk.Combobox(solver, textvariable=self.solver_var, state="readonly",
                                         width=22, values=[SOLVER_IMPROVED, SOLVER_ORIGINAL])
        self.solver_combo.grid(row=0, column=0, columnspan=2)
        self.solver_combo.bind("<<ComboboxSelected>>", self._on_solver_selected)
        self.solver_var.set(SOLVER_IMPROVED if self.improved_module else SOLVER_ORIGINAL)
        ttk.Label(solver, text="Time limit (s)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(solver, textvariable=self.time_limit_var, width=8).grid(
            row=1, column=1, sticky="e", pady=(4, 0))

        actions = ttk.Frame(bar)
        actions.pack(side=tk.LEFT, fill=tk.Y)
        self.solve_button = ttk.Button(actions, text="Solve", command=self.solve)
        self.solve_button.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Clear solution", command=self.clear_solution).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Clear all", command=self.clear_all).grid(
            row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Load CSV...", command=self.load_csv).grid(
            row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(actions, text="Save CSV...", command=self.save_csv).grid(
            row=1, column=1, sticky="ew", padx=2, pady=2)

    def _build_grid_area(self):
        outer = ttk.Frame(self.root, padding=(8, 4, 8, 4))
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0, background="#d9d9d9")
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.canvas.yview)
        hbar = ttk.Scrollbar(outer, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        self.grid_frame = tk.Frame(self.canvas, background="#808080")
        self._grid_window = self.canvas.create_window(0, 0, window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self._update_scroll_region())
        self.canvas.bind("<Configure>", lambda e: self._schedule_rescale())
        self._rescale_job = None

    def _build_status_bar(self):
        bar = ttk.Frame(self.root, padding=(8, 2, 8, 6))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(self.root).pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X,
                                                                       expand=True)
        ttk.Label(bar, text="Arrows/Tab: move   Del: clear   F5: solve",
                  foreground="#666666").pack(side=tk.RIGHT)

    # ----- grid construction ------------------------------------------------

    def on_resize(self):
        try:
            rows, cols = int(self.rows_var.get()), int(self.cols_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid size", "Rows and columns must be whole numbers.")
            return
        if not (MIN_SIZE <= rows <= MAX_SIZE and MIN_SIZE <= cols <= MAX_SIZE):
            messagebox.showerror("Invalid size",
                                 f"Rows and columns must be between {MIN_SIZE} and {MAX_SIZE}.")
            return
        self.create_grid(rows, cols, keep=self.get_clues())

    def create_grid(self, num_rows, num_cols, keep=None):
        """(Re)build the cell widgets; clues in `keep` that still fit are preserved."""
        if self.is_solving():
            return
        for frame in self.cell_frames.values():
            frame.destroy()
        self.cell_vars.clear()
        self.cell_entries.clear()
        self.cell_frames.clear()
        self.solution = None
        self.num_rows, self.num_cols = num_rows, num_cols
        self.rows_var.set(num_rows)
        self.cols_var.set(num_cols)

        px = self._cell_pixels()
        self._apply_font_size(px)
        for r in range(1, num_rows + 1):
            for c in range(1, num_cols + 1):
                frame = tk.Frame(self.grid_frame, width=px, height=px)
                frame.grid(row=r - 1, column=c - 1, padx=(0, 1), pady=(0, 1))
                frame.pack_propagate(False)
                var = tk.StringVar()
                entry = tk.Entry(frame, textvariable=var, justify="center", relief="flat", bd=0,
                                 font=self.font_bold, background=self.COLOR_EMPTY,
                                 insertwidth=1, highlightthickness=2,
                                 highlightcolor=self.COLOR_FOCUS,
                                 highlightbackground=self.COLOR_EMPTY,
                                 validate="key", validatecommand=self._vcmd)
                entry.pack(fill=tk.BOTH, expand=True)
                self._bind_cell(entry, r, c)
                var.trace_add("write", lambda *_: self._on_cell_edited())
                self.cell_vars[(r, c)] = var
                self.cell_entries[(r, c)] = entry
                self.cell_frames[(r, c)] = frame

        self.set_clues(keep or {})
        self.grid_frame.configure(padx=1, pady=1)
        self._update_scroll_region()
        self.status_var.set(f"{num_rows}x{num_cols} grid. Type clue numbers into the cells.")

    def _bind_cell(self, entry, r, c):
        moves = {"<Up>": (-1, 0), "<Down>": (1, 0), "<Left>": (0, -1), "<Right>": (0, 1),
                 "<Return>": (1, 0)}
        for seq, (dr, dc) in moves.items():
            entry.bind(seq, lambda e, dr=dr, dc=dc: self._move_focus(r + dr, c + dc))
        entry.bind("<Tab>", lambda e: self._step_focus(r, c, +1))
        entry.bind("<Shift-Tab>", lambda e: self._step_focus(r, c, -1))
        entry.bind("<ISO_Left_Tab>", lambda e: self._step_focus(r, c, -1))
        entry.bind("<Delete>", lambda e: self._clear_cell(r, c))
        entry.bind("<BackSpace>", lambda e: self._clear_cell(r, c))
        entry.bind("<FocusIn>", lambda e: e.widget.select_range(0, tk.END))

    def _move_focus(self, r, c):
        r = min(max(r, 1), self.num_rows)
        c = min(max(c, 1), self.num_cols)
        self.cell_entries[(r, c)].focus_set()
        return "break"

    def _step_focus(self, r, c, step):
        idx = ((r - 1) * self.num_cols + (c - 1) + step) % (self.num_rows * self.num_cols)
        return self._move_focus(idx // self.num_cols + 1, idx % self.num_cols + 1)

    def _clear_cell(self, r, c):
        self.cell_vars[(r, c)].set("")
        return "break"

    @staticmethod
    def _validate_cell_text(proposed):
        if proposed == "":
            return True
        return (proposed.isdigit() and proposed[0] != "0"
                and len(proposed) <= MAX_CLUE_DIGITS)

    # ----- sizing -----------------------------------------------------------

    def _cell_pixels(self):
        """Fit cells to the visible canvas, clamped to a readable range (scroll beyond)."""
        self.root.update_idletasks()
        w = max(self.canvas.winfo_width(), 300)
        h = max(self.canvas.winfo_height(), 250)
        rows, cols = max(self.num_rows, 1), max(self.num_cols, 1)
        px = min((w - 4) // cols, (h - 4) // rows) - 1
        return int(min(max(px, 22), 48))

    def _apply_font_size(self, px):
        size = -max(int(px * 0.5), 9)
        self.font_bold.configure(size=size)

    def _schedule_rescale(self):
        if self._rescale_job is not None:
            self.root.after_cancel(self._rescale_job)
        self._rescale_job = self.root.after(120, self._rescale)

    def _rescale(self):
        self._rescale_job = None
        if not self.cell_frames:
            return
        px = self._cell_pixels()
        any_frame = next(iter(self.cell_frames.values()))
        if int(any_frame.cget("width")) != px:
            self._apply_font_size(px)
            for frame in self.cell_frames.values():
                frame.configure(width=px, height=px)
        self._update_scroll_region()

    def _update_scroll_region(self):
        self.root.update_idletasks()
        fw, fh = self.grid_frame.winfo_reqwidth(), self.grid_frame.winfo_reqheight()
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        # Center the board when it is smaller than the viewport
        self.canvas.coords(self._grid_window, max((cw - fw) // 2, 0), max((ch - fh) // 2, 0))
        self.canvas.configure(scrollregion=(0, 0, max(fw, cw), max(fh, ch)))

    # ----- clue access ------------------------------------------------------

    def get_clues(self):
        """Return {(r, c): value} for all non-empty cells (1-indexed)."""
        clues = {}
        for rc, var in self.cell_vars.items():
            text = var.get().strip()
            if text.isdigit() and int(text) > 0:
                clues[rc] = int(text)
        return clues

    def set_clues(self, clues):
        self._suppress_trace = True
        try:
            for rc, var in self.cell_vars.items():
                var.set(str(clues[rc]) if rc in clues else "")
        finally:
            self._suppress_trace = False
        self.clear_solution()

    def _on_cell_edited(self):
        if not self._suppress_trace and self.solution is not None:
            self.clear_solution()
            self.status_var.set("Puzzle edited; solution cleared.")

    # ----- solution display -------------------------------------------------

    def show_solution(self, grid):
        self.solution = grid
        for (r, c), entry in self.cell_entries.items():
            mark = grid[r - 1][c - 1]
            if mark == "#":
                bg, fg = self.COLOR_WALL, self.COLOR_WALL
            elif mark == ".":
                bg, fg = self.COLOR_GARDEN, "#000000"
            else:
                bg, fg = self.COLOR_CLUE_SOLVED, "#000000"
            entry.configure(background=bg, foreground=fg, insertbackground=fg,
                            highlightbackground=bg)

    def clear_solution(self):
        self.solution = None
        for entry in self.cell_entries.values():
            entry.configure(background=self.COLOR_EMPTY, foreground="#000000",
                            insertbackground="#000000", highlightbackground=self.COLOR_EMPTY)

    def clear_all(self):
        if self.is_solving():
            return
        self.set_clues({})
        self.status_var.set("Grid cleared.")

    # ----- CSV --------------------------------------------------------------

    def load_csv(self, path=None):
        if self.is_solving():
            return False
        if path is None:
            path = filedialog.askopenfilename(
                title="Load Nurikabe puzzle", initialdir=HERE,
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if not path:
                return False
        try:
            rows, cols, clues = read_puzzle_csv(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Load failed", str(exc))
            return False
        self.create_grid(rows, cols, keep=clues)
        self.status_var.set(f"Loaded {os.path.basename(path)} ({rows}x{cols}, "
                            f"{len(clues)} clues).")
        return True

    def save_csv(self, path=None):
        if path is None:
            path = filedialog.asksaveasfilename(
                title="Save Nurikabe puzzle", initialdir=HERE, defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
            if not path:
                return False
        try:
            write_puzzle_csv(path, self.num_rows, self.num_cols, self.get_clues())
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return False
        self.status_var.set(f"Saved {os.path.basename(path)}.")
        return True

    # ----- solving ----------------------------------------------------------

    def _on_solver_selected(self, _event=None):
        if self.solver_var.get() == SOLVER_IMPROVED and self.improved_module is None:
            # Retry: the file may have appeared since startup
            self.improved_module, self.improved_error = load_improved_module()
            if self.improved_module is None:
                messagebox.showwarning(
                    "Improved solver unavailable",
                    "The Claude improved model could not be loaded, so the original "
                    f"solver will be used.\n\n{self.improved_error}")
                self.solver_var.set(SOLVER_ORIGINAL)

    def get_solver(self):
        if self.solver_var.get() == SOLVER_IMPROVED:
            self._on_solver_selected()
            if self.improved_module is not None:
                return SOLVER_IMPROVED, make_improved_solver(self.improved_module)
        return SOLVER_ORIGINAL, solve_original

    def _parse_time_limit(self):
        text = self.time_limit_var.get().strip()
        if not text:
            return None
        value = float(text)  # ValueError handled by caller
        if value <= 0:
            raise ValueError
        return value

    def is_solving(self):
        return self._solve_thread is not None and self._solve_thread.is_alive()

    def solve(self, background=True):
        """Validate, then run the selected solver (in a worker thread by default)."""
        if self.is_solving():
            return False
        clues = self.get_clues()
        error = validate_puzzle(self.num_rows, self.num_cols, clues)
        if error:
            messagebox.showerror("Invalid puzzle", error)
            return False
        try:
            time_limit = self._parse_time_limit()
        except ValueError:
            messagebox.showerror("Invalid time limit",
                                 "Time limit must be a positive number of seconds, or blank.")
            return False

        name, solver_fn = self.get_solver()
        clue_list = sorted(clues.items())
        args = (self.num_rows, self.num_cols, clue_list, time_limit)
        self.clear_solution()
        self.solve_button.configure(state=tk.DISABLED)
        self._solve_result = None
        self._solve_start = time.perf_counter()
        self._solve_name = name
        self._solve_shape = (self.num_rows, self.num_cols)

        def worker():
            try:
                self._solve_result = solver_fn(*args)
            except Exception as exc:  # noqa: BLE001
                self._solve_result = _result("error", message=f"{type(exc).__name__}: {exc}")

        if background:
            self._solve_thread = threading.Thread(target=worker, daemon=True)
            self._solve_thread.start()
            self._poll_solve()
        else:
            worker()
            self._finish_solve()
        return True

    def _poll_solve(self):
        if self.is_solving():
            elapsed = time.perf_counter() - self._solve_start
            self.status_var.set(f"Solving with {self._solve_name}... {elapsed:.1f}s")
            self.root.after(100, self._poll_solve)
        else:
            self._finish_solve()

    def _finish_solve(self):
        self._solve_thread = None
        self.solve_button.configure(state=tk.NORMAL)
        res = self._solve_result or _result("error", message="No result returned.")
        status, grid = res.get("status"), res.get("grid")
        t = res.get("solve_time") or 0.0
        msg = res.get("message") or ""
        shape_ok = (grid is not None and len(grid) == self._solve_shape[0]
                    and all(len(row) == self._solve_shape[1] for row in grid))

        if grid is not None and shape_ok and self._solve_shape == (self.num_rows, self.num_cols):
            self.show_solution(grid)
        summary = f"{self._solve_name}: {status} in {t:.2f}s"
        self.status_var.set(summary + (f" - {msg}" if msg else ""))

        if status == "infeasible":
            messagebox.showerror("No solution", msg or "The puzzle is infeasible.")
        elif status == "time_limit" and not shape_ok:
            messagebox.showwarning("Time limit", msg or "Time limit reached without a solution.")
        elif status == "error" or (status == "optimal" and not shape_ok):
            messagebox.showerror("Solver error", msg or "The solver returned an invalid grid.")


def main():
    if sys.platform == "win32":
        try:  # crisp text on high-DPI displays; must happen before Tk() is created
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - best-effort only
            pass
    root = tk.Tk()
    app = NurikabeApp(root)
    if app.improved_module is None:
        app.status_var.set("Claude improved model not available; using the original solver.")
    root.geometry("900x760")
    root.mainloop()


if __name__ == "__main__":
    main()
