"""
cellLibrary.py -- the sky130 standard-cell library, derived from
`sky130_prims.v` rather than written out by hand.

The behavioural Verilog is the only real statement of what a cell is, and a
second copy of that fact in Python is a copy that drifts: a mistyped pin name
or a stale function makes downstream matching quietly stop working instead of
failing loudly. Deriving it means a cell added to the Verilog is a cell every
consumer immediately understands.

Consumers get two things: `cell_class()`, which strips the vendor prefix and
drive-strength suffix so `and2_2` and `and2_4` are one class, and `CELLS`,
a {class: CellSpec} map carrying each cell's pins, Boolean function, derived
pin symmetry and whether it is sequential.

This module is import-only -- it has no CLI.
"""

import itertools
import os
import re
from collections import defaultdict, namedtuple

CELL_PREFIX = "sky130_fd_sc_hd__"


# ---------------------------------------------------------------------------
# Cell library
# ---------------------------------------------------------------------------
# The library is READ FROM sky130_prims.v rather than written out here. The
# behavioural Verilog is the only real statement of what a cell is, and a
# second copy of that fact in Python is a copy that drifts: a mistyped pin
# name or a stale function makes templates quietly stop matching instead of
# failing loudly. Deriving it means a cell added to the Verilog is a cell
# this matcher immediately understands.
#
# Entries are keyed by "cell class": the library name with the sky130 prefix
# and the drive-strength suffix stripped (sky130_fd_sc_hd__and2_2 -> AND2),
# since drive strength is a physical property with no bearing on function --
# clkbuf_4, clkbuf_8 and clkbuf_16 are one class, CLKBUF.
#
#   ins  -- input pin names, in the cell's declared order
#   out  -- the primary output pin (None for a cell with no output at all,
#           like an antenna diode)
#   sym  -- groups of input positions that are interchangeable, DERIVED by
#           testing the function rather than asserted: the matcher may try
#           A&B against B&A, but never A0/A1/S of a mux in the wrong order
#   fn   -- the Boolean function, keyed by pin name
#   seq  -- True for state elements (a clocked always block); the walk stops
#           at these
#   extra_outs -- ((pin, fn), ...) for the rare multi-output cell (conb)
CellSpec = namedtuple("CellSpec", "ins out sym fn seq extra_outs")


def cell_class(cell_name):
    """sky130_fd_sc_hd__and2_2 -> AND2; clkbuf_4/_8/_16 -> CLKBUF."""
    base = cell_name[len(CELL_PREFIX):] if cell_name.startswith(CELL_PREFIX) else cell_name
    return re.sub(r"_\d+$", "", base).upper()


def verilog_to_python(expr):
    """Rewrite a 1-bit Verilog expression as an equivalent Python one.

    & | ^ ~ and parentheses carry over as-is; Python's ~ makes intermediates
    negative, which is harmless because every operator here is bitwise and
    the caller masks the result back to one bit. Sized literals and the one
    conditional form the library uses are rewritten explicitly."""
    expr = re.sub(r"\d*'b([01])", r"\1", expr).strip()
    ternary = re.fullmatch(r"(.+?)\?(.+?):(.+)", expr, re.DOTALL)
    if ternary:
        cond, then, other = (part.strip() for part in ternary.groups())
        if any("?" in part for part in (cond, then, other)):
            raise ValueError(f"nested conditional not supported: {expr}")
        expr = f"(({then}) if ({cond}) else ({other}))"
    return expr


def derive_symmetry(ins, fn):
    """Which inputs are interchangeable, established by trying it: swap two
    inputs, and if the truth table is unchanged for every assignment they
    are symmetric. Groups are the connected components of that relation,
    each confirmed closed under all its own permutations before being
    trusted (a component that isn't is dropped rather than assumed)."""
    n = len(ins)
    if n < 2 or fn is None:
        return ()

    def table(order):
        return tuple(fn(**{ins[order[k]]: (a >> k) & 1 for k in range(n)})
                     for a in range(1 << n))

    base = table(tuple(range(n)))

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in itertools.combinations(range(n), 2):
        order = list(range(n))
        order[i], order[j] = order[j], order[i]
        if table(tuple(order)) == base:
            parent[find(j)] = find(i)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    confirmed = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ok = True
        for perm in itertools.permutations(group):
            order = list(range(n))
            for slot, src in zip(group, perm):
                order[slot] = src
            if table(tuple(order)) != base:
                ok = False
                break
        if ok:
            confirmed.append(tuple(sorted(group)))
    return tuple(sorted(confirmed))


def load_cell_library(path):
    """Parse sky130_prims.v into {class: CellSpec}.

    A module with continuous assignments is combinational and its functions
    are compiled from those assignments; a module with a clocked always
    block is sequential and gets no function (the walk stops there, so no
    template ever needs to evaluate it). Drive-strength variants of the
    same class must agree -- they are the same cell -- and disagreement is
    an error rather than a silent last-one-wins."""
    with open(path) as f:
        text = f.read()

    cells = {}
    for name, ports, body in re.findall(
            r"module\s+(\w+)\s*\((.*?)\);(.*?)endmodule", text, re.DOTALL):
        declared = re.findall(r"\b(input|output)\s+(?:reg\s+|wire\s+)?(\w+)", ports)
        ins = tuple(pin for kind, pin in declared if kind == "input")
        outs = tuple(pin for kind, pin in declared if kind == "output")
        seq = bool(re.search(r"\balways\b", body))

        fns = {}
        if not seq:
            for target, expr in re.findall(r"assign\s+(\w+)\s*=\s*(.*?);", body, re.DOTALL):
                source = (f"lambda {', '.join(ins)}: "
                          f"({verilog_to_python(expr)}) & 1")
                # The only input is this repo's own cell library, and the
                # expression grammar accepted above is a handful of bitwise
                # operators -- but evaluate with no builtins regardless.
                fns[target] = eval(source, {"__builtins__": {}})  # noqa: S307

        primary = outs[0] if outs else None
        spec = CellSpec(
            ins=ins,
            out=primary,
            sym=derive_symmetry(ins, fns.get(primary)),
            fn=fns.get(primary),
            seq=seq,
            extra_outs=tuple((pin, fns[pin]) for pin in outs[1:] if pin in fns),
        )

        cls = cell_class(name)
        if cls in cells:
            before = cells[cls]
            assert before.ins == spec.ins and before.out == spec.out, (
                f"{cls}: drive-strength variants disagree on pinout "
                f"({before.ins}->{before.out} vs {spec.ins}->{spec.out})")
            continue
        cells[cls] = spec
    return cells


HERE = os.path.dirname(os.path.abspath(__file__))
CELLS = load_cell_library(os.path.join(HERE, "sky130_prims.v"))
