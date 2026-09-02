#!/usr/bin/env python3
"""Tests for klayoutNetlist.py.

Two layers of checking. First a synthetic layout built here cell by cell, with
the expected netlist worked out on paper: two cells, one wire between them, and
a deliberate two-level via stack, so the extraction is measured against a known
answer rather than against itself.

Then the warmup, which has something better than a fixture — the authors' own
post-place-and-route DEF, listing every instance they placed. Extracting
`04_final.gds` and comparing cell-for-cell against that DEF is an independent
oracle: it catches a whole class of error a self-consistent fixture cannot.

Run:  python3 tests/testKlayoutNetlist.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

WARMUP_GDS = os.path.join(ROOT, "warmup", "04_final.gds")
WARMUP_DEF = os.path.join(ROOT, "warmup", "03_post_place_and_route.def")

try:
    import klayout.db as kdb
except ImportError:                                              # pragma: no cover
    print("klayoutNetlist: SKIPPED (no klayout module)")
    sys.exit(0)

import klayoutNetlist as kn                                      # noqa: E402
from lefLib import parse_lef                                     # noqa: E402

LI1, LI1_LABEL, MCON, MET1 = (67, 20), (67, 5), (67, 44), (68, 20)

TEST_LEF = """
MACRO TESTINV
  CLASS CORE ;
  SIZE 2.0 BY 2.72 ;
  PIN A
    DIRECTION INPUT ;
    PORT
      LAYER li1 ;
        RECT 0.2 1.0 0.5 1.4 ;
    END
  END A
  PIN Y
    DIRECTION OUTPUT ;
    PORT
      LAYER li1 ;
        RECT 1.5 1.0 1.8 1.4 ;
    END
  END Y
  PIN VPWR
    DIRECTION INOUT ; USE POWER ;
    PORT
      LAYER li1 ;
        RECT 0.0 2.6 2.0 2.72 ;
    END
  END VPWR
END TESTINV
END LIBRARY
"""


def build_fixture(path):
    """Two TESTINVs; u0.Y wired to u1.A on li1, then up to met1 and back down.

    Expected, by hand:
      instances  u0, u1
      nets       u0.A alone; u0.Y -> u1.A; u1.Y alone
      the power strap is its own net and must never appear
    """
    layout = kdb.Layout()
    layout.dbu = 0.001
    li1 = layout.layer(*LI1)
    lbl = layout.layer(*LI1_LABEL)
    mcon = layout.layer(*MCON)
    met1 = layout.layer(*MET1)

    cell = layout.create_cell("TESTINV")
    cell.shapes(li1).insert(kdb.Box(200, 1000, 500, 1400))          # A
    cell.shapes(li1).insert(kdb.Box(1500, 1000, 1800, 1400))        # Y
    cell.shapes(li1).insert(kdb.Box(0, 2600, 2000, 2720))           # VPWR
    cell.shapes(lbl).insert(kdb.Text("A", kdb.Trans(350, 1200)))
    cell.shapes(lbl).insert(kdb.Text("Y", kdb.Trans(1650, 1200)))
    cell.shapes(lbl).insert(kdb.Text("VPWR", kdb.Trans(1000, 2660)))

    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(0, 0)))
    top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(4000, 0)))
    # u0.Y (at x 1500..1800) up to met1, across, and back down into u1.A
    # (at x 4200..4500), so the join only holds if the via stack is followed.
    top.shapes(mcon).insert(kdb.Box(1600, 1100, 1750, 1300))
    top.shapes(mcon).insert(kdb.Box(4250, 1100, 4400, 1300))
    top.shapes(met1).insert(kdb.Box(1600, 1100, 4400, 1300))
    layout.write(path)


def extract_fixture(tmp):
    gds = os.path.join(tmp, "fixture.gds")
    lef = os.path.join(tmp, "test.lef")
    build_fixture(gds)
    with open(lef, "w") as handle:
        handle.write(TEST_LEF)
    return kn.extract(gds, parse_lef(lef), [])


def check_synthetic_layout():
    with tempfile.TemporaryDirectory() as tmp:
        graph = extract_fixture(tmp)

    assert graph["top"] == "TOP", graph["top"]
    assert graph["extractor"] == "klayout"
    assert len(graph["instances"]) == 2, graph["instances"]
    assert {i["cell"] for i in graph["instances"]} == {"TESTINV"}
    assert graph["cells"]["TESTINV"] == {"inputs": ["A"], "outputs": ["Y"]}, \
        graph["cells"]
    # Placement survives as the same matrix stage 3 emits, in database units.
    origins = sorted(tuple(i["transform"][4:]) for i in graph["instances"])
    assert origins == [(0, 0), (4000, 0)], origins

    wired = [n for n in graph["nets"] if len(n["endpoints"]) == 2]
    assert len(wired) == 1, [n["endpoints"] for n in graph["nets"]]
    assert sorted(tuple(e) for e in wired[0]["endpoints"]) == [
        ("u0", "Y", "output"), ("u1", "A", "input")], wired[0]["endpoints"]
    # Three nets in all: the joined one, plus each dangling pin.
    assert len(graph["nets"]) == 3, [n["endpoints"] for n in graph["nets"]]
    # The power strap connects both cells but must not appear as a signal.
    for net in graph["nets"]:
        for _inst, pin, _d in net["endpoints"]:
            assert pin != "VPWR", net
    print("  synthetic layout: 2 cells, via stack followed, power kept out")


def check_units_and_schema():
    with tempfile.TemporaryDirectory() as tmp:
        graph = extract_fixture(tmp)
    assert abs(graph["units"]["meters_per_db_unit"] - 1e-9) < 1e-15, \
        graph["units"]
    for key in ("top", "cells", "instances", "ports", "nets", "tied_pins",
                "const_nets", "warnings"):
        assert key in graph, "stage-3 schema is missing %r" % key
    print("  emits the stage-3 schema, so downstream stages need no changes")


def _warmup_graph():
    macros = parse_lef(os.path.join(ROOT, kn.DEFAULT_LEF))
    return kn.extract(WARMUP_GDS, macros, [])


def _def_components():
    """The authors' own placement list: an oracle this code cannot influence."""
    import re
    pattern = re.compile(r"^\s+- (\S+) (sky130_\S+)")
    counts, inside = {}, False
    for line in open(WARMUP_DEF):
        if line.startswith("COMPONENTS"):
            inside = True
            continue
        if line.startswith("END COMPONENTS"):
            break
        if not inside:
            continue
        match = pattern.match(line)
        if match and ("/" in match.group(1)
                      or match.group(1).startswith("clkbuf")):
            counts[match.group(2)] = counts.get(match.group(2), 0) + 1
    return counts


def check_against_the_authors_def():
    graph = _warmup_graph()
    got = {}
    for inst in graph["instances"]:
        got[inst["cell"]] = got.get(inst["cell"], 0) + 1
    want = _def_components()
    assert got == want, "extraction disagrees with the DEF:\n  %r\n  %r" % (
        sorted(got.items()), sorted(want.items()))
    assert sum(got.values()) == 79, sum(got.values())
    print("  warmup: 79 instances, cell-for-cell identical to the authors' DEF")


def check_names_come_from_the_layout():
    graph = _warmup_graph()
    ports = {p["name"] for p in graph["ports"]}
    assert ports == {"A", "B", "S", "clk", "en", "rst_n"}, ports
    directions = {p["name"]: p["direction"] for p in graph["ports"]}
    assert directions["S"] == "output", directions
    assert directions["clk"] == "input", directions
    # Pin names come from the cells' own labels, not from geometry matching.
    flop = graph["cells"]["sky130_fd_sc_hd__dfrtp_2"]
    assert flop["inputs"] == ["CLK", "D", "RESET_B"], flop
    assert flop["outputs"] == ["Q"], flop
    print("  port and pin names are read from the layout's own labels")


def check_downstream_stage_runs():
    from coneDecompose import decompose
    graph = _warmup_graph()
    result = decompose(graph, [])
    assert result["summary"]["registers"] == 16, result["summary"]
    assert result["summary"]["orphan_cells"] == 0, result["summary"]
    roots = {tuple(sorted(l["kind"] for l in c["leaves"]))
             for c in result["cones"]}
    assert roots, "no cones built from the klayout netlist"
    print("  coneDecompose runs on it unchanged: 16 registers, 0 orphan cells")


def main():
    print("klayoutNetlist")
    for fn in (check_synthetic_layout, check_units_and_schema,
               check_against_the_authors_def, check_names_come_from_the_layout,
               check_downstream_stage_runs):
        fn()
    print("OK")


if __name__ == "__main__":
    main()
