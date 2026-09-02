#!/usr/bin/env python3
"""lefLib.py — parse the standard-cell library LEF into per-cell pin geometry.

Import-only helper for stage 2 (plus a tiny CLI summary). Reads the MACRO sections of a
LEF file (e.g. the public sky130_fd_sc_hd merged LEF in lib/) and returns, per cell:

  Macro(name, size_um=(w, h),
        pins={pin_name: Pin(name, direction, use, rects=[(layer, x1, y1, x2, y2), ...])})

Coordinates are in microns exactly as the LEF states them (floats); the consumer scales
them to database units using the GDS file's own UNITS record. Only PIN/PORT geometry is
kept — OBS (obstructions) carry no connectivity and are skipped.

Classification helpers, derived from the LEF alone (no name lists):
- signal pins: USE SIGNAL or USE CLOCK; power pins: USE POWER or USE GROUND.
- a macro with no signal pins at all (decap, tap, fill, diode) is `is_filler`.

This file knows the LEF grammar, not any particular design.
"""

import re
from collections import namedtuple

Pin = namedtuple("Pin", "name direction use rects")
Macro = namedtuple("Macro", "name size_um pins")

SIGNAL_USES = {"SIGNAL", "CLOCK"}
POWER_USES = {"POWER", "GROUND"}
POWER_PIN_NAMES = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VCC", "GND", "KAPWR"}
WELL_LAYERS = {"nwell", "pwell"}


def is_power_pin(pin):
    """True for supply/bulk pins. USE POWER/GROUND is authoritative, but some
    macros (e.g. sky130_ef decaps) omit USE on their VNB/VPB well taps — catch
    those by rail-style name or by geometry living only on well layers."""
    if pin.use in POWER_USES:
        return True
    if pin.name.upper() in POWER_PIN_NAMES:
        return True
    if pin.rects and all(r[0] in WELL_LAYERS for r in pin.rects):
        return True
    return False


def parse_lef(path):
    """Parse a LEF file -> {macro_name: Macro}."""
    macros = {}
    with open(path) as f:
        text = f.read()

    # LEF statements end with ';'; sections nest via MACRO/PIN/PORT ... END.
    tokens = re.sub(r"#[^\n]*", "", text).split()
    i, n = 0, len(tokens)

    def skip_statement(j):
        while j < n and tokens[j] != ";":
            j += 1
        return j + 1

    while i < n:
        if tokens[i] != "MACRO":
            i += 1
            continue
        name = tokens[i + 1]
        i += 2
        size = None
        pins = {}
        while i < n:
            t = tokens[i]
            if t == "END" and i + 1 < n and tokens[i + 1] == name:
                i += 2
                break
            if t == "SIZE":
                size = (float(tokens[i + 1]), float(tokens[i + 3]))  # SIZE w BY h ;
                i = skip_statement(i)
            elif t == "PIN":
                pin_name = tokens[i + 1]
                i += 2
                direction, use, rects = None, "SIGNAL", []
                while i < n:
                    t = tokens[i]
                    if t == "END" and i + 1 < n and tokens[i + 1] == pin_name:
                        i += 2
                        break
                    if t == "DIRECTION":
                        direction = tokens[i + 1]
                        i = skip_statement(i)
                    elif t == "USE":
                        use = tokens[i + 1]
                        i = skip_statement(i)
                    elif t == "PORT":
                        i += 1
                        layer = None
                        while i < n and tokens[i] != "END":
                            if tokens[i] == "LAYER":
                                layer = tokens[i + 1]
                                i = skip_statement(i)
                            elif tokens[i] == "RECT":
                                rects.append((layer,
                                              float(tokens[i + 1]), float(tokens[i + 2]),
                                              float(tokens[i + 3]), float(tokens[i + 4])))
                                i = skip_statement(i)
                            else:
                                i = skip_statement(i)
                        i += 1  # END (of PORT)
                    else:
                        i = skip_statement(i)
                pins[pin_name] = Pin(pin_name, direction, use, rects)
            elif t == "OBS":
                i += 1
                while i < n and tokens[i] != "END":
                    i = skip_statement(i)
                i += 1
            else:
                i = skip_statement(i)
        macros[name] = Macro(name, size, pins)
    return macros


def signal_pins(macro):
    return {p.name: p for p in macro.pins.values()
            if p.use in SIGNAL_USES and not is_power_pin(p)}


def is_filler(macro):
    """Carries no logic: no signal pins at all (decap, tap, fill), or signal inputs
    but no output (antenna diodes, bleeders) — nothing downstream can depend on it."""
    pins = signal_pins(macro)
    return not any(p.direction == "OUTPUT" for p in pins.values())


def match_structure(struct_name, macros):
    """Map a GDS structure name to a library macro name, tolerating prefixes/suffixes
    added by the flow. Exact match first, then unique substring containment."""
    if struct_name in macros:
        return struct_name
    candidates = [m for m in macros if m in struct_name]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return max(candidates, key=len)   # longest name wins ("...inv_16" over "...inv_1")
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Summarize a LEF standard-cell library.")
    ap.add_argument("lef", nargs="?", default="lib/sky130_fd_sc_hd_merged.lef")
    args = ap.parse_args()
    macros = parse_lef(args.lef)
    fillers = sum(1 for m in macros.values() if is_filler(m))
    print("%s: %d macros (%d filler/no-signal-pin)" % (args.lef, len(macros), fillers))
    layers = {}
    for m in macros.values():
        for p in signal_pins(m).values():
            for r in p.rects:
                layers[r[0]] = layers.get(r[0], 0) + 1
    print("signal-pin rects by layer:", dict(sorted(layers.items())))


if __name__ == "__main__":
    main()
