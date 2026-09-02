#!/usr/bin/env python3
"""boolExpr.py — parse and canonicalise the Boolean expressions used by cells.

Two jobs, shared by stage 5:

1. Parse the Verilog expressions in the cell-function tables (``A & B``,
   ``~((A1 & A2) | B1)``, ``S ? A1 : A0``, ``1'b0`` ...) into an AST.

2. Put an expression into a canonical normal form, so that two structurally
   different but functionally identical cones can be recognised without building
   a truth table. The normal form is:

   * negation-normal form — DeMorgan is applied until ``~`` sits only on
     variables (``~(a & b)`` becomes ``~a | ~b``);
   * constants folded away (``a & 1`` -> ``a``, ``a | 1`` -> ``1``, ...);
   * associative operators flattened (``(a & b) & c`` -> ``and(a, b, c)``);
   * commutative operands deduplicated and sorted, so operand order cannot
     affect the result;
   * ``a & a`` -> ``a``, ``a & ~a`` -> ``0``, ``a ^ a`` -> ``0``, and XOR parity
     folded into a single inverted flag.

   Nodes are immutable tuples, so identical subexpressions compare equal and the
   whole expression hashes to a single canonical key.

The normal form is *sound*: equal keys mean equal functions. It is not complete —
two equivalent functions can still normalise differently (no polynomial form is
complete), so a key match proves equivalence while a mismatch does not disprove
it. For small cones stage 5 uses an exact truth table instead, which is both.

Node shapes:
    ("c", 0|1)                  constant
    ("v", index)                variable
    ("nv", index)               negated variable
    ("and", (child, ...))       n-ary, sorted, deduplicated
    ("or",  (child, ...))       n-ary, sorted, deduplicated
    ("xor", (child, ...), p)    n-ary, sorted; p=1 means the result is inverted
"""

import re

TOKEN_RE = re.compile(r"\s*(\d+'b[01xz]|[A-Za-z_][A-Za-z0-9_]*|[~&|^?:()]|.)")

ZERO = ("c", 0)
ONE = ("c", 1)


class ParseError(Exception):
    pass


def tokenize(text):
    out = []
    pos = 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            break
        tok = m.group(1)
        pos = m.end()
        if tok.strip():
            out.append(tok)
    return out


class Parser(object):
    """Recursive descent over: ternary > or > xor > and > unary > primary."""

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None:
            raise ParseError("unexpected end of expression")
        if expected is not None and tok != expected:
            raise ParseError("expected %r, got %r" % (expected, tok))
        self.i += 1
        return tok

    def parse(self):
        node = self.ternary()
        if self.peek() is not None:
            raise ParseError("trailing tokens: %r" % self.toks[self.i:])
        return node

    def ternary(self):
        cond = self.or_expr()
        if self.peek() == "?":
            self.take("?")
            a = self.ternary()
            self.take(":")
            b = self.ternary()
            return ("mux", cond, a, b)
        return cond

    def or_expr(self):
        node = self.xor_expr()
        while self.peek() == "|":
            self.take("|")
            node = ("or2", node, self.xor_expr())
        return node

    def xor_expr(self):
        node = self.and_expr()
        while self.peek() == "^":
            self.take("^")
            node = ("xor2", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.unary()
        while self.peek() == "&":
            self.take("&")
            node = ("and2", node, self.unary())
        return node

    def unary(self):
        if self.peek() == "~":
            self.take("~")
            return ("not", self.unary())
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok == "(":
            node = self.ternary()
            self.take(")")
            return node
        if "'" in tok:
            bit = tok.split("'b")[1]
            if bit not in ("0", "1"):
                raise ParseError("non-Boolean literal %r" % tok)
            return ("lit", int(bit))
        if re.match(r"^[A-Za-z_]", tok):
            return ("name", tok)
        raise ParseError("unexpected token %r" % tok)


def parse(text):
    return Parser(tokenize(text)).parse()


# ---------------------------------------------------------------- construction
# The build_* helpers construct canonical nodes directly, so anything built with
# them is already in normal form.

def negate(node):
    """Complement a canonical node, pushing the negation inward (DeMorgan)."""
    kind = node[0]
    if kind == "c":
        return ("c", 1 - node[1])
    if kind == "v":
        return ("nv", node[1])
    if kind == "nv":
        return ("v", node[1])
    if kind == "and":
        return build_or([negate(c) for c in node[1]])
    if kind == "or":
        return build_and([negate(c) for c in node[1]])
    if kind == "xor":
        return ("xor", node[1], 1 - node[2])
    raise ValueError("cannot negate %r" % (node,))


def _flatten(kind, children):
    out = []
    for c in children:
        if c[0] == kind:
            out.extend(c[1])
        else:
            out.append(c)
    return out


def build_and(children):
    children = _flatten("and", children)
    seen = set()
    kept = []
    for c in children:
        if c == ZERO:
            return ZERO
        if c == ONE:
            continue
        if c in seen:
            continue                      # a & a = a
        seen.add(c)
        kept.append(c)
    for c in kept:
        if negate(c) in seen:             # a & ~a = 0
            return ZERO
    if not kept:
        return ONE
    if len(kept) == 1:
        return kept[0]
    return ("and", tuple(sorted(kept)))


def build_or(children):
    children = _flatten("or", children)
    seen = set()
    kept = []
    for c in children:
        if c == ONE:
            return ONE
        if c == ZERO:
            continue
        if c in seen:
            continue                      # a | a = a
        seen.add(c)
        kept.append(c)
    for c in kept:
        if negate(c) in seen:             # a | ~a = 1
            return ONE
    if not kept:
        return ZERO
    if len(kept) == 1:
        return kept[0]
    return ("or", tuple(sorted(kept)))


def build_xor(children):
    parity = 0
    flat = []
    for c in children:
        if c[0] == "xor":
            flat.extend(c[1])
            parity ^= c[2]
        else:
            flat.append(c)
    kept = []
    for c in flat:
        if c == ZERO:
            continue
        if c == ONE:
            parity ^= 1                   # a ^ 1 = ~a
            continue
        if c in kept:
            kept.remove(c)                # a ^ a = 0
            continue
        kept.append(c)
    if not kept:
        return ("c", parity)
    if len(kept) == 1:
        node = kept[0]
        return negate(node) if parity else node
    return ("xor", tuple(sorted(kept)), parity)


def build_mux(sel, a, b):
    """sel ? a : b, expanded so the normal form has no separate mux node."""
    return build_or([build_and([sel, a]), build_and([negate(sel), b])])


def from_ast(ast, env):
    """Turn a parsed expression into a canonical node. `env` maps pin names to
    canonical nodes."""
    kind = ast[0]
    if kind == "lit":
        return ONE if ast[1] else ZERO
    if kind == "name":
        if ast[1] not in env:
            raise KeyError("no value for %r" % ast[1])
        return env[ast[1]]
    if kind == "not":
        return negate(from_ast(ast[1], env))
    if kind == "and2":
        return build_and([from_ast(ast[1], env), from_ast(ast[2], env)])
    if kind == "or2":
        return build_or([from_ast(ast[1], env), from_ast(ast[2], env)])
    if kind == "xor2":
        return build_xor([from_ast(ast[1], env), from_ast(ast[2], env)])
    if kind == "mux":
        return build_mux(from_ast(ast[1], env), from_ast(ast[2], env),
                         from_ast(ast[3], env))
    raise ValueError("unknown ast node %r" % (ast,))


def key(node):
    """A stable, hashable canonical key for a normalised node."""
    kind = node[0]
    if kind == "c":
        return "%d" % node[1]
    if kind == "v":
        return "v%d" % node[1]
    if kind == "nv":
        return "!v%d" % node[1]
    if kind == "xor":
        return "^%d(%s)" % (node[2], ",".join(key(c) for c in node[1]))
    return "%s(%s)" % ("&" if kind == "and" else "|",
                       ",".join(key(c) for c in node[1]))


def variables(node, into=None):
    into = set() if into is None else into
    if node[0] in ("v", "nv"):
        into.add(node[1])
    elif node[0] in ("and", "or"):
        for c in node[1]:
            variables(c, into)
    elif node[0] == "xor":
        for c in node[1]:
            variables(c, into)
    return into


def evaluate(node, values, mask):
    """Evaluate over integer bit-masks (parallel evaluation of many assignments).
    `values` maps variable index -> mask; `mask` is the all-ones word."""
    kind = node[0]
    if kind == "c":
        return mask if node[1] else 0
    if kind == "v":
        return values[node[1]]
    if kind == "nv":
        return mask ^ values[node[1]]
    if kind == "and":
        acc = mask
        for c in node[1]:
            acc &= evaluate(c, values, mask)
            if acc == 0:
                return 0
        return acc
    if kind == "or":
        acc = 0
        for c in node[1]:
            acc |= evaluate(c, values, mask)
            if acc == mask:
                return mask
        return acc
    if kind == "xor":
        acc = mask if node[2] else 0
        for c in node[1]:
            acc ^= evaluate(c, values, mask)
        return acc
    raise ValueError("cannot evaluate %r" % (node,))


def size(node, seen=None):
    """Number of distinct operator nodes (shared subexpressions counted once)."""
    seen = set() if seen is None else seen
    if node[0] in ("c", "v", "nv") or node in seen:
        return 0
    seen.add(node)
    return 1 + sum(size(c, seen) for c in node[1])
