"""
netGraph.py -- reconstruct electrical connectivity from OUT.json's layout
geometry, using a union-find (disjoint-set) over every conductive shape in
the flattened design.

=====================================================================
WHY THIS EXISTS
=====================================================================
gdsParser.py --json OUT.json dumps the RAW layout: every structure's
polygons/paths in their own local coordinates, plus every individual
SREF instantiation (position + rotation/mirror) that places those
structures into the top cell. Nothing in that file says which shapes
are electrically the same net -- GDS has no concept of "net" at all,
only geometry.

Two shapes are electrically connected if they PHYSICALLY TOUCH in a
way that's actually a wire, not just abutting silicon:
  - two shapes on the SAME interconnect layer (li1, met1..met5) whose
    footprints overlap -- it's literally one continuous piece of metal,
    or
  - a contact/via shape (mcon, via, via2, via3, via4) whose footprint
    overlaps a shape on either of the two layers it's allowed to bridge
    (e.g. a `via` shape only ever bridges met1<->met2, never met1<->met3).

Deliberately NOT included: poly/diff/tap. Those transistor-level layers
in sky130 are drawn as continuous strips under whole rows of series
transistors, so "touching" there means "physically abutting silicon,"
not "same electrical net" -- telling those apart needs real device
recognition (true LVS territory), which is out of scope here. See
CONDUCTOR_CANON below for the full reasoning.

That's a union-find problem: every shape starts in its own singleton
set, and every touching/bridging pair gets union()'d. Whatever set a
shape ends up in at the end IS a net. Overlap is tested with exact
polygon-polygon intersection (not just bounding boxes) -- sky130
routing polygons are often L/T-shaped, and bbox-only overlap produces
false connections that snowball through the union-find into one
giant net. This is still a deliberately simplified, no-SPICE version
of what real LVS ("layout vs schematic") connectivity extraction does.

=====================================================================
PIPELINE
=====================================================================
1. FLATTEN: OUT.json only has local-coordinate geometry + placement
   transforms. Walk every instance placed in a container structure
   (in this design, only "adder_demo" places anything) and transform
   its referenced structure's polygons/path-segments into absolute
   coordinates: mirror about X -> scale by mag -> rotate by angle ->
   translate by pos. Each result is one "shape": a material + an
   absolute bounding box + which instance it came from.

2. BUCKET: comparing every shape against every other shape is
   O(n^2) -- tens of thousands of shapes here, so hundreds of
   millions of pairs. Instead, bucket shapes into a spatial grid,
   split further by which "connectivity group" (canonical layer, see
   CONDUCTOR_CANON/CONTACT_LINKS below) they could possibly connect
   to. Only shapes sharing a bucket are ever compared.

3. UNION: within each bucket, union() any pair whose bounding boxes
   actually overlap AND whose materials are allowed to connect.

4. REPORT: find() every shape to get its net's root id -- that
   shape_id -> root_id mapping (a "long map": one entry per shape,
   thousands of them) is the parent_map in NET_GRAPH.json. Shapes are
   then grouped by root into `nets`, and a bipartite `cell_graph`
   (instance -> which net ids it touches) is derived from that -- the
   "graph of each cell": what a given standard-cell/via instance is
   wired to.
=====================================================================
"""

import argparse    # command-line flag parsing -- see gdsParser.py's main() for a full walkthrough
import json        # read OUT.json, write NET_GRAPH.json
import math        # trig (cos/sin/radians) for rotating placed shapes, hypot() for vector length
import os          # building default file paths (see gdsParser.py's `here = os.path.dirname(...)`)
import time        # time.time() -- timestamps used only to print "this step took N seconds"
from collections import defaultdict
    # defaultdict: see the detailed explanation in gdsParser.py's imports.
    # Used constantly below as "a dict where a missing key auto-creates an
    # empty list/set instead of crashing" -- e.g. `defaultdict(list)`,
    # `defaultdict(set)`.

# ---------------------------------------------------------------------------
# 1. Union-Find (disjoint set), path compression + union by rank.
# ---------------------------------------------------------------------------
# Python mechanics -- CLASSES, briefly, since this is the only class in
# either file: a `class` is a blueprint for creating objects that bundle
# together some data (here: `self.parent` and `self.rank`, two lists) with
# functions that operate on that data (here: `find` and `union`). Every
# function defined inside a class automatically receives the specific
# object it was called on as its first argument, conventionally named
# `self` -- so `dsu.find(5)` behind the scenes calls `find(dsu, 5)`,
# letting the function reach back into `dsu`'s own `self.parent`/
# `self.rank` lists. `__init__` is a special method Python calls
# automatically when you write `UnionFind(n)` -- it's where you set up an
# object's starting state (its lists), which is why it's often called the
# "constructor."
class UnionFind:
    """Union-Find (a.k.a. "disjoint set union" / DSU): a data structure
    purpose-built for exactly one question, answered very fast: "are
    these two things in the same group?" -- and one action: "merge two
    groups into one." That's literally the whole job of this file: every
    "shape" (a polygon or a chunk of a routing wire) starts in its own
    private group of one, and every time two shapes are found to
    physically touch, their groups get merged. Whatever ends up in the
    same group at the end is one electrical net.

    Internally, a group is represented as a TREE: every node points at
    its "parent," and the node whose parent points at ITSELF is the
    tree's root -- that root doubles as the group's unique ID. Two shapes
    are "in the same group" exactly when walking up their parent chains
    lands on the same root.
    """

    def __init__(self, n):
        # `self.parent` starts as [0, 1, 2, ..., n-1] -- every one of the
        # n shapes begins as its own parent, i.e. n separate one-shape
        # groups. `list(range(n))` builds that: `range(n)` lazily counts
        # 0..n-1 (like the `range(0, n, 2)` seen in gdsParser.py's
        # decode_xy, just without a custom step), and `list(...)` forces
        # it into an actual list of numbers.
        self.parent = list(range(n))
        # `self.rank` is bookkeeping used only to keep the trees flat/fast
        # (explained in `union` below) -- everyone starts at rank 0.
        # `[0] * n` is list-repetition syntax: "the list [0], repeated n
        # times" -- a quick way to build a list of n zeros.
        self.rank = [0] * n

    def find(self, x):
        """Which group (root id) does shape `x` currently belong to?"""
        # First loop: walk up parent pointers -- `root`, then
        # `parent[root]`, then `parent[parent[root]]`, etc. -- until we
        # land on a node whose parent is itself (`self.parent[root] ==
        # root`), which by definition is the root of the tree/group.
        root = x
        while self.parent[root] != root:
            root = self.parent[root]

        # Second loop: PATH COMPRESSION. Now that we know the true root,
        # walk from `x` up to it a second time, and along the way rewire
        # every node we pass to point DIRECTLY at the root instead of at
        # its old (possibly far-away) parent. This means the NEXT time
        # anyone calls find() on any of these nodes, it resolves in one
        # hop instead of retracing the whole original chain -- the tree
        # gets flatter every time it's used. `self.parent[x], x =
        # root, self.parent[x]` updates two things on one line: it's
        # Python's simultaneous/tuple assignment (both right-hand values
        # are computed FIRST, using the OLD value of x, before either
        # assignment happens) -- so this rewires x's parent to `root`,
        # then moves `x` up to where its parent used to point, in one
        # step, without needing a temporary variable to avoid clobbering
        # `x` before you're done using its old value.
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        """Merge shape a's group and shape b's group into one."""
        # `ra, rb = self.find(a), self.find(b)` -- tuple assignment again,
        # computing both roots and naming them in one line.
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return  # already in the same group -- nothing to do

        # UNION BY RANK: `rank` is a rough estimate of "how tall is this
        # tree" (not tracked with perfect precision, but that's fine --
        # it's just a heuristic). When merging two groups, we always hang
        # the SHORTER tree underneath the TALLER one's root, rather than
        # picking arbitrarily -- if you always attached trees the "wrong"
        # way you could build one long chain n nodes deep, making later
        # find() calls slow. Keeping trees shallow is what makes
        # union-find fast even across thousands of shapes.
        # `ra, rb = rb, ra` is the classic Python swap -- exchanging two
        # variables' values without needing a temporary third variable.
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra  # attach the shorter tree's root under the taller one's root
        if self.rank[ra] == self.rank[rb]:
            # only grows when merging two EQUAL-height trees -- attaching
            # a strictly shorter tree under a taller one never increases
            # the taller one's height, so no rank bump is needed then.
            self.rank[ra] += 1


# ---------------------------------------------------------------------------
# 2. Layer connectivity rules (sky130 stack).
#
#    Deliberately starts at li1, not poly/diff/tap: for interconnect
#    (li1 up through met5, tied together by contacts/vias), two touching
#    shapes on the same layer really are one continuous net -- that's a
#    valid geometric rule. Transistor-level layers are NOT: sky130 draws
#    diffusion as one continuous strip under a whole row of series
#    transistors (and poly similarly runs cell-to-cell), so "touching"
#    there means "physically abutting silicon," not "same electrical
#    net" -- the source of transistor A and the drain of transistor B
#    can share a diffusion polygon while being electrically identical by
#    construction, but two *different* transistors' gates touching would
#    NOT imply a real net merge the way two touching met1 wires does.
#    Telling those apart needs actual device recognition (real LVS
#    territory), which is out of scope here -- so poly/diff/tap are
#    excluded from the conductor set entirely rather than reported with
#    misleading confidence. Everything else (nwell, psdm/nsdm implants,
#    npc, areaid.*/boundary.*/unverified.* markers, prBoundary) is
#    drawing metadata, not a conductor, and was never a candidate.
# ---------------------------------------------------------------------------
# A Python SET literal (see gdsParser.py's ELEMENT_START_TYPES comment for
# the general idea) -- "the list of layer names that count as real
# interconnect wiring." Used below purely for fast `x in CONDUCTOR_CANON`
# membership checks.
CONDUCTOR_CANON = {"li1", "met1", "met2", "met3", "met4", "met5"}

# contact material -> (layer-names-below, layer-names-above) it bridges
#
# This dict's VALUES are themselves a 2-tuple of two sets, e.g.
# `("mcon") -> ({"li1"}, {"met1"})`. It's nested this way (rather than,
# say, four separate variables) so the two related pieces of information
# --"what's on the bottom side" and "what's on the top side" of a given
# contact -- always travel together and get looked up with one dict access.
CONTACT_LINKS = {
    "mcon": ({"li1"}, {"met1"}),
    "via": ({"met1"}, {"met2"}),
    "via2": ({"met2"}, {"met3"}),
    "via3": ({"met3"}, {"met4"}),
    "via4": ({"met4"}, {"met5"}),
}


def canon(material):
    """Strip the ".pin" suffix so a pin label shape (met1.pin) is treated
    as the same conductor as the metal it labels (met1)."""
    # `"met1.pin".endswith(".pin")` -- a string method asking "does this
    # text end with that substring?" `material[:-4]` is a slice with a
    # NEGATIVE index: "everything except the last 4 characters" (".pin" is
    # 4 characters long), which turns "met1.pin" into "met1". The ternary
    # (`X if COND else Y`, same pattern seen throughout gdsParser.py) means
    # "strip it if there's a suffix to strip, otherwise leave it exactly
    # as-is."
    return material[:-4] if material.endswith(".pin") else material


def is_conductive(material):
    # `canon(material) in CONDUCTOR_CANON` -- "is this (suffix-stripped)
    # material one of our real wiring layers?" `or material in
    # CONTACT_LINKS` -- "...or is it itself a via/contact?" `in` on a dict
    # (like `CONTACT_LINKS` here) checks its KEYS, not its values.
    return canon(material) in CONDUCTOR_CANON or material in CONTACT_LINKS


def connects(m1, m2):
    """True if two shapes on materials m1/m2 are allowed to form one net
    when their footprints overlap."""
    b1, b2 = canon(m1), canon(m2)
    if b1 == b2 and b1 in CONDUCTOR_CANON:
        return True  # same real wiring layer on both sides -- e.g. met2 touching met2

    # `CONTACT_LINKS.items()` yields `(key, value)` pairs one at a time --
    # here that's `(contact_name, (below_set, above_set))`. Because the
    # value itself is a 2-tuple, the for-loop header can unpack THREE
    # things at once: the dict key into `contact`, and the two sets
    # straight out of the value-tuple into `below`/`above`, all in one
    # line, by nesting the unpacking pattern to match the data's shape.
    for contact, (below, above) in CONTACT_LINKS.items():
        # "if m1 IS this specific contact material, and m2's (canon'd)
        # layer is one of the two sides it's allowed to bridge..."
        # `below or above` here isn't boolean logic on sets themselves --
        # `b2 in below or b2 in above` checks membership in EITHER set.
        if m1 == contact and (b2 in below or b2 in above):
            return True
        # ...and the mirror image, in case it's m2 that's the contact
        # material instead of m1 (connects() should treat both orderings
        # the same way).
        if m2 == contact and (b1 in below or b1 in above):
            return True
    return False


def bucket_groups(material):
    """Which spatial-bucket group(s) a shape should be filed under, so it
    lands next to everything it could possibly connect to.

    (This function doesn't decide connectivity itself -- connects() above
    does that. This one only decides which "filing cabinet drawers"
    (buckets, built in union_shapes() below) a shape gets dropped into, so
    that when we later go looking for things it MIGHT connect to, they're
    findable without comparing against every shape in the whole design.)
    """
    b = canon(material)
    if b in CONDUCTOR_CANON:
        # `(b,)` -- a one-element TUPLE. Note the trailing comma: without
        # it, `(b)` would just be `b` wrapped in ordinary parentheses, NOT
        # a tuple at all (Python needs that comma to know you mean a
        # tuple). This function always returns a tuple of bucket names, so
        # even the single-conductor case has to be wrapped as one.
        return (b,)
    if material in CONTACT_LINKS:
        below, above = CONTACT_LINKS[material]
        # `below | above` is SET UNION: combine both sets into one (with
        # duplicates automatically merged, though there aren't any here --
        # a contact's below/above layer names never overlap). `tuple(...)`
        # then converts that combined set into a tuple, matching the
        # `(b,)` case above so callers always get the same kind of thing
        # back regardless of which branch ran.
        return tuple(below | above)
    return ()  # not a conductor at all -- goes in no bucket, never considered for connectivity


# ---------------------------------------------------------------------------
# 3. Placement transform: GDSII SREF order is mirror -> mag -> rotate ->
#    translate (spec-defined, not our choice).
# ---------------------------------------------------------------------------
def transform_point(x, y, pos, angle, mag, mirror):
    """Take one point in a structure's own LOCAL coordinates and work out
    where it actually ends up once that structure is placed (an SREF) at
    position `pos`, possibly mirrored/scaled/rotated. Four separate
    geometric operations chained together, applied in the exact order the
    GDSII spec requires."""
    if mirror:
        # Mirroring about the X axis just flips the sign of every y
        # coordinate -- `y = -y` reassigns `y` to its negative. (Reminder:
        # this reassigns the LOCAL Python variable `y` inside this
        # function; it has no effect outside transform_point.)
        y = -y
    # `x *= mag` is shorthand for `x = x * mag` -- Python supports this
    # "augmented assignment" shorthand for any operator (`+=`, `-=`, `*=`,
    # `/=`, ...), seen throughout this codebase (`current_layer` doesn't
    # use it, but e.g. `power_nets_dropped += 1` further down does).
    x *= mag
    y *= mag
    if angle:  # 0.0 is "falsy" in Python, so `if angle:` skips this whole block when there's no rotation at all
        # `math.radians(angle)` converts degrees (what GDS stores) into
        # radians (what math.cos/math.sin expect). This is standard 2D
        # ROTATION MATRIX math: rotating a point (x, y) by angle r about
        # the origin gives a new point
        #   x' = x*cos(r) - y*sin(r)
        #   y' = x*sin(r) + y*cos(r)
        # `c, s = math.cos(r), math.sin(r)` computes both once and names
        # them; then `x, y = x * c - y * s, x * s + y * c` is tuple
        # assignment again (both new values computed from the OLD x/y
        # first, then assigned together) -- necessary here because the
        # formula for the new y needs the OLD x, so we can't overwrite x
        # first and then compute y from it.
        r = math.radians(angle)
        c, s = math.cos(r), math.sin(r)
        x, y = x * c - y * s, x * s + y * c
    # Finally, translate (shift) by the placement position. `pos` is a
    # [x, y] pair, so `pos[0]`/`pos[1]` pull out its x/y components.
    return [x + pos[0], y + pos[1]]


def bbox_of(points):
    """The axis-aligned bounding box of a list of [x, y] points: the
    smallest rectangle (given as [xmin, ymin, xmax, ymax]) that contains
    all of them. Used as a cheap first-pass overlap check before doing
    the more expensive exact polygon test."""
    # Two list comprehensions (see decode_xy in gdsParser.py for the full
    # explanation of this syntax) pulling out just the x's, then just the
    # y's, from a list of [x, y] pairs.
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # `min()`/`max()` are Python built-ins that work directly on a list.
    return [min(xs), min(ys), max(xs), max(ys)]


def bbox_overlap(a, b):
    """Do two [xmin, ymin, xmax, ymax] boxes overlap (or just touch) at
    all? Two rectangles overlap exactly when each one's range overlaps
    the other's on BOTH axes -- this is the standard trick for that: check
    "a's left edge is left of b's right edge" AND "b's left edge is left
    of a's right edge" (that pair covers the X axis), then the same pair
    for Y. If any of the four fails, there's a gap on that axis and the
    boxes can't be touching."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def path_segment_rect(x0, y0, x1, y1, width, pathtype):
    """A PATH centerline segment -> its actual drawn rectangle (4 corners),
    not just an axis-aligned bbox -- matters for diagonal segments, and
    keeps this consistent with the exact polygon-overlap test below.
    pathtype 2 = square, ends extended by half-width; 0 = flush butt ends;
    1 (round) is approximated here as square, close enough for overlap
    purposes."""
    # Basic vector math: (dx, dy) is the direction from the segment's
    # start to its end; `math.hypot(dx, dy)` computes its LENGTH
    # (equivalent to sqrt(dx**2 + dy**2), just a built-in that does it in
    # one call). Dividing (dx, dy) by its own length gives a UNIT vector
    # (ux, uy) -- same direction, but exactly 1 unit long -- which is what
    # lets the rest of this function move "half a width" or "one
    # extension-length" along that exact direction without the segment's
    # actual length messing up the math.
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        # Degenerate case: a "segment" with the same start and end point
        # has no real direction -- just pick an arbitrary one (pointing
        # along +x) so the rest of the function doesn't divide by zero.
        ux, uy = 1.0, 0.0
    else:
        ux, uy = dx / length, dy / length
    half = width / 2.0
    # pathtype 2 (and, approximated, 1) extend the drawn rectangle past
    # each endpoint by half the wire's width, like a Sharpie's rounded/
    # square cap sticking out past where you lifted the pen; pathtype 0
    # stops exactly at the endpoint ("butt" end, no extension).
    ext = half if pathtype != 0 else 0.0
    # Push the two endpoints outward along the direction vector (ux, uy)
    # by `ext`, to account for that end-cap extension.
    px0, py0 = x0 - ux * ext, y0 - uy * ext
    px1, py1 = x1 + ux * ext, y1 + uy * ext
    # perpendicular unit vector, scaled to half-width
    #
    # Rotating a 2D vector (ux, uy) by 90 degrees gives (-uy, ux) -- a
    # well-known shortcut (no trig function call needed, unlike
    # transform_point's rotation above) for "the direction sideways from
    # this one." Scaling that by `half` gives exactly the offset needed
    # to push a point from the segment's centerline out to one edge of
    # the wire.
    perp_x, perp_y = -uy * half, ux * half
    # The wire rectangle's four corners: from each extended endpoint,
    # step sideways by +perp and -perp to land on that end's two
    # corners -- four corners total, listed in order so they form a
    # proper rectangle outline (not a bowtie/crossed shape) when read in
    # sequence.
    return [
        [px0 + perp_x, py0 + perp_y],
        [px1 + perp_x, py1 + perp_y],
        [px1 - perp_x, py1 - perp_y],
        [px0 - perp_x, py0 - perp_y],
    ]


# ---------------------------------------------------------------------------
# 3b. Exact polygon-polygon overlap test (not just bounding boxes).
#
#     Sky130 shapes are often non-rectangular (an L/T-shaped li1 routing
#     polygon, say), so two shapes' AXIS-ALIGNED BOUNDING BOXES can touch
#     even when the actual polygons never do. Using bbox-only overlap as
#     the union() trigger produces false connections that snowball through
#     the union-find into one giant net -- this exact test is what avoids
#     that. bbox_overlap() above is still used first as a cheap reject.
# ---------------------------------------------------------------------------
# Python mechanic: a function name starting with a single underscore
# (`_orient`, `_on_segment`, ...) is a convention meaning "internal helper,
# not meant to be called from outside this file" -- Python doesn't
# actually enforce that (nothing stops another file from calling
# `netGraph._orient(...)`), it's purely a signal to a human reader that
# these four exist only to support `polygons_overlap` below and aren't
# part of this module's "public" toolkit the way e.g. `build_shapes` is.

def _orient(p, q, r):
    """The classic computational-geometry "orientation" test: given three
    points p, q, r, are they arranged clockwise, counter-clockwise, or all
    in a straight line? This single test is the building block every
    other function on this page is made of.

    The math is the CROSS PRODUCT of vector (q-p) and vector (r-p) --
    its SIGN alone tells you the turn direction (you don't need its exact
    magnitude): positive means one rotational direction, negative the
    other, and (near) zero means the three points are collinear (r sits
    on the line through p and q)."""
    val = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    # Comparing against a tiny threshold (1e-9 = 0.000000001) instead of
    # exactly 0 guards against floating-point rounding noise -- after the
    # rotate/scale math in transform_point, a value that's mathematically
    # supposed to be exactly zero might come out as, say, 0.0000000003
    # instead, which this treats as "close enough to zero."
    if val > 1e-9:
        return 1
    if val < -1e-9:
        return -1
    return 0


def _on_segment(p, q, r):
    """Given that p, q, r are already known to be collinear (in a
    straight line -- this is only ever called after `_orient` returned 0),
    is point q actually BETWEEN p and r, rather than off past one end?"""
    # A point lying on the line through p and r is "between" them exactly
    # when its x is between p's and r's x (in either order), AND likewise
    # for y. `min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9` is
    # a CHAINED COMPARISON -- Python lets you write `low <= value <= high`
    # directly, instead of `low <= value and value <= high` -- to check
    # "q[0] falls within this range," again padded by that same tiny
    # 1e-9 tolerance for floating-point noise. The `and` on the next line
    # joins the X-axis check and the Y-axis check: both must hold.
    return (min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9 and
            min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9)


def _segments_intersect(p1, p2, p3, p4):
    """Do line segment p1->p2 and line segment p3->p4 cross, touch, or
    overlap at all? This is the standard textbook algorithm, built
    entirely out of the `_orient` test above, covering both the "normal"
    crossing case and the tricky "one segment's endpoint lands exactly on
    the other segment" edge case."""
    # The key insight: segment p1-p2 crosses segment p3-p4 exactly when
    # p3 and p4 are on OPPOSITE sides of the (infinite) line through
    # p1-p2, AND (symmetrically) p1 and p2 are on opposite sides of the
    # line through p3-p4. "Opposite sides" is exactly what two DIFFERENT
    # `_orient` results mean.
    o1, o2 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    o3, o4 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True  # the "normal" crossing case

    # The remaining four checks handle collinear/touching special cases:
    # if `_orient` came back exactly 0 (three points in a line), the
    # segments MIGHT still touch (e.g. one segment's endpoint sits right
    # on top of the other segment) even though the "opposite sides" test
    # above can't detect that. Each of these four checks one specific
    # such case: "p1, p2, p3 are collinear (o1==0), AND p3 actually falls
    # between p1 and p2 (not off past either end)" -- and so on for the
    # other three point combinations.
    if o1 == 0 and _on_segment(p1, p3, p2):
        return True
    if o2 == 0 and _on_segment(p1, p4, p2):
        return True
    if o3 == 0 and _on_segment(p3, p1, p4):
        return True
    if o4 == 0 and _on_segment(p3, p2, p4):
        return True
    return False


def _point_in_polygon(pt, poly):
    """Is point `pt` strictly inside polygon `poly` (a list of [x, y]
    corners)? Classic "ray casting" algorithm: imagine firing a
    horizontal ray from `pt` off to the right, forever; count how many of
    the polygon's edges that ray crosses. An ODD number of crossings
    means you started inside the shape (you have to cross the boundary an
    odd number of times to escape to the outside); an EVEN number
    (including zero) means you started outside."""
    x, y = pt
    inside = False   # our running "have we crossed an odd number of edges so far?" flag
    n = len(poly)
    j = n - 1   # start `j` at the LAST corner, so the very first edge checked is (last corner -> corner 0)
    # Walking every edge of the polygon: edge j->i, for i = 0, 1, 2, ..., n-1,
    # wrapping around so the final edge connects the last corner back to
    # the first one (that's what starting `j` at `n - 1` accomplishes,
    # rather than needing a special wraparound case at the end).
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # "Does this edge straddle pt's horizontal ray?" -- i.e. does one
        # endpoint sit above pt's y and the other below it? `(yi > y) !=
        # (yj > y)` compares two True/False values with `!=`: this is
        # exactly True when they DISAGREE (one above, one not) -- a
        # compact way to write "exactly one of these two is above y"
        # without a longer `or`/`and` combination.
        if (yi > y) != (yj > y):
            # This edge does straddle pt's height -- find the exact X
            # coordinate where the edge crosses that horizontal line
            # (basic "similar triangles" interpolation along the edge).
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                # The crossing point is to the right of pt, i.e. the ray
                # (which shoots rightward from pt) does pass through it --
                # flip our inside/outside flag. `inside = not inside` is
                # how you toggle a boolean in Python: `not` simply
                # negates True<->False.
                inside = not inside
        j = i   # advance: next iteration's "previous corner" is this one
    return inside


def polygons_overlap(a, b):
    """True if simple polygons a, b (lists of [x, y], open or closed)
    touch or intersect at all -- shared edges/vertices count as touching,
    which is the "these two shapes are physically connected" semantic we
    want for net extraction."""
    # GDS polygons are sometimes stored "closed" (the last point repeats
    # the first, explicitly closing the loop) and sometimes "open" (it
    # doesn't). `a[-1]` is Python's way to index from the END of a list
    # (`-1` = last item, `-2` = second-to-last, etc.) -- so `a[0] ==
    # a[-1]` asks "does this polygon repeat its starting point at the
    # end?" If so, `a[:-1]` (a slice dropping just the last item) removes
    # that duplicate, so the rest of this function can always assume "open"
    # form and treat point i and point i+1 as one edge without special-casing
    # a redundant closing point.
    if len(a) > 1 and a[0] == a[-1]:
        a = a[:-1]
    if len(b) > 1 and b[0] == b[-1]:
        b = b[:-1]
    if len(a) < 2 or len(b) < 2:
        return True  # degenerate; caller already bbox-filtered
    na, nb = len(a), len(b)
    # Check EVERY edge of polygon a against EVERY edge of polygon b --
    # nested for-loops, O(na * nb) comparisons (fine here since these
    # polygons only have a handful of corners each). `a[(i + 1) % na]`
    # is the WRAPAROUND trick: `%` (modulo, "remainder after division")
    # makes the index cycle back to 0 once it would otherwise run past
    # the last valid index, so edge i's second point is correctly "the
    # next corner, wrapping back to the first corner after the last one."
    for i in range(na):
        p1, p2 = a[i], a[(i + 1) % na]
        for j in range(nb):
            p3, p4 = b[j], b[(j + 1) % nb]
            if _segments_intersect(p1, p2, p3, p4):
                return True   # found one crossing/touching edge pair -- that's enough, they overlap

    # If we got here, none of the EDGES cross -- but the polygons could
    # still overlap if one is sitting entirely inside the other (like a
    # via cut fully enclosed within a bigger metal rectangle, never
    # touching its boundary). Testing whether just ONE corner of a is
    # inside b (or vice versa) is enough to catch that case: if no edges
    # cross, then either ALL of a's points are inside b, or NONE are --
    # so checking a single point tells you which.
    if na >= 3 and _point_in_polygon(a[0], b):
        return True
    if nb >= 3 and _point_in_polygon(b[0], a):
        return True
    return False


# ---------------------------------------------------------------------------
# 4. Flatten OUT.json into a flat list of absolute-coordinate conductive
#    shapes. Every shape remembers which instance placed it, so the
#    resulting nets can be traced back to real standard-cell/via
#    instantiations, not just anonymous polygons.
# ---------------------------------------------------------------------------
def build_shapes(data):
    """Turn OUT.json's per-structure local geometry into one flat Python
    list of "shapes" in ABSOLUTE (whole-chip) coordinates -- this is the
    "FLATTEN" step described in the big module docstring at the top of
    this file. `data` is just the parsed OUT.json (a nested dict, exactly
    what `json.load` handed back in main() below)."""
    structures = data["structures"]
    # `data.get("instances", {})` -- like `dict.get(key, default)` seen in
    # gdsParser.py's material_for -- reads the "instances" key, or falls
    # back to an empty dict `{}` if OUT.json somehow didn't have one at
    # all (defensive, since a hand-edited or from-scratch input might
    # omit it).
    instances_by_container = data.get("instances", {})
    shapes = []  # {"material", "poly", "bbox", "instance_id"}

    # Python mechanic -- these two `def`s are NESTED functions (a
    # function defined inside another function), also called CLOSURES:
    # they're only usable from inside build_shapes, and -- importantly --
    # they can see and use `shapes` (the list from the enclosing function)
    # directly, without it being passed in as an argument. `shapes.append`
    # inside `add_polygon` mutates the very same list that build_shapes
    # will `return` at the bottom. This pattern ("a small helper that
    # closes over a shared list") is used here just to avoid repeating the
    # same "build a shape dict and append it" logic in four different
    # places below (top-level polygons, top-level paths, per-instance
    # polygons, per-instance paths).
    def add_polygon(material, layer, xy, instance_id, src=None):
        if len(xy) < 2:
            return  # not enough points to be a real shape -- skip silently
        # `src` is this polygon's index inside its own cell structure's
        # "polygons" list (None for TOP-level geometry and for paths). It
        # is what lets bond_intra_cell_pins() below map a per-cell-TYPE
        # fact ("islands 5 and 10 are the same pin") onto each individual
        # placement of that type, without redoing any geometry per instance.
        shapes.append({"material": material, "layer": layer, "poly": xy, "bbox": bbox_of(xy),
                       "instance_id": instance_id, "src": src})

    def add_path(material, layer, width, xy, instance_id, pathtype):
        # `zip(xy, xy[1:])` pairs up CONSECUTIVE points: zip walks two (or
        # more) lists side by side, stopping as soon as the shortest one
        # runs out. `xy[1:]` is "xy, but starting from its second element"
        # -- so pairing `xy` with `xy[1:]` produces (point0, point1),
        # (point1, point2), (point2, point3), ... -- exactly "every
        # adjacent pair," which is exactly "every line segment along this
        # multi-point path." The `for (x0, y0), (x1, y1) in ...` then
        # unpacks each of those pairs-of-points straight down into four
        # separate numbers in one go.
        for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
            rect = path_segment_rect(x0, y0, x1, y1, width, pathtype)
            shapes.append({
                "material": material,
                "layer": layer,
                "poly": rect,
                "bbox": bbox_of(rect),
                "instance_id": instance_id,
            })

    # `.items()` on a dict yields (key, value) pairs -- here,
    # `container` is a structure name (e.g. "adder_demo") and
    # `placements` is the list of instances placed inside it.
    for container, placements in instances_by_container.items():
        struct = structures.get(container, {})

        # shapes drawn directly in the container (not via an instance),
        # e.g. adder_demo's own top-level BOUNDARY/PATH elements
        #
        # `struct.get("polygons", [])` -- another `.get(key, default)`:
        # if this structure has no "polygons" key at all, loop over an
        # empty list instead of crashing. Every shape drawn directly
        # (not through a placed instance) is tagged with the special
        # instance id `"TOP"`.
        for poly in struct.get("polygons", []):
            if is_conductive(poly["material"]):
                add_polygon(poly["material"], poly["layer"], poly["xy"], "TOP")
        for path in struct.get("paths", []):
            if is_conductive(path["material"]):
                add_path(path["material"], path["layer"], path["width"], path["xy"], "TOP", path.get("pathtype", 0))

        # every individual instantiation placed inside this container
        #
        # `enumerate(placements)` walks a list while also handing back
        # each item's position -- `idx` (0, 1, 2, ...) alongside `inst`
        # (the actual instance dict) -- so we can build a unique id for
        # every placement below, even when two placements reference the
        # SAME structure (e.g. the 313 separate placements of
        # VIA_M1M2_PR all need different ids).
        for idx, inst in enumerate(placements):
            ref = inst.get("ref")
            ref_struct = structures.get(ref)
            if ref_struct is None or inst.get("kind") != "sref":
                continue  # this design has no AREF; skip gracefully if one appears
            pos = inst["pos"]
            angle = inst.get("angle", 0.0)
            mag = inst.get("mag", 1.0)
            mirror = inst.get("mirror", False)
            # f-string again: "index:reference-name", e.g. "313:VIA_M1M2_PR"
            # -- unique per placement (thanks to `idx`) while still
            # showing which structure it's a placement OF.
            instance_id = f"{idx}:{ref}"

            for poly_idx, poly in enumerate(ref_struct.get("polygons", [])):
                if not is_conductive(poly["material"]):
                    continue
                # List comprehension building a whole new list of
                # transformed points in one line: for every (x, y) pair
                # unpacked out of `poly["xy"]`, run it through
                # transform_point (mirror/scale/rotate/translate, see
                # section 3 above) using this instance's specific
                # placement, producing the shape's real position on the
                # finished chip.
                xy_abs = [transform_point(x, y, pos, angle, mag, mirror) for x, y in poly["xy"]]
                add_polygon(poly["material"], poly["layer"], xy_abs, instance_id, src=poly_idx)

            for path in ref_struct.get("paths", []):
                if not is_conductive(path["material"]):
                    continue
                xy_abs = [transform_point(x, y, pos, angle, mag, mirror) for x, y in path["xy"]]
                # `path["width"] * mag`: if this instance was placed with
                # a scale factor other than 1.0, the wire's width needs
                # to scale along with its coordinates, or the drawn
                # rectangle would be the wrong thickness relative to the
                # rest of the (scaled) shape.
                add_path(path["material"], path["layer"], path["width"] * mag, xy_abs, instance_id, path.get("pathtype", 0))

    return shapes


# ---------------------------------------------------------------------------
# 4b. Flatten labels (GDS TEXT elements) the same way, and match each one
#     to the conductive shape it's sitting on top of -- this is what lets
#     us attach a real pin/port NAME (and hence "is this VPWR/VGND?") to a
#     net, instead of guessing from geometry alone.
# ---------------------------------------------------------------------------
def build_labels(data):
    """The same flattening idea as build_shapes above, but for GDS TEXT
    labels (pin/port names) instead of drawn geometry -- walk every
    top-level label plus every label belonging to a placed instance, and
    convert each into one absolute-coordinate point. This is deliberately
    a near-duplicate of build_shapes's structure (same loops, same
    transform_point calls) rather than trying to force the two into one
    shared function -- labels and shapes end up used quite differently
    downstream, and keeping them as two plain, separately-readable
    functions was judged clearer than a more "clever" shared abstraction."""
    structures = data["structures"]
    instances_by_container = data.get("instances", {})
    labels = []  # {"text", "layer", "xy", "instance_id"}

    for container, placements in instances_by_container.items():
        struct = structures.get(container, {})

        # Labels drawn directly on the container itself -- for
        # "adder_demo" this is exactly the 6 real circuit port names
        # (A, B, S, clk, en, rst_n) plus its own VPWR/VGND labels.
        for lbl in struct.get("labels", []):
            labels.append({"text": lbl["text"], "layer": lbl["layer"], "xy": lbl["xy"], "instance_id": "TOP"})

        for idx, inst in enumerate(placements):
            ref = inst.get("ref")
            ref_struct = structures.get(ref)
            if ref_struct is None or inst.get("kind") != "sref":
                continue
            # Multiple values pulled out of `inst` in one tuple-assignment
            # line, each with its own `.get(key, default)` fallback --
            # same pattern as build_shapes, just written on one line here
            # instead of four.
            pos, angle, mag, mirror = inst["pos"], inst.get("angle", 0.0), inst.get("mag", 1.0), inst.get("mirror", False)
            instance_id = f"{idx}:{ref}"
            for lbl in ref_struct.get("labels", []):
                # A label only has ONE point (not a list of points like a
                # polygon), so transform_point is called once here rather
                # than inside a list comprehension.
                x, y = transform_point(lbl["xy"][0], lbl["xy"][1], pos, angle, mag, mirror)
                labels.append({"text": lbl["text"], "layer": lbl["layer"], "xy": [x, y], "instance_id": instance_id})

    return labels


def match_labels_to_nets(labels, shapes, dsu):
    """For each label, find the conductive shape (same instance, same raw
    GDS layer, a real conductor -- not a via/contact -- containing the
    label's point) it's naming, and record that net's root -> {texts}.
    Labels that land on a layer/instance we don't track as conductive
    (e.g. the layer-83 cell-name annotation, or VPB/VNB on the nwell
    layer) simply find no match and are dropped -- exactly right, since
    those were never candidate nets anyway.

    Returns (net_labels, net_top_labels, instance_pin_labels, unmatched):
      net_labels     -- root -> ALL label texts seen anywhere on that net
                         (every standard-cell instance labels its own pins
                         too, e.g. generic names like "A"/"X"/"Y" repeated
                         on every instance of a given cell type -- good
                         enough to spot VPWR/VGND, useless as a unique
                         net/port name).
      net_top_labels -- root -> label texts that came specifically from
                         the TOP structure's own TEXT elements -- these
                         are the real, unique external circuit port names
                         (e.g. "clk", "A", "S"), the only ones safe to use
                         as a port identity.
      instance_pin_labels -- instance_id -> {root -> {pin names}}. Unlike
                         net_labels (which throws every instance's pin
                         names into one net-wide bucket, losing WHICH
                         instance said "A"), this keeps the label's own
                         instance attached -- so for one specific
                         instance on one specific net you can look up
                         exactly which of ITS pins landed there (e.g.
                         "137:sky130_fd_sc_hd__mux2_1" on net 1996 used
                         pin "A"). That's the piece a netlist writer needs
                         to know which LUT port to connect a wire to.
    """
    # Build an index: instance_id -> list of shape indices belonging to
    # that instance. Without this, finding "which shapes belong to the
    # same instance as this label" would mean scanning the ENTIRE shapes
    # list (thousands of entries) for every single label -- building this
    # lookup table once up front turns that into a fast dict access.
    # `defaultdict(list)` -- same idiom as `instances` in gdsParser.py's
    # parse_gds_full: reading a brand-new key auto-creates an empty list
    # to `.append` onto.
    shapes_by_instance = defaultdict(list)
    for i, s in enumerate(shapes):
        shapes_by_instance[s["instance_id"]].append(i)

    # `defaultdict(set)`: same idea, but a missing key auto-creates an
    # empty SET instead of a list -- used here because `net_labels[root]`
    # collects label TEXTS, and a set automatically avoids storing the
    # same text twice for one net (e.g. many different standard-cell
    # instances on the same power net all contribute the label "VPWR" --
    # we only need to know it appeared, not how many times).
    net_labels = defaultdict(set)
    net_top_labels = defaultdict(set)
    # Nested defaultdict: `instance_pin_labels[instance_id]` auto-creates an
    # empty `defaultdict(set)` the first time an instance is seen, so
    # `instance_pin_labels[inst][root].add(text)` below just works without
    # ever needing to pre-populate either level by hand.
    instance_pin_labels = defaultdict(lambda: defaultdict(set))
    unmatched = []
    for lbl in labels:
        # Only consider shapes belonging to the SAME instance as this
        # label -- a label always names a pin/port on the specific
        # structure it's drawn inside, never something in a different,
        # unrelated instance.
        candidates = shapes_by_instance.get(lbl["instance_id"], [])
        hit = None
        for i in candidates:
            s = shapes[i]
            # Skip any shape that isn't on the exact same raw GDS layer as
            # the label, or isn't a "real" conductor (canon(...) not in
            # CONDUCTOR_CANON skips vias/contacts -- see the module
            # docstring's reasoning for why labels should bind to the
            # routing layer, not the contact sitting on top of it).
            if s["layer"] != lbl["layer"] or canon(s["material"]) not in CONDUCTOR_CANON:
                continue
            # First a cheap bbox reject (chained comparison, see
            # `_on_segment` above): if the label's point isn't even inside
            # this shape's BOUNDING box, it can't be inside the exact
            # polygon either, so there's no point running the pricier
            # check below.
            if not (s["bbox"][0] <= lbl["xy"][0] <= s["bbox"][2] and s["bbox"][1] <= lbl["xy"][1] <= s["bbox"][3]):
                continue
            # Then the EXACT test, via the same ray-casting
            # `_point_in_polygon` used for shape-to-shape overlap above.
            # A bbox-only check isn't tight enough here: sky130 pin shapes
            # are often L/T-shaped (e.g. a VPWR/VGND rail strip whose bbox
            # spans most of the cell), so a neighboring signal pin's label
            # point can fall inside that rail's BBOX while sitting nowhere
            # near its actual polygon -- silently mismatching a real pin
            # (say, a mux's "X" output) onto the power net. Requiring the
            # point to be truly inside the polygon closes that hole.
            if len(s["poly"]) >= 3 and _point_in_polygon(lbl["xy"], s["poly"]):
                hit = i
                break   # good enough -- stop looking as soon as we find any match
        if hit is None:
            unmatched.append(lbl)
            continue
        # `dsu.find(hit)` -- ask the union-find "which net does this
        # matched shape ultimately belong to?" (this function is always
        # called AFTER union_shapes has already run, so the union-find is
        # fully built by this point). `.add(...)` on a set inserts a value
        # (a no-op if it's already present, unlike a list's `.append`
        # which would create a duplicate).
        root = dsu.find(hit)
        net_labels[root].add(lbl["text"])
        if lbl["instance_id"] == "TOP":
            net_top_labels[root].add(lbl["text"])
        # Same fact as net_labels[root].add(...) above, just filed under
        # this label's OWN instance too -- this is what makes it possible
        # to later ask "what pin does instance X use on net root R?"
        # instead of only "what pin names appear ANYWHERE on net R?"
        instance_pin_labels[lbl["instance_id"]][root].add(lbl["text"])

    return net_labels, net_top_labels, instance_pin_labels, unmatched


# ---------------------------------------------------------------------------
# 4c. Intra-cell pin bonding: rejoin li1 islands that are one pin below li1.
#
#     Everything above models connectivity from li1 upward, which is the
#     right altitude for routing -- but it is NOT the whole story INSIDE a
#     standard cell. A single input pin frequently has several separate li1
#     islands that are one electrical node only because each one drops
#     through a licon1 contact onto the SAME poly gate. Only one of those
#     islands carries the pin's TEXT label; the router is free to land on
#     any of them.
#
#     When it lands on an unlabelled island, that island becomes a net of
#     its own and the labelled island keeps the pin name, so a single pin
#     is split into two nets: one holding the driver with a nameless
#     endpoint, the other holding the pin name with no driver at all. That
#     is exactly how net 7725 (a31oi_2 6991's A1) lost its driver -- the
#     route arrived on the cell's unlabelled second A1 island (net 7659).
#
#     The bond is computed once per cell TYPE from the cell's own local
#     geometry (81 structures, not 9875 placements) and then replayed onto
#     every placement, so it costs almost nothing and cannot depend on a
#     placement's rotation/mirroring.
#
#     Deliberately poly-only: two li1 islands sharing a poly polygon are
#     the same gate terminal, and a poly polygon is by construction one
#     conductor. Bonding through `diff` instead would be wrong -- a
#     transistor's source and drain sit on one diffusion polygon separated
#     only by the channel, so treating diff as a conductor would short
#     every transistor in the design.
# ---------------------------------------------------------------------------
def _rail_bound_li1(struct):
    """li1 polygon indices in this cell that are part of its VPWR/VGND rails.

    Needed because the poly-sharing test below is electrically true but too
    coarse for a tie cell: sky130's conb_1 ties its HI/LO outputs to the
    rails *through poly* (the cell even carries `resistive_li1_ok` markers
    saying so), so poly-sharing alone would dissolve HI into VPWR and LO
    into VGND. That is right about the silicon and wrong about the netlist
    -- a tie cell's output is a pin that drives real gate inputs, and
    merging it into a rail makes moduleGraph drop those inputs entirely as
    power nets. Rail islands are therefore never bonded to anything.

    Rails are found from the cell's own geometry rather than assumed: build
    a little union-find over just this cell's conductive shapes, then see
    which local nets a VPWR/VGND label lands on. The rails are drawn as
    PATHS in most cells, so paths have to be rasterized here too."""
    local = []   # (material, layer, poly, bbox, li1_index or None)
    for k, p in enumerate(struct.get("polygons", [])):
        if is_conductive(p["material"]):
            local.append((p["material"], p["layer"], p["xy"], bbox_of(p["xy"]),
                          k if p["material"] == "li1" else None))
    for p in struct.get("paths", []):
        if not is_conductive(p["material"]):
            continue
        for (x0, y0), (x1, y1) in zip(p["xy"], p["xy"][1:]):
            rect = path_segment_rect(x0, y0, x1, y1, p["width"], p.get("pathtype", 0))
            local.append((p["material"], p["layer"], rect, bbox_of(rect), None))

    dsu = UnionFind(len(local))
    for i in range(len(local)):
        for j in range(i + 1, len(local)):
            if not connects(local[i][0], local[j][0]):
                continue
            if not bbox_overlap(local[i][3], local[j][3]):
                continue
            if polygons_overlap(local[i][2], local[j][2]):
                dsu.union(i, j)

    power_roots = set()
    for lbl in struct.get("labels", []):
        if lbl["text"] not in POWER_NAMES:
            continue
        for i, (material, layer, poly, bbox, _) in enumerate(local):
            if layer != lbl["layer"] or canon(material) not in CONDUCTOR_CANON:
                continue
            if not (bbox[0] <= lbl["xy"][0] <= bbox[2] and bbox[1] <= lbl["xy"][1] <= bbox[3]):
                continue
            if len(poly) >= 3 and _point_in_polygon(lbl["xy"], poly):
                power_roots.add(dsu.find(i))
                break

    return {li1_idx for i, (_, _, _, _, li1_idx) in enumerate(local)
            if li1_idx is not None and dsu.find(i) in power_roots}


def build_pin_bond_groups(structures):
    """{structure_name: [frozenset(li1 polygon indices), ...]} -- which of a
    cell's li1 islands are a single electrical node beneath li1.

    Two islands are bonded when a licon1 contact ties each of them to the
    same poly polygon. Only groups of 2+ islands are returned; a pin whose
    li1 is a single island needs no repair. Islands belonging to the cell's
    power rails are excluded first -- see _rail_bound_li1."""
    groups = {}
    for name, struct in structures.items():
        polys = struct.get("polygons", [])
        li1_idx, poly_idx, licon_idx = [], [], []
        for k, p in enumerate(polys):
            if p["material"] == "li1":
                li1_idx.append(k)
            elif p["material"] == "poly":
                poly_idx.append(k)
            elif p["material"] == "licon1":
                licon_idx.append(k)
        if not (li1_idx and poly_idx and licon_idx):
            continue

        rails = _rail_bound_li1(struct)
        li1_idx = [k for k in li1_idx if k not in rails]
        if len(li1_idx) < 2:
            continue

        # bbox cache, so the overlap tests below stay cheap
        bb = {k: bbox_of(polys[k]["xy"]) for k in li1_idx + poly_idx + licon_idx}

        # poly polygon -> the li1 islands that contact it through a licon1
        touching = defaultdict(set)
        for c in licon_idx:
            cb = bb[c]
            hit_li = [k for k in li1_idx
                      if bbox_overlap(bb[k], cb) and polygons_overlap(polys[k]["xy"], polys[c]["xy"])]
            if not hit_li:
                continue
            hit_poly = [k for k in poly_idx
                        if bbox_overlap(bb[k], cb) and polygons_overlap(polys[k]["xy"], polys[c]["xy"])]
            for g in hit_poly:
                touching[g].update(hit_li)

        # Islands sharing a gate are one node; two gates sharing an island
        # chain together, so merge transitively rather than per-gate.
        merged = []
        for islands in touching.values():
            if len(islands) < 2:
                continue
            islands = set(islands)
            rest = []
            for existing in merged:
                if existing & islands:
                    islands |= existing
                else:
                    rest.append(existing)
            rest.append(islands)
            merged = rest
        if merged:
            groups[name] = [frozenset(g) for g in merged]
    return groups


def bond_intra_cell_pins(shapes, dsu, structures):
    """Union the flattened shapes of each placement according to the
    per-cell-type bonds from build_pin_bond_groups(). Returns how many
    unions it actually performed (0 means the design had no split pins)."""
    groups = build_pin_bond_groups(structures)
    if not groups:
        return 0

    # instance -> {source polygon index -> flattened shape index}
    by_instance = defaultdict(dict)
    for i, s in enumerate(shapes):
        if s.get("src") is not None:
            by_instance[s["instance_id"]][s["src"]] = i

    bonded = 0
    for instance_id, src_map in by_instance.items():
        ref = instance_id.split(":", 1)[1] if ":" in instance_id else None
        for group in groups.get(ref, ()):
            members = [src_map[k] for k in group if k in src_map]
            for other in members[1:]:
                if dsu.find(members[0]) != dsu.find(other):
                    dsu.union(members[0], other)
                    bonded += 1
    return bonded


# ---------------------------------------------------------------------------
# 5. Spatially-bucketed union-find over the flattened shapes.
# ---------------------------------------------------------------------------
def union_shapes(shapes, grid=2000, structures=None):
    """The "BUCKET" and "UNION" steps from the module docstring's
    pipeline: sort every shape into a coarse spatial+layer grid, then only
    ever compare shapes that landed in the same grid cell, instead of
    comparing all ~11,000 shapes against each other (which would be
    ~11,000 * 11,000 / 2 ~= 60 million comparisons -- this cuts that down
    to a few hundred thousand at most, since most shapes are nowhere near
    most other shapes).

    `grid=2000` is a DEFAULT ARGUMENT VALUE: calling `union_shapes(shapes)`
    with nothing else uses 2000 automatically, but a caller (see main()
    below, which exposes it as a `--grid` command-line flag) can override
    it with `union_shapes(shapes, grid=5000)` if they want bigger/smaller
    grid cells."""
    # `buckets` maps a "bucket key" -> list of shape indices filed under
    # it. A bucket key is a 3-tuple `(connectivity_group, grid_x,
    # grid_y)` -- e.g. `("met2", 14, 7)` means "met2-layer shapes sitting
    # in grid column 14, row 7." Tuples (not lists) are used as the dict
    # key here for the same reason `(layer, datatype)` was used as a dict
    # key in gdsParser.py's LAYER_MATERIAL_MAP: dict keys must be
    # immutable, and tuples are.
    buckets = defaultdict(list)
    for i, s in enumerate(shapes):
        xmin, ymin, xmax, ymax = s["bbox"]
        # Which grid column(s)/row(s) does this shape's bounding box
        # span? `xmin // grid` (integer division, see decode_xy in
        # gdsParser.py) converts a real coordinate into "which grid cell
        # index is this in" -- e.g. with grid=2000, an xmin of 4500 falls
        # in column 2 (4500 // 2000 == 2). A shape can be bigger than one
        # grid cell, so we compute BOTH the starting column (`gx0`, from
        # xmin) and ending column (`gx1`, from xmax) -- the loop just
        # below then files this shape into every cell it touches, not
        # just one.
        gx0, gx1 = int(xmin // grid), int(xmax // grid)
        gy0, gy1 = int(ymin // grid), int(ymax // grid)
        # `bucket_groups(...)` (defined in section 2 above) returns which
        # connectivity group(s) this shape's material belongs to -- e.g. a
        # `via` shape belongs to BOTH the "met1" and "met2" groups, since
        # it might need to meet a shape on either side. Looping over every
        # (group, grid-cell) combination this shape spans files it into
        # every bucket it could possibly need to be compared within.
        for group in bucket_groups(s["material"]):
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    buckets[(group, gx, gy)].append(i)

    dsu = UnionFind(len(shapes))
    # A shape spanning multiple grid cells (see above) gets filed into
    # more than one bucket -- meaning the SAME pair of shapes could turn
    # up together in more than one bucket's list, and we'd otherwise waste
    # time running the (somewhat expensive) exact overlap test on them
    # twice. `checked` is a set of "(i, j) pairs we've already tested,"
    # so we can skip repeats. Sets support very fast "have I seen this
    # before?" checks, same reason ELEMENT_START_TYPES was a set back in
    # gdsParser.py.
    checked = set()
    for idxs in buckets.values():
        n = len(idxs)
        # Compare every pair WITHIN one bucket -- `for a in range(n)` then
        # `for b in range(a + 1, n)` is the standard way to iterate every
        # unique pair of positions (a, b) with a < b, without ever
        # comparing a shape to itself or checking the same pair twice in
        # opposite order.
        for a in range(n):
            i = idxs[a]
            si = shapes[i]
            for b in range(a + 1, n):
                j = idxs[b]
                # Always store the pair with the smaller index first, so
                # `(3, 7)` and `(7, 3)` are treated as the same entry in
                # `checked` (otherwise the same pair encountered in a
                # different order would slip past the "already checked"
                # guard).
                pair = (i, j) if i < j else (j, i)
                if pair in checked:
                    continue
                checked.add(pair)
                sj = shapes[j]
                # Three checks, cheapest first, each one a chance to
                # `continue` (skip to the next pair) without paying for
                # the more expensive checks after it:
                #   1. connects() -- pure lookup, no geometry at all
                #   2. bbox_overlap() -- a handful of comparisons
                #   3. polygons_overlap() -- the expensive exact test,
                #      only ever run on pairs that already passed both
                #      cheaper filters
                if not connects(si["material"], sj["material"]):
                    continue
                if not bbox_overlap(si["bbox"], sj["bbox"]):
                    continue
                if polygons_overlap(si["poly"], sj["poly"]):
                    dsu.union(i, j)

    # Geometry above only sees li1 and up. `structures` (the raw per-cell
    # geometry) lets us also rejoin li1 islands that are one pin through a
    # shared poly gate -- see section 4c. Optional so existing callers that
    # only have `shapes` keep working, but every caller in this repo passes
    # it: without it, a cell's unlabelled pin islands stay split off.
    if structures is not None:
        bond_intra_cell_pins(shapes, dsu, structures)
    return dsu


# ---------------------------------------------------------------------------
# 6. Build the reported structures: the long shape->root parent map, the
#    grouped nets, and the bipartite cell (instance) -> nets graph.
# ---------------------------------------------------------------------------
def build_report(shapes, dsu):
    """Turn the raw union-find (just parent/rank arrays -- see the
    UnionFind class docstring) into three human/JSON-friendly views:
    `parent_map` (every shape's net id), `nets` (grouped, with details),
    and `cell_graph` (which nets touch which instance)."""
    # DICT COMPREHENSION: like a list comprehension (see decode_xy in
    # gdsParser.py) but building a dict instead of a list, using
    # `{key_expr: value_expr for ... in ...}`. This one builds, in a
    # single line, `{"0": <root of shape 0>, "1": <root of shape 1>, ...}`
    # for every shape index -- i.e. calling `dsu.find(i)` (which also
    # applies path compression as a side effect, see UnionFind.find) on
    # every single shape, once. `str(i)` converts the integer index to
    # text because JSON object keys must always be strings, never plain
    # numbers (this dict eventually gets written out with `json.dump` in
    # main() below).
    parent_map = {str(i): dsu.find(i) for i in range(len(shapes))}

    # Now group shape indices by their root, the opposite direction from
    # parent_map (root -> list of shapes, rather than shape -> root).
    # `dsu.find(i) for i in range(len(shapes))` is a GENERATOR EXPRESSION
    # (see gdsParser.py's `total_polys` for the same syntax) computing
    # every shape's root lazily; wrapping it in `enumerate(...)` numbers
    # those roots 0, 1, 2, ... as they come out -- but note that number is
    # really just `i` again (`enumerate` here is really only used to get a
    # readable `i, root` pair per loop iteration, since a plain `for root
    # in (...)` wouldn't tell us which shape index that root belonged to).
    members_by_root = defaultdict(list)
    for i, root in enumerate(dsu.find(i) for i in range(len(shapes))):
        members_by_root[root].append(i)

    nets = {}
    for root, members in members_by_root.items():
        # `{shapes[i]["material"] for i in members}` -- a SET
        # comprehension (curly braces, like a dict comprehension, but with
        # just one expression instead of a key:value pair -- so it
        # produces a set, automatically de-duplicating). This collects
        # every DISTINCT material among this net's member shapes (e.g. a
        # net touching both li1 and met1 shapes gives `{"li1", "met1"}`,
        # not one entry per shape). `sorted(...)` then converts that
        # unordered set into an alphabetically ordered list -- sets have
        # no guaranteed order, but a list written into JSON should be
        # stable/reproducible from run to run.
        materials = sorted({shapes[i]["material"] for i in members})
        instance_ids = sorted({shapes[i]["instance_id"] for i in members})
        # Four separate list comprehensions pulling out just one number
        # (one edge of the bounding box) from every member shape, so the
        # `min`/`max` calls below can find this net's OVERALL bounding
        # box across all its shapes combined.
        xs0 = [shapes[i]["bbox"][0] for i in members]
        ys0 = [shapes[i]["bbox"][1] for i in members]
        xs1 = [shapes[i]["bbox"][2] for i in members]
        ys1 = [shapes[i]["bbox"][3] for i in members]
        nets[str(root)] = {
            "shape_count": len(members),
            "materials": materials,
            "instances": instance_ids,
            "bbox": [min(xs0), min(ys0), max(xs1), max(ys1)],
        }

    # Bipartite instance -> nets view (see the earlier conversation for
    # why this is a "bipartite" structure rather than direct cell-to-cell
    # edges): for every net, record it against every instance that
    # touches it.
    cell_graph = defaultdict(set)
    for net_id, info in nets.items():
        for inst in info["instances"]:
            cell_graph[inst].add(net_id)
    # `{k: sorted(v, key=int) for k, v in cell_graph.items()}` -- a dict
    # comprehension again, this time used to convert every VALUE (each
    # instance's set of net ids) into a sorted list, while keeping the
    # same keys. `key=int` (same "how should sorted() compare items"
    # concept as gdsParser.py's `key=lambda kv: ...`) tells `sorted` to
    # compare the net-id strings AS NUMBERS (so "10" sorts after "9",
    # not before it the way plain text sorting would place it).
    cell_graph = {k: sorted(v, key=int) for k, v in cell_graph.items()}

    return parent_map, nets, cell_graph


# ---------------------------------------------------------------------------
# 7. Shared filtering helpers, used by moduleGraph.py to build the
#    nodes=modules/edges=wires graph from the shapes/dsu/net_labels this
#    file already computes.
# ---------------------------------------------------------------------------
POWER_NAMES = {"VPWR", "VGND", "VPB", "VNB"}


def is_logic_cell(instance_id):
    """A shape's `instance_id` is either "TOP" (drawn directly, not
    through a placed instance), "<index>:VIA_..." (a via/contact cell --
    physically real, but not "logic"), or "<index>:sky130_fd_sc_hd__..."
    (an actual standard cell like a NAND gate or flip-flop). This is True
    only for that last kind -- `!=` and `not in` combined with `and` (both
    conditions must hold)."""
    return instance_id != "TOP" and "VIA_" not in instance_id


# ---------------------------------------------------------------------------
# 8. CLI entry point.
# ---------------------------------------------------------------------------
def main():
    # Same "figure out where this script lives, so default paths work
    # from anywhere" trick as gdsParser.py's main() -- see its comments
    # for the full explanation of `__file__`/`os.path.dirname`/
    # `os.path.abspath`/`os.path.join`.
    here = os.path.dirname(os.path.abspath(__file__))
    default_in = os.path.join(here, "puzzleNetlist.json")
    default_out = os.path.join(here, "puzzleGraph.json")

    # argparse setup -- see gdsParser.py's main() for the full walkthrough
    # of what each piece (`ArgumentParser`, positional vs `--flag`
    # arguments, `default=`, `type=int`) means.
    parser = argparse.ArgumentParser(
        description="Union-find over OUT.json's flattened layout geometry: "
                     "unions shapes that touch on the same conductive layer "
                     "or through a via/contact, reconstructing electrical "
                     "nets and a per-cell connectivity graph."
    )
    parser.add_argument("in_json", nargs="?", default=default_in,
                         help=f"OUT.json produced by gdsParser.py (default: {default_in})")
    parser.add_argument("--out", default=default_out,
                         help=f"where to write the full net graph JSON (default: {default_out})")
    parser.add_argument("--grid", type=int, default=2000,
                         help="spatial bucket size in database units (default: 2000)")
    args = parser.parse_args()

    # `open(path)` with no mode argument defaults to `"r"` (read TEXT
    # mode) -- unlike gdsParser.py's `open(path, "rb")`, we want JSON
    # decoded as text here, not raw bytes. `json.load(f)` reads the whole
    # file and parses it back into nested Python dicts/lists -- the exact
    # reverse of `json.dump(...)` in gdsParser.py's main().
    with open(args.in_json) as f:
        data = json.load(f)

    # `time.time()` returns "seconds since a fixed reference point" as a
    # plain number -- on its own it's not very meaningful, but taking the
    # DIFFERENCE between two calls (`t1 - t0` etc., used in the print
    # statements below) gives you "how long did the code between these
    # two calls take to run," which is all this is used for -- rough,
    # human-readable timing printed to the terminal, not stored anywhere.
    t0 = time.time()
    shapes = build_shapes(data)
    labels = build_labels(data)
    t1 = time.time()
    dsu = union_shapes(shapes, grid=args.grid, structures=data["structures"])
    t2 = time.time()
    parent_map, nets, cell_graph = build_report(shapes, dsu)
    net_labels, net_top_labels, instance_pin_labels, unmatched_labels = match_labels_to_nets(labels, shapes, dsu)
    t3 = time.time()

    print(f"Flattened {len(shapes)} conductive shapes, {len(labels)} pin/port labels ({t1 - t0:.2f}s)")
    print(f"Union-find pass: {len(nets)} nets found ({t2 - t1:.2f}s)")
    print(f"Report build: parent_map={len(parent_map)} entries, "
          f"cell_graph={len(cell_graph)} instances, "
          f"{len(unmatched_labels)} labels unmatched ({t3 - t2:.2f}s)")

    # Same sorted()+key=lambda+slice pattern as gdsParser.py's main()
    # ("sort by computed value, descending, keep only the first N") --
    # here ranking nets by shape_count instead of polygon count.
    biggest = sorted(nets.values(), key=lambda n: -n["shape_count"])[:5]
    print("\nLargest nets (by shape count):")
    for n in biggest:
        # `{value:5d}` / `{value:4d}` format specs: `d` means "format as a
        # plain integer," and the number before it is a minimum field
        # width (right-aligned, padded with spaces) -- purely cosmetic
        # column alignment, same idea as gdsParser.py's `{material:<24}`
        # (just right-aligned here instead of left-aligned, since there's
        # no `<`).
        print(f"  {n['shape_count']:5d} shapes, {len(n['instances']):4d} instances, "
              f"materials={n['materials']}")

    # Write the detailed net graph (parent_map/nets/cell_graph) to disk as
    # JSON -- same `with open(..., "w") as f: json.dump(...)` pattern as
    # gdsParser.py's main().
    with open(args.out, "w") as f:
        json.dump({
            "stats": {
                "num_shapes": len(shapes),
                "num_nets": len(nets),
                "num_instances": len(cell_graph),
                "grid": args.grid,
            },
            "parent_map": parent_map,
            "nets": nets,
            "cell_graph": cell_graph,
            # JSON object keys must be strings, and instance_pin_labels'
            # inner keys are union-find root ints and its leaf values are
            # sets -- neither is JSON-legal as-is, so this nested dict
            # comprehension rebuilds it as instance_id -> {net_id_str:
            # [sorted pin names]}, the same str(root)/sorted(...) fixups
            # used for parent_map/cell_graph above.
            "instance_pins": {
                inst: {str(root): sorted(pins) for root, pins in by_net.items()}
                for inst, by_net in instance_pin_labels.items()
            },
        }, f, indent=2)
    print(f"\nWrote {args.out}")


# Same "only run main() if this script was launched directly, not merely
# imported" guard as gdsParser.py -- see its comment at the very bottom
# for the full explanation of `__name__ == "__main__"`.
if __name__ == "__main__":
    main()
