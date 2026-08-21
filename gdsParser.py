"""
gdsParser.py -- a from-scratch GDSII cell/polygon parser, written to *learn*
the GDSII stream format (no gdstk/gdspy/klayout libraries used on purpose).

=====================================================================
GDSII 101 (read this before reading the code below)
=====================================================================

A .gds file is NOT text -- it's a flat, binary stream of "records".
There is no tree structure baked into the bytes; nesting (library ->
structures -> elements) is implied purely by BEGIN/END marker records
that we have to track ourselves as we read sequentially.

Every record has the exact same 4-byte header:

    byte 0-1 : record length, BIG-ENDIAN uint16, in bytes,
               INCLUDING these 4 header bytes.
    byte 2   : record type   (what kind of record this is, e.g. "LAYER")
    byte 3   : data type     (how to interpret the payload bytes)
    byte 4.. : payload       (length - 4 bytes of actual data)

Data types we care about (byte 3 of the header):
    0x00  no data at all (pure marker record, e.g. ENDEL, ENDSTR)
    0x01  bit array   (2 bytes)
    0x02  2-byte signed integer  (int16)
    0x03  4-byte signed integer  (int32)
    0x04  4-byte "GDS real" float (rare, obsolete)
    0x05  8-byte "GDS real" float (used for UNITS)
    0x06  ASCII string, padded with a trailing 0x00 byte if odd length

A "structure" in GDS-speak is what everyone else calls a CELL: a named
container of drawing elements (and/or references to other structures).
Structures are delimited by:

    BGNSTR  -- "a structure starts here" (payload = creation/mod timestamps)
    STRNAME -- "the structure you just started is named ..." (ASCII string)
    ...elements...
    ENDSTR  -- "the structure ends here"

The library itself is just one big flat list of structures:

    HEADER, BGNLIB, LIBNAME, UNITS, [structure]* , ENDLIB

Inside a structure, each drawing ELEMENT also follows a begin/end
pattern:

    BOUNDARY   -- "a filled polygon starts here" <-- what we care about!
    LAYER      -- which GDS layer number the polygon lives on (int16)
    DATATYPE   -- a sub-classification of that layer (int16)
    XY         -- the actual (x, y) vertex coordinates, in database units
    ENDEL      -- "the element ends here"

(There are other element types -- PATH for wires, SREF/AREF for
placing an instance of another structure [like a Verilog module
instantiation!], TEXT for on-die labels, BOX for a rarer rectangle
primitive. This first version only cares about BOUNDARY, since that's
what actually draws the silicon/metal/etc. shapes -- "polygons".)

Crucially: GDS layer numbers are just bare integers. The file itself
does NOT say "layer 66 is polysilicon" anywhere -- that mapping lives
in the PDK (Process Design Kit) the chip was manufactured with, in a
separate "layer map" file that KLayout/etc. read. This design uses
standard-cell names like "sky130_fd_sc_hd__nand3_2", which tells us
the layer numbers below follow SkyWater's open-source sky130 PDK
convention. The LAYER_MATERIAL_MAP below hard-codes that mapping so we
can translate raw numbers into human names like "poly" or "met1".
=====================================================================
"""

# `import X` pulls in one of Python's built-in "standard library" modules --
# code that ships with Python itself, no install needed. Each is used for a
# specific job below:
import argparse    # parses command-line flags like `--json OUT.json`
import json        # reads/writes JSON (the OUT.json we produce)
import os          # filesystem helpers (building file paths below)
import struct      # decodes raw bytes into numbers (ints/floats) -- the
                    # core tool for reading a BINARY file format like GDS
from collections import Counter, defaultdict
    # Counter: a dict subclass specialized for counting things
    #   (Counter()["poly"] += 1 works even the first time "poly" is seen --
    #   a plain dict would raise KeyError until you initialized it to 0).
    # defaultdict: a dict that auto-creates a default value the first time
    #   a new key is accessed, instead of raising KeyError. We use
    #   `defaultdict(Counter)` below, meaning "a dict where every new key
    #   automatically gets an empty Counter() as its value."

# ---------------------------------------------------------------------------
# 1. Record type codes (byte offset 2 of every record header).
#    This is the full GDSII spec list -- we only *handle* a handful of
#    these, but having the names makes the parser self-documenting and
#    lets us print a sane error if we ever hit something unexpected.
#
#    Python mechanics note: each line below (HEADER = 0x00, BGNLIB = 0x01,
#    ...) just creates a plain variable holding a small integer. `0x00` is
#    hex notation for zero, `0x0A` is hex for 10, etc. -- hex is used here
#    purely because that's how the GDSII spec documents these codes, so
#    it's easy to cross-reference. These are "module-level constants":
#    plain variables sitting at the top of the file (not inside any
#    function), so every function below can see and use them.
# ---------------------------------------------------------------------------
HEADER = 0x00
BGNLIB = 0x01
LIBNAME = 0x02
UNITS = 0x03
ENDLIB = 0x04
BGNSTR = 0x05
STRNAME = 0x06
ENDSTR = 0x07
BOUNDARY = 0x08     # <- element: a filled polygon. This is our target.
PATH = 0x09         # element: a routed wire (a polygon drawn along a line)
SREF = 0x0A         # element: "place an instance of another structure here"
AREF = 0x0B         # element: an array of instances (e.g. a memory array)
TEXT = 0x0C         # element: a text label (not physically manufactured)
LAYER = 0x0D
SNAME = 0x12
COLROW = 0x13
TEXTTYPE = 0x16
PRESENTATION = 0x17
STRING = 0x19
STRANS = 0x1A
MAG = 0x1B
ANGLE = 0x1C
PATHTYPE = 0x21
DATATYPE = 0x0E
WIDTH = 0x0F
XY = 0x10
ENDEL = 0x11        # marker: "the element that started above ends here"
BOX = 0x2D          # element: a rarer rectangle primitive (also a polygon)
BOXTYPE = 0x2E
BGNEXTN = 0x30
ENDEXTN = 0x31

# Record types that *start* a drawable element. Every one of these is
# eventually closed by a matching ENDEL record.
#
# `{ ... }` with comma-separated values (no colons) is a Python SET: an
# unordered bag of unique values. We use a set (not a list) because all we
# ever do with this is ask "is rectype one of these?" (`rectype in
# ELEMENT_START_TYPES`) -- a set answers that in constant time, no matter
# how many entries it has, whereas checking a list has to scan it one by
# one. Small performance detail, but it's the idiomatic Python choice for
# "membership tests."
ELEMENT_START_TYPES = {BOUNDARY, PATH, SREF, AREF, TEXT, BOX}

# Human-readable names, purely for debug printing / error messages.
#
# `{ key: value, ... }` is a Python DICT (dictionary): a lookup table from
# keys to values, e.g. `RECORD_NAMES[0x08]` gives back the string
# "BOUNDARY". Here the keys are the integer constants defined above (like
# BOUNDARY, which is just another name for 0x08) and the values are their
# human-readable names. This dict is never used to change behavior -- only
# to make debug/error text readable instead of a wall of hex numbers.
RECORD_NAMES = {
    HEADER: "HEADER", BGNLIB: "BGNLIB", LIBNAME: "LIBNAME", UNITS: "UNITS",
    ENDLIB: "ENDLIB", BGNSTR: "BGNSTR", STRNAME: "STRNAME", ENDSTR: "ENDSTR",
    BOUNDARY: "BOUNDARY", PATH: "PATH", SREF: "SREF", AREF: "AREF",
    TEXT: "TEXT", LAYER: "LAYER", SNAME: "SNAME", COLROW: "COLROW",
    TEXTTYPE: "TEXTTYPE", PRESENTATION: "PRESENTATION", STRING: "STRING",
    STRANS: "STRANS", MAG: "MAG", ANGLE: "ANGLE", PATHTYPE: "PATHTYPE",
    DATATYPE: "DATATYPE", WIDTH: "WIDTH", XY: "XY", ENDEL: "ENDEL",
    BOX: "BOX", BOXTYPE: "BOXTYPE", BGNEXTN: "BGNEXTN", ENDEXTN: "ENDEXTN",
}


# ---------------------------------------------------------------------------
# 2. Master (layer, datatype) -> material lookup table.
#
#    Source: SkyWater sky130 open-source PDK GDS layer conventions
#    (the same numbers KLayout's sky130A.lyp layer-properties file
#    uses). Confirmed against this exact file: every number below was
#    cross-checked by (a) the fact standard cells in this GDS are
#    literally named "sky130_fd_sc_hd__*", and (b) PATH-drawn wires
#    only appear on the met1-met5/li1 entries, which is exactly what
#    you'd expect for interconnect routing layers.
#
#    IMPORTANT LEARNING POINT: nothing in the GDS file itself proves
#    these names. GDS only stores bare (layer, datatype) integers --
#    the *meaning* is a convention shared between the tool that wrote
#    the file and whoever reads it later, defined by the PDK. If you
#    were handed a GDS from an unknown/undocumented process, you would
#    NOT be able to fully trust a table like this -- you'd have to
#    infer purpose from geometry (sizes, which layers touch which,
#    etc). A few rare entries below are marked "unverified" for
#    exactly this reason: they occur too rarely in this file to be
#    confident from context alone.
# ---------------------------------------------------------------------------
# Python mechanic: a dict's keys don't have to be strings or numbers --
# here every key is a TUPLE `(layer, datatype)`, e.g. `(64, 20)`. A tuple
# is like a list but immutable (can't be changed after creation), which is
# exactly why Python allows it as a dict key and doesn't allow a list
# (dict keys must never change, or lookups would break). So
# `LAYER_MATERIAL_MAP[(66, 20)]` looks up "which material is GDS layer 66,
# datatype 20?" and returns "poly".
LAYER_MATERIAL_MAP = {
    # --- front-end-of-line (transistor-level) layers ---
    (64, 20): "nwell",              # N-well tub (where PMOS transistors sit)
    (64, 16): "nwell.pin",          # nwell used as a labeled pin/terminal
    (65, 20): "diff",               # active/diffusion area (source/drain/etc)
    (65, 44): "tap",                # substrate/well tap (ties well to supply)
    (66, 20): "poly",               # polysilicon (transistor gates)
    (66, 44): "licon1",             # contact: poly/diff  -> li1 (local interconnect)
    (66, 15): "poly.model",         # rare poly sub-purpose (unverified)
    (93, 44): "nsdm",               # N-select implant mask (defines NMOS diffusion)
    (94, 20): "psdm",               # P-select implant mask (defines PMOS diffusion)
    (95, 20): "npc",                # nitride-poly-cut (protects poly at contacts)
    (78, 44): "hvtp",               # high-voltage/threshold implant marker

    # --- local interconnect + metal stack (back-end-of-line) ---
    (67, 20): "li1",                # local interconnect metal (licon1<->mcon layer)
    (67, 44): "mcon",               # contact: li1 -> met1
    (68, 20): "met1",
    (68, 44): "via",                # via1: met1 -> met2
    (69, 20): "met2",
    (69, 44): "via2",               # met2 -> met3
    (70, 20): "met3",
    (70, 44): "via3",               # met3 -> met4
    (71, 20): "met4",
    (71, 44): "via4",               # met4 -> met5
    (72, 20): "met5",               # top routing metal in this design
    (68, 16): "met1.pin",
    (67, 16): "li1.pin",

    # --- markers / non-physical "areaid" and boundary layers ---
    (81, 4): "areaid.standardc",    # sky130 marker: "this is a std-cell boundary"
    (81, 23): "areaid.unverified",  # rare sibling of 81/4 (unverified)
    (235, 4): "prBoundary",         # classic Cadence/Magic "cell abutment box" marker
    (236, 0): "boundary.unverified",  # seen once per std cell; exact purpose unverified
    (122, 16): "unverified.122_16",   # seen once per std cell; exact purpose unverified
    (200, 0): "unverified.200_0",     # very rare; exact purpose unverified
}


def material_for(layer, datatype):
    """Translate a raw (layer, datatype) pair into a human name.

    Falls back to a generic "LAYER<n>/DT<n>" label for anything not in
    our map, so unknown/未-mapped layers are still visible in the
    output instead of silently disappearing.
    """
    # `dict.get(key, default)` looks up `key`; if it's not found, instead
    # of raising an error (like `dict[key]` would) it just returns
    # `default`. That's exactly the "fall back to a generic label" logic
    # described above, in one line.
    #
    # `f"LAYER{layer}/DT{datatype}"` is an f-STRING (formatted string
    # literal) -- the `f` prefix means anything inside `{ }` is Python code
    # that gets evaluated and inserted into the string. So if layer=99 and
    # datatype=5, this produces the literal text "LAYER99/DT5". f-strings
    # are used everywhere in this file for building readable text.
    return LAYER_MATERIAL_MAP.get((layer, datatype), f"LAYER{layer}/DT{datatype}")


# ---------------------------------------------------------------------------
# 3. Low level: turn the raw file bytes into a stream of (rectype,
#    datatype, payload) records. This function knows NOTHING about
#    cells or polygons -- it only understands "how do I chop this file
#    into records", which is the lowest layer of the format.
# ---------------------------------------------------------------------------
def iter_records(path):
    """Walk a .gds file's raw bytes and hand back one record at a time.

    Python mechanic -- this is a GENERATOR, not a normal function. Notice
    it has `yield` instead of `return` below. Calling `iter_records(path)`
    does NOT immediately read the whole file and hand you a big list;
    instead it hands you a special "iterator" object that reads and
    produces ONE record only when something asks it for the next one
    (e.g. a `for rectype, dtype, payload in iter_records(path): ...` loop,
    used all over this file). That matters here because a .gds file can
    have thousands of records -- generating them lazily, one at a time,
    means we're never holding "a list of every record" in memory at once,
    just the current one.
    """
    # `open(path, "rb")` opens the file in "read binary" mode -- "rb" means
    # we get back raw `bytes` (numbers 0-255), not decoded text. GDS is a
    # binary format, so this is required; opening it as text would corrupt
    # the data. `with ... as f:` is a "context manager": it guarantees the
    # file gets closed automatically when this block ends, even if an
    # error happens inside it -- you never have to remember to call
    # `f.close()` yourself.
    with open(path, "rb") as f:
        data = f.read()  # `data` now holds the ENTIRE file as one `bytes` object

    i = 0            # `i` is our "read cursor" -- which byte we're currently at
    n = len(data)    # total number of bytes in the file
    while i < n:     # keep going until we've consumed the whole file
        # The length field includes itself + the type/datatype bytes,
        # so a record with no payload at all still has length == 4.
        if i + 4 > n:
            raise ValueError(f"truncated record header at byte {i}")

        # `data[i:i+2]` is Python SLICING: it grabs a sub-range of bytes
        # without needing a loop -- "give me bytes from index i up to (but
        # not including) i+2". `struct.unpack(fmt, bytes)` then interprets
        # those raw bytes as a number according to `fmt`:
        #   ">"  = big-endian byte order (most-significant byte first --
        #          this is what the GDSII spec mandates for every field)
        #   "H"  = unsigned 16-bit integer (2 bytes)
        # `struct.unpack` always returns a TUPLE (even for one value), so
        # `[0]` pulls the single number back out of that one-element tuple.
        length = struct.unpack(">H", data[i:i + 2])[0]

        if length == 0:
            # A zero-length record is a padding artifact some tools
            # leave at the very end of the file. Nothing more to read.
            break

        # Indexing a `bytes` object with a single index (no colon) gives
        # back a plain Python int for that one byte (0-255) -- no unpack
        # needed since a single byte IS already just a small integer.
        rectype = data[i + 2]
        datatype = data[i + 3]
        # The payload is "everything after the 4-byte header, up to the
        # end of this record" -- another slice.
        payload = data[i + 4:i + length]

        # `yield` is what makes this function a generator (see the
        # docstring above): execution PAUSES here and hands
        # `(rectype, datatype, payload)` back to whoever is looping over
        # us. Next time they ask for another value, execution resumes
        # right after this line -- picking up with `i += length` below --
        # rather than starting the function over from the top.
        yield rectype, datatype, payload
        i += length  # advance the cursor past this record, to the next one


# ---------------------------------------------------------------------------
# 4. Payload decoders. Only the data types this parser actually needs.
# ---------------------------------------------------------------------------
def decode_ascii(payload):
    """GDS ASCII strings are padded to an even length with a trailing
    0x00 byte when the real string has odd length -- strip it off."""
    # `payload` here is a `bytes` object (raw numbers), not yet text.
    # `.rstrip(b"\x00")` removes trailing NUL bytes (the `b"..."` prefix
    # means "this is a bytes literal, not a normal text string"; `\x00` is
    # the null byte). `.decode("ascii", ...)` then converts the remaining
    # bytes into an actual Python `str` (text) you can print/compare/etc.
    # `errors="replace"` means "if some byte isn't valid ASCII, substitute
    # a placeholder character instead of crashing" -- a defensive choice
    # since we're trusting a file we didn't write.
    return payload.rstrip(b"\x00").decode("ascii", errors="replace")


def decode_int16(payload):
    # LAYER and DATATYPE are always exactly one 2-byte signed int.
    # "h" (lowercase) in a struct format string = SIGNED 16-bit int,
    # as opposed to "H" (uppercase, seen in iter_records above) which is
    # UNSIGNED. Signed matters here because some GDS int16 fields
    # (like ANGLE-adjacent flags) are allowed to be negative.
    return struct.unpack(">h", payload)[0]


def decode_xy(payload):
    """Decode an XY record's payload into a list of [x, y] int pairs.

    XY payloads are a flat run of big-endian int32s, always an even
    count (each point is 2 of them). Used for polygon vertices, path
    centerline points, and SREF/AREF placement points alike.
    """
    # Each coordinate is a 4-byte ("i" = signed 32-bit int) value, and
    # they come in x,y pairs back to back: x0,y0,x1,y1,x2,y2,... So the
    # total COUNT of individual numbers is (number of payload bytes / 4).
    n = len(payload) // 4   # `//` is INTEGER (floor) division: 7 // 2 == 3, not 3.5

    # `f">{n}i"` builds a struct format string like ">12i" (meaning "12
    # big-endian signed 32-bit ints in a row") by inserting `n` into an
    # f-string. struct.unpack then returns all `n` numbers at once, as one
    # flat tuple: (x0, y0, x1, y1, x2, y2, ...).
    flat = struct.unpack(f">{n}i", payload)

    # This line is a LIST COMPREHENSION -- Python's compact way to build a
    # new list by looping. The long way to write the same thing would be:
    #
    #   result = []
    #   for i in range(0, n, 2):
    #       result.append([flat[i], flat[i + 1]])
    #   return result
    #
    # `range(0, n, 2)` counts 0, 2, 4, 6, ... up to (not including) n --
    # i.e. every EVEN index, which is exactly where each x (not y) sits in
    # the flat list. For each such `i`, `[flat[i], flat[i+1]]` pairs that x
    # with the y right after it, producing one [x, y] point. The list
    # comprehension `[EXPR for i in ITERABLE]` does the loop-and-append in
    # one line and is the idiomatic Python style you'll see throughout
    # this codebase.
    return [[flat[i], flat[i + 1]] for i in range(0, n, 2)]


def decode_real8(payload):
    """Decode GDSII's oddball 8-byte floating point format.

    This is one of the weirder corners of the spec, worth understanding
    once: it is NOT IEEE-754. It's an "excess-64, base-16" float from
    old IBM mainframes:

        byte 0        : bit 7 = sign, bits 0-6 = exponent (excess-64,
                         base 16 -- i.e. the real exponent is
                         (byte0 & 0x7F) - 64, and the value is scaled
                         by 16^exponent, not 2^exponent)
        bytes 1-7      : 56-bit unsigned mantissa fraction (value in
                         [0, 1)), read as a big-endian integer over 2^56

        value = (-1)^sign * (mantissa / 2**56) * 16**(exponent)

    We only use this to decode the UNITS record for a nice printout;
    it never affects layer/cell grouping.
    """
    b0 = payload[0]  # first byte, as a plain int (0-255) -- see the indexing note in iter_records above

    # `X if CONDITION else Y` is Python's TERNARY expression -- an inline
    # if/else that evaluates to a single value. Read as: "sign is -1.0 if
    # the condition is true, otherwise 1.0".
    #
    # `b0 & 0x80` is a BITWISE AND: it checks whether bit 7 (the top bit)
    # of b0 is set, by masking with the binary pattern 10000000 (0x80 in
    # hex). Any nonzero result is "truthy" in Python (treated as True in
    # an `if`), so this reads as "is the sign bit on?".
    sign = -1.0 if (b0 & 0x80) else 1.0

    # `b0 & 0x7F` masks OFF the sign bit, keeping only the lower 7 bits
    # (the exponent). `**` is Python's exponent/power operator (like ^ in
    # some other languages) -- used again a few lines down.
    exponent = (b0 & 0x7F) - 64

    # `int.from_bytes(some_bytes, "big")` reads a run of raw bytes as one
    # big unsigned integer, most-significant byte first ("big" = big-endian,
    # same byte order as everywhere else in GDS). `payload[1:8]` slices out
    # bytes 1 through 7 (7 bytes total -- the mantissa).
    mantissa = int.from_bytes(payload[1:8], "big")
    return sign * (mantissa / (2 ** 56)) * (16.0 ** exponent)


# ---------------------------------------------------------------------------
# 5. High-level walk: turn the flat record stream into
#    { cell_name: Counter(material_name -> polygon_count) }
#
#    We do this with a tiny bit of state, since GDS gives us no tree --
#    just "start" and "end" markers we must pair up ourselves:
#      - which structure (cell) are we currently inside?
#      - which element are we currently inside, and what LAYER/DATATYPE
#        has it declared so far?
# ---------------------------------------------------------------------------
def parse_gds_cells(path):
    """Parse a .gds file and return (cells, meta).

    cells: dict of {cell_name: Counter({material_name: polygon_count})}
           one entry per BOUNDARY (and BOX) element found, grouped by
           the cell (structure) that contains it.
    meta:  small dict of library-level info (units, header version)
           just for a friendlier printout.
    """
    # `defaultdict(Counter)` -- see the import comment at the top of the
    # file. Reading `cells["some_new_name"]` for the very first time
    # doesn't crash; it silently creates an empty `Counter()` for that key
    # and returns it. That's what lets `cells[current_cell][material] += 1`
    # below "just work" the first time a given cell/material combo shows
    # up, with no "if not in dict, initialize to 0" boilerplate.
    cells = defaultdict(Counter)

    # These "current_*" variables are our hand-rolled STATE MACHINE. GDS
    # is a flat stream of records with no tree/nesting built in (see the
    # big docstring at the top of the file), so as we read one record at
    # a time we have to remember "what have we seen so far, that we
    # haven't closed out yet?" -- e.g. "we're currently inside cell X,
    # inside a BOUNDARY element, which has declared LAYER=66 so far but no
    # DATATYPE yet." Every one of these gets read, written, and reset as
    # we walk through BGNSTR/STRNAME/ENDSTR and element-start/ENDEL pairs
    # below. `None` is Python's "no value yet" placeholder.
    current_cell = None       # name of the structure we're inside, or None
    current_element = None    # record type of the element we're inside
    current_layer = None
    current_datatype = None
    meta = {}

    # `for a, b, c in some_generator:` -- this loop calls iter_records()
    # once, gets back the generator described above, and on each pass
    # pulls the next `(rectype, dtype_code, payload)` TUPLE it yields,
    # automatically unpacking the three values into three separate loop
    # variables. This single loop is the entire "outer shell" of the
    # parser -- everything else is just deciding what to do for each
    # record type.
    for rectype, dtype_code, payload in iter_records(path):
        # `dtype_code` here is the record HEADER's data-type byte (int16
        # vs ascii vs real8, etc. -- see section 1's comment block). It
        # is a totally different concept from the GDS *DATATYPE record*
        # (rectype == DATATYPE below), which is the polygon's layer
        # sub-classification. Same word, two unrelated meanings -- a
        # classic GDS gotcha. We only need it for one sanity check:
        # confirming LAYER/DATATYPE records really are encoded as the
        # int16 we assume in decode_int16().
        #
        # `rectype in (LAYER, DATATYPE)` checks membership in a TUPLE the
        # same way `in` checked membership in the ELEMENT_START_TYPES set
        # earlier -- "is rectype equal to either of these two values?"
        # `assert CONDITION, message` is a built-in sanity check: if
        # CONDITION is false, Python immediately stops the program and
        # raises an error containing `message`. It's a way of saying "this
        # should NEVER happen if my understanding of the file format is
        # correct -- if it does, I'd rather crash loudly here than
        # silently produce wrong data later."
        if rectype in (LAYER, DATATYPE):
            assert dtype_code == 0x02, (
                f"expected int16 (data type 0x02) for record "
                f"{RECORD_NAMES.get(rectype, rectype)}, got 0x{dtype_code:02x}"
            )

        # This long if/elif/elif/... chain is the heart of the state
        # machine: "based on which record type we just read, do the one
        # right thing." Python has no switch/case statement (older
        # versions didn't, anyway) -- a chained if/elif is the traditional
        # way to express "pick exactly one of these branches."
        if rectype == HEADER:
            meta["gds_version"] = decode_int16(payload)

        elif rectype == UNITS:
            # payload is two REAL8 values back to back: user-units-per-
            # database-unit, then meters-per-database-unit.
            user_units = decode_real8(payload[0:8])
            meters_per_dbunit = decode_real8(payload[8:16])
            meta["user_units_per_dbunit"] = user_units
            meta["meters_per_dbunit"] = meters_per_dbunit

        elif rectype == BGNSTR:
            # A new structure (cell) is starting. Its name arrives in
            # the NEXT record (STRNAME), so just clear our tracker.
            current_cell = None

        elif rectype == STRNAME:
            current_cell = decode_ascii(payload)
            # Touch the dict so even an empty cell shows up in results.
            # This line looks like it does nothing (the looked-up value
            # isn't stored anywhere!) -- but the mere act of reading
            # `cells[current_cell]` on a defaultdict is enough to trigger
            # its "create an empty Counter for this key if missing"
            # behavior. Without this line, a cell with zero BOUNDARY/BOX
            # elements would never appear in `cells` at all, since nothing
            # would ever otherwise touch that key.
            cells[current_cell]  # noqa: B018 (defaultdict touch is intentional)

        elif rectype == ENDSTR:
            current_cell = None

        elif rectype in ELEMENT_START_TYPES:
            # We only *care* about BOUNDARY/BOX (real polygons), but we
            # still track SREF/AREF/PATH/TEXT starts so that ENDEL
            # pairs up correctly and we don't misattribute a stray
            # LAYER record from one of those to the wrong element.
            current_element = rectype
            current_layer = None
            current_datatype = None

        elif rectype == LAYER:
            current_layer = decode_int16(payload)

        elif rectype == DATATYPE:
            current_datatype = decode_int16(payload)

        elif rectype == ENDEL:
            # `current_element in (BOUNDARY, BOX)` -- "was the element we
            # just finished a BOUNDARY or a BOX?" `and current_cell is not
            # None` -- "and are we actually inside some named cell right
            # now?" (both need to be true, `and` short-circuits: if the
            # first half is false, Python never bothers checking the
            # second). Comparing to `None` with `is not` (rather than
            # `!=`) is the Python idiom for "identity check against the
            # singleton None" -- always prefer it over `!= None`.
            if current_element in (BOUNDARY, BOX) and current_cell is not None:
                material = material_for(current_layer, current_datatype)
                # `cells[current_cell]` is a Counter (see above); indexing
                # it with `[material]` and adding 1 works even the first
                # time a given material is seen for this cell, because
                # Counter (like defaultdict) treats a missing key as 0.
                cells[current_cell][material] += 1
            current_element = None
            current_layer = None
            current_datatype = None

        # Any other record type (XY, WIDTH, PATHTYPE, STRANS, SNAME,
        # COLROW, TEXTTYPE, PRESENTATION, STRING, MAG, ANGLE, ENDLIB,
        # BGNEXTN/ENDEXTN, ...) doesn't affect cell/polygon grouping
        # for this first version, so we deliberately ignore it here.
        # (Notice there's no final `else:` -- if none of the branches
        # above match, Python just falls through and does nothing, which
        # is exactly the "ignore it" behavior we want.)

    # Functions can return more than one value by just separating them
    # with a comma -- Python packs them into a tuple automatically. The
    # caller then unpacks them right back out, e.g.
    # `cells, meta = parse_gds_cells(path)` further down in this file.
    return cells, meta


# ---------------------------------------------------------------------------
# 5b. Full-geometry walk: unlike parse_gds_cells() above (which only keeps
#     a per-material *count*), this keeps the actual polygon/path vertex
#     coordinates, AND -- crucially -- every individual SREF placement
#     instead of collapsing all placements of the same structure into one
#     entry. A via cell like VIA_M1M2_PR is defined ONCE as a structure,
#     but gets *instantiated* (SREF'd) hundreds of times at different XY
#     spots; parse_gds_cells can't tell those placements apart, but a
#     downstream connectivity/overlap analysis needs each one separately.
# ---------------------------------------------------------------------------
def parse_gds_full(path):
    """Parse a .gds file into full per-structure geometry.

    Returns (structures, instances, meta):

      structures: {struct_name: {"polygons": [...], "paths": [...], "labels": [...]}}
        polygon: {"layer", "datatype", "material", "xy": [[x, y], ...]}
        path:    {"layer", "datatype", "material", "width", "pathtype",
                   "xy": [[x, y], ...]}   (xy = centerline points)
        label:   {"layer", "text", "xy": [x, y]}   -- a GDS TEXT element.
                  This is the ONLY place a real pin/net/port name shows up
                  anywhere in a GDS file (e.g. "VPWR", "VGND", "A", "Y",
                  "CLK") -- everything else is bare geometry. Standard
                  cells carry one of these per pin, sitting on that pin's
                  actual layer; the top cell carries one per external
                  circuit port.
        These are in the structure's own LOCAL coordinate system, exactly
        as drawn in its BGNSTR/ENDSTR block -- no placement transform
        applied yet.

      instances: {container_struct_name: [instance, ...]}
        One entry per individual SREF/AREF found, keyed by the structure
        it was placed INSIDE (usually just the top cell, but this stays
        correct for deeper hierarchies too). Each instance:
          {"kind": "sref"|"aref", "ref": <structure name>, "pos": [x, y],
           "angle": deg, "mag": float, "mirror": bool,
           # aref only:
           "cols": int, "rows": int, "xy2": [x,y], "xy3": [x,y]}
        pos/xy2/xy3 are the raw SREF/AREF XY point(s) -- the placement is
        not yet resolved to absolute polygon coordinates, so this is the
        full list of *instantiations*, not a flattened shape list.

      meta: same library-level info as parse_gds_cells().
    """
    # `{}` is an empty dict literal; `[]` (used below) is an empty list
    # literal. `structures` will map cell name -> that cell's geometry;
    # unlike `cells` in parse_gds_cells (a defaultdict), here we build
    # each entry explicitly with `.setdefault(...)` a few lines down, so a
    # plain `{}` is enough to start.
    structures = {}
    instances = defaultdict(list)  # container name -> list of instances placed inside it
    meta = {}

    # Same state-machine idea as parse_gds_cells above, just tracking more
    # fields because we now care about full geometry (widths, transforms,
    # text) and not just counts.
    current_cell = None
    current_element = None
    current_layer = None
    current_datatype = None
    current_width = None
    current_pathtype = None
    current_xy = None
    current_sname = None
    current_mirror = False
    current_angle = 0.0
    current_mag = 1.0
    current_colrow = None
    current_string = None

    for rectype, dtype_code, payload in iter_records(path):
        if rectype == HEADER:
            meta["gds_version"] = decode_int16(payload)

        elif rectype == UNITS:
            meta["user_units_per_dbunit"] = decode_real8(payload[0:8])
            meta["meters_per_dbunit"] = decode_real8(payload[8:16])

        elif rectype == BGNSTR:
            current_cell = None

        elif rectype == STRNAME:
            current_cell = decode_ascii(payload)
            # `dict.setdefault(key, default)`: "if `key` is already in the
            # dict, leave it alone and return its current value; if not,
            # set it to `default` AND return that." We don't use the
            # return value here, only the side effect: make sure this
            # cell has an empty polygons/paths/labels record waiting for
            # it, without ever accidentally WIPING OUT one that was
            # already partially filled in (which a plain
            # `structures[current_cell] = {...}` would risk doing, since
            # some structures get visited more than once across a file).
            structures.setdefault(current_cell, {"polygons": [], "paths": [], "labels": []})

        elif rectype == ENDSTR:
            current_cell = None

        elif rectype in ELEMENT_START_TYPES:
            # A new element is starting -- reset every "have we seen this
            # yet" tracker back to its default. This matters because,
            # say, a PATH element never sends a MAG/ANGLE record, so
            # without this reset, `current_mag` could still be holding a
            # value left over from some earlier, unrelated SREF.
            current_element = rectype
            current_layer = None
            current_datatype = None
            current_width = None
            current_pathtype = None
            current_xy = None
            current_sname = None
            current_mirror = False
            current_angle = 0.0
            current_mag = 1.0
            current_colrow = None
            current_string = None

        elif rectype == LAYER:
            current_layer = decode_int16(payload)

        elif rectype == DATATYPE:
            current_datatype = decode_int16(payload)

        elif rectype == WIDTH:
            # "i" = signed 32-bit int (see decode_xy above for the same
            # format code) -- a PATH's width is one plain 4-byte number,
            # not worth a whole helper function like decode_int16/decode_xy.
            current_width = struct.unpack(">i", payload)[0]

        elif rectype == PATHTYPE:
            current_pathtype = decode_int16(payload)

        elif rectype == XY:
            current_xy = decode_xy(payload)

        elif rectype == SNAME:
            current_sname = decode_ascii(payload)

        elif rectype == STRANS:
            # bit 15 (0x8000) of the flag word = mirror about X axis,
            # applied BEFORE angle/mag, per the GDSII spec.
            flag = struct.unpack(">H", payload)[0]
            # `flag & 0x8000` (bitwise AND, same trick as decode_real8's
            # sign bit above) isolates just that one bit; `bool(...)`
            # converts the result to an actual True/False instead of a
            # raw integer that merely behaves that way.
            current_mirror = bool(flag & 0x8000)

        elif rectype == MAG:
            current_mag = decode_real8(payload)

        elif rectype == ANGLE:
            current_angle = decode_real8(payload)

        elif rectype == COLROW:
            # "hh" = two signed 16-bit ints back to back (columns, then
            # rows) -- struct.unpack still returns a tuple even with two
            # format codes, so `current_colrow` becomes `(cols, rows)`.
            current_colrow = struct.unpack(">hh", payload)

        elif rectype == STRING:
            current_string = decode_ascii(payload)

        elif rectype == ENDEL:
            # We've now seen every record belonging to one element (its
            # start marker, LAYER, XY, etc.) and hit its closing ENDEL --
            # time to package up whatever we collected into a permanent
            # record and file it away under the right structure/kind.
            #
            # `X or DEFAULT` is a common Python idiom for "use X, unless
            # it's falsy (None, empty list, 0, ''...), in which case use
            # DEFAULT instead." `current_xy or []` means: "use the XY
            # points we read, or an empty list if somehow no XY record
            # ever arrived" -- a defensive fallback so a malformed/unusual
            # element can't crash the parser with a `None` where a list
            # was expected.
            if current_element in (BOUNDARY, BOX) and current_cell is not None:
                material = material_for(current_layer, current_datatype)
                # `{"key": value, ...}` here is a dict LITERAL describing
                # one polygon record; `list.append(...)` adds it to the
                # end of this structure's "polygons" list. Every
                # polygon/path/label/instance in this file is built the
                # same way: gather the loose current_* state into one
                # dict, append it to the right list.
                structures[current_cell]["polygons"].append({
                    "layer": current_layer,
                    "datatype": current_datatype,
                    "material": material,
                    "xy": current_xy or [],
                })

            elif current_element == TEXT and current_cell is not None:
                structures[current_cell]["labels"].append({
                    "layer": current_layer,
                    "text": current_string,
                    # A TEXT element's XY payload is technically a list of
                    # points, but a text label only ever has exactly one
                    # position. `(current_xy or [[0, 0]])[0]` falls back
                    # to a single default point `[0, 0]` if XY is somehow
                    # missing, then takes index `[0]` -- "the first (and
                    # only) point" -- either way.
                    "xy": (current_xy or [[0, 0]])[0],
                })

            elif current_element == PATH and current_cell is not None:
                material = material_for(current_layer, current_datatype)
                structures[current_cell]["paths"].append({
                    "layer": current_layer,
                    "datatype": current_datatype,
                    "material": material,
                    "width": current_width or 0,
                    "pathtype": current_pathtype or 0,
                    "xy": current_xy or [],
                })

            elif current_element == SREF and current_cell is not None:
                instances[current_cell].append({
                    "kind": "sref",
                    "ref": current_sname,
                    "pos": (current_xy or [[0, 0]])[0],
                    "angle": current_angle,
                    "mag": current_mag,
                    "mirror": current_mirror,
                })

            elif current_element == AREF and current_cell is not None:
                # An AREF (array reference) carries THREE XY points
                # instead of one: the placement origin, plus two more
                # points that define the array's column/row spacing. This
                # design has zero AREFs (confirmed while building this
                # tool), so this branch is defensive completeness rather
                # than something actually exercised here.
                pts = current_xy or [[0, 0], [0, 0], [0, 0]]
                # Unpacking a tuple straight into two names at once:
                # `a, b = some_tuple` is shorthand for `a = some_tuple[0]`
                # + `b = some_tuple[1]`, and only works if the right-hand
                # side has exactly as many items as names on the left.
                cols, rows = current_colrow or (1, 1)
                instances[current_cell].append({
                    "kind": "aref",
                    "ref": current_sname,
                    "pos": pts[0],
                    # `X if CONDITION else Y` ternary again: use the real
                    # second/third point if it exists, otherwise just
                    # reuse the first point as a harmless fallback.
                    "xy2": pts[1] if len(pts) > 1 else pts[0],
                    "xy3": pts[2] if len(pts) > 2 else pts[0],
                    "cols": cols,
                    "rows": rows,
                    "angle": current_angle,
                    "mag": current_mag,
                    "mirror": current_mirror,
                })

            current_element = None
            current_layer = None
            current_datatype = None

    # `instances` was built as a `defaultdict(list)` (see near the top of
    # this function) so that `instances[current_cell].append(...)` above
    # would "just work" the first time any given container name was seen.
    # But defaultdicts have one sharp edge: if OTHER code later looks up a
    # key that was never actually written (e.g. `instances["typo"]`),
    # it'll silently create a new empty list instead of raising a
    # `KeyError` you'd notice. Wrapping it in `dict(...)` here converts it
    # to a plain, ordinary dict before handing it back to the caller, so
    # that kind of silent-typo bug can't happen downstream in netGraph.py.
    return structures, dict(instances), meta


# ---------------------------------------------------------------------------
# 6. CLI entry point / pretty printer.
# ---------------------------------------------------------------------------
def main():
    # `__file__` is a special variable Python fills in automatically: the
    # path to THIS source file (gdsParser.py). `os.path.abspath(...)`
    # turns it into a full, unambiguous path (resolving any ".." or
    # relative parts); `os.path.dirname(...)` then strips the filename
    # off, leaving just the folder this script lives in. `here` is that
    # folder, used below so the default input/output file paths work no
    # matter what directory you happen to run the script FROM.
    here = os.path.dirname(os.path.abspath(__file__))
    # `os.path.join(a, b)` glues a folder and filename together with the
    # correct separator for whatever OS this runs on ("/" on Mac/Linux,
    # "\" on Windows) -- always prefer it over manually writing
    default_gds = os.path.join(here, "puzzle.gds")
    default_json = os.path.join(here, "puzzleNetlist.json")

    # `argparse` (imported at the top of the file) is Python's standard
    # library for building a command-line interface: turning things typed
    # after `python3 gdsParser.py` (like `--top 5` or a file path) into
    # normal Python values you can use. `ArgumentParser(...)` creates the
    # parser object; `description=` is the text shown when someone runs
    # `python3 gdsParser.py --help`.
    parser = argparse.ArgumentParser(
        description="Parse a GDSII file's cells (structures) and report "
                     "the polygon (BOUNDARY) material types found in each, "
                     "using the sky130 PDK layer/datatype convention."
    )
    # Each `parser.add_argument(...)` call declares one flag/argument.
    # This first one has no leading dashes ("gds_file", not "--gds_file"),
    # which makes it a POSITIONAL argument -- something you can type
    # directly, e.g. `python3 gdsParser.py somefile.gds`, without naming
    # the flag. `nargs="?"` means "this is optional, zero or one value";
    # `default=default_gds` says "if the user doesn't type one, use our
    # 04_final.gds path instead."
    parser.add_argument(
        "gds_file", nargs="?", default=default_gds,
        help=f"path to a .gds file (default: {default_gds})",
    )
    # A leading "--" makes this an OPTIONAL flag, e.g.
    # `--json somewhere/else.json`. `metavar` only affects how it's shown
    # in --help text, purely cosmetic.
    # `--out` is accepted as an alias so every stage of the pipeline takes
    # its output path under the same flag name (netGraph.py, moduleGraph.py
    # and the rest all use --out); `--json` stays for existing invocations.
    parser.add_argument(
        "--json", "--out", metavar="OUT.json", default=default_json,
        help="write full per-structure geometry (polygons/paths with XY) "
             f"and every individual instantiation as JSON (default: {default_json})",
    )
    # `action="store_true"` makes this a boolean ON/OFF SWITCH rather than
    # a flag that takes a value: typing `--no-json` sets
    # `args.no_json = True`; not typing it at all leaves it `False`. No
    # `=something` needed after it on the command line.
    parser.add_argument(
        "--no-json", action="store_true",
        help="skip writing the JSON file, just print the summary",
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="only print the N cells with the most polygons (default: all)",
    )
    # This is where argparse actually reads `sys.argv` (the real
    # command-line text the user typed) and produces `args`, an object
    # where each declared argument is available as an attribute --
    # `args.gds_file`, `args.json`, `args.no_json`, `args.top`.
    args = parser.parse_args()

    cells, meta = parse_gds_cells(args.gds_file)

    print(f"Parsed: {args.gds_file}")
    # `"gds_version" in meta` checks dict membership: "does this key
    # exist?" (distinct from checking a value -- it's fine even if the
    # associated value were 0 or empty). This guards against `meta` not
    # having that key at all if, say, the file had no HEADER record.
    if "gds_version" in meta:
        print(f"  GDSII version: {meta['gds_version']}")
    if "meters_per_dbunit" in meta:
        # `{value:.3e}` inside an f-string is a FORMAT SPEC: `.3e` means
        # "scientific notation, 3 digits after the decimal point", e.g.
        # 1.000e-09. Format specs like this control how a value is
        # rendered as text without needing separate formatting code.
        print(f"  1 database unit = {meta['meters_per_dbunit']:.3e} meters")
    print(f"  Structures (cells) found: {len(cells)}")
    # `sum(sum(c.values()) for c in cells.values())` is two nested
    # summations using a GENERATOR EXPRESSION -- like a list comprehension
    # (see decode_xy above) but with `()` instead of `[]`, which makes it
    # lazy (values are produced one at a time, not all built into a list
    # first). Reading inside-out: `cells.values()` gives every Counter in
    # the dict (one per cell); for each Counter `c`, `c.values()` gives
    # its material counts, and the INNER `sum()` adds those up into "total
    # polygons in this one cell"; the OUTER `sum()` then adds those
    # per-cell totals into one grand total.
    total_polys = sum(sum(c.values()) for c in cells.values())
    print(f"  Total BOUNDARY/BOX polygons found: {total_polys}")
    print()

    # Sort cells by polygon count, most first -- makes it easy to spot
    # the "real" standard cells vs. tiny via/marker structures.
    #
    # `sorted(iterable, key=..., reverse=...)` returns a NEW sorted list
    # without changing the original. `cells.items()` gives
    # `(cell_name, material_counts)` pairs. `key=lambda kv: ...` tells
    # `sorted` HOW to compare items: a `lambda` is a tiny, throwaway,
    # unnamed function -- `lambda kv: sum(kv[1].values())` is shorthand
    # for a function that takes one argument `kv` (a `(name, counts)`
    # pair) and returns the total polygon count for that cell. `sorted`
    # then ranks every item by that computed number. `reverse=True` flips
    # it to descending (biggest first) instead of the default ascending.
    ordered = sorted(cells.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
    if args.top is not None:
        # `list[:N]` is a SLICE with no start -- "everything from the
        # beginning up to (not including) index N" -- i.e. "the first N
        # items."
        ordered = ordered[: args.top]

    for cell_name, material_counts in ordered:
        total = sum(material_counts.values())
        print(f"{cell_name}  ({total} polygons)")
        # Same sorted()+lambda+key pattern as above, but sorting by
        # NEGATIVE count (`-kv[1]`) instead of passing `reverse=True` --
        # both achieve "largest first," this is just an alternate style
        # you'll see used interchangeably.
        for material, count in sorted(material_counts.items(), key=lambda kv: -kv[1]):
            # `{material:<24}` is another format spec: `<24` means
            # "left-align this text, pad with spaces to at least 24
            # characters wide" -- purely so the printed columns line up
            # neatly regardless of how long each material name is.
            print(f"    {material:<24} {count}")
        print()

    if not args.no_json:
        # Full-geometry pass: every polygon/path with real XY coordinates,
        # plus every individual SREF/AREF instantiation (not collapsed
        # down to "this structure exists"), keyed by which structure they
        # were placed inside.
        structures, instances, full_meta = parse_gds_full(args.gds_file)
        # `instances` maps container name -> list of instances placed in
        # it. `instances.values()` gives just the lists (discarding the
        # container names); `len(v)` counts each list; the generator
        # expression + `sum()` combo (same pattern as `total_polys`
        # above) adds all those per-container counts into one grand total
        # of "how many SREF/AREF placements did we find, everywhere?"
        num_instances = sum(len(v) for v in instances.values())
        # `open(path, "w")` opens (creating if needed) a file for writing
        # TEXT ("w" = write mode, as opposed to "rb"/binary used to read
        # the .gds file back in iter_records). Again a `with...as` context
        # manager, so the file is guaranteed to be closed/flushed to disk
        # once this block finishes.
        with open(args.json, "w") as f:
            # `json.dump(obj, file, indent=2)` converts a Python
            # dict/list/etc. into JSON text and writes it straight to
            # `f`. `indent=2` makes the output human-readable (nested,
            # indented) rather than one giant unbroken line -- purely
            # cosmetic, doesn't change what data is stored.
            json.dump(
                {"meta": full_meta, "structures": structures, "instances": instances},
                f, indent=2,
            )
        print(f"Wrote {len(structures)} structures and {num_instances} "
              f"individual instantiations to {args.json}")


# This is Python's standard "only run main() if this file was executed
# directly" guard. `__name__` is another special variable, automatically
# set to `"__main__"` when you run `python3 gdsParser.py`, but set to the
# module's own name (`"gdsParser"`) if some OTHER script instead does
# `import gdsParser` to reuse its functions (exactly what netGraph.py's
# test/debug snippets did during this session). Without this guard, simply
# importing the file would immediately re-run the whole CLI, print output,
# and overwrite OUT.json -- not what you want when you just want to borrow
# a function like `parse_gds_full`.
if __name__ == "__main__":
    main()
