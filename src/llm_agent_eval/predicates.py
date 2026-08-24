"""Scalar predicate language (semantics §9A) — a closed grammar, hand-rolled.

This module implements the §9A scalar predicate language as a hand-rolled
recursive-descent parser over a **closed grammar**. It never calls ``eval()``
or ``exec()`` and never reaches into Python objects: dotted paths resolve by
dict lookup and pydantic field access only, and identifiers starting with an
underscore are rejected at compile time. There is no function call, no
indexing, no attribute escape.

Grammar (the closed grammar — nothing else is accepted):

    expr       := or
    or         := and ( "||" and )*
    and        := not ( "&&" not )*
    not        := "!" not | comparison
    comparison := primary ( ("==" | "!=" | "<" | "<=" | ">" | ">=") primary )?
    primary    := literal | path | "(" expr ")"
    literal    := string | integer | float | "true" | "false" | "null"
    path       := field ( "." ident )*
    field      := "payload" | "cost" | "timestamp" | "provider_request_id"
                | "parent_event_id" | "redaction_state"

The first path segment must be one of the whitelisted envelope fields above
(the §12B envelope names; the §9A.2 CEL variables ``args``/``output``/``test``/
``case``/``raw_value`` are metric-environment concerns, not event paths, and
are out of this language). Rejected at compile time: function calls, indexing
(``[]``), arithmetic beyond the closed grammar, any identifier not on the
field whitelist, and any identifier starting with ``_``.

Missing-value rule (stated here so it is contractual):

    Path resolution on a MISSING field returns the typed sentinel MISSING.
    Any comparison involving a MISSING operand evaluates to UNKNOWN — which
    is rendered as False at the top level (no silent truthiness) — EXCEPT
    ``x == null``, which is True exactly when x is MISSING (or an explicit
    JSON null). So ``x != null`` with x missing is False, not True: a missing
    field never passes a check by accident.

Comparisons are three-valued internally (True / False / UNKNOWN):

    - ``==`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=`` with a MISSING operand
      is UNKNOWN, except ``x == null`` with x missing (True).
    - Type mismatches (``payload.age < "five"``, bool vs number, ordering a
      list) are UNKNOWN — never a Python cross-type comparison.
    - Boolean combinators propagate UNKNOWN SQL-style: ``UNKNOWN && false`` is
      False, ``UNKNOWN || true`` is True, ``!UNKNOWN`` is UNKNOWN.
    - A non-boolean value (or MISSING) in a boolean position is UNKNOWN —
      never coerced to truthiness.
    - ``Predicate.evaluate`` returns True only for True; UNKNOWN and False
      both return False.

``Predicate.compile(text)`` raises explicit compile errors
(:class:`PredicateError`, with position) — compilation is never silent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pydantic import BaseModel

from .events import TraceEvent

__all__ = [
    "MISSING",
    "UNKNOWN",
    "ALLOWED_FIELDS",
    "PredicateError",
    "PredicateSyntaxError",
    "PredicateSemanticError",
    "Predicate",
]


# ---------------------------------------------------------------------------
# Value model: MISSING / UNKNOWN sentinels
# ---------------------------------------------------------------------------


class _MissingType:
    """Typed sentinel for a dotted path that did not resolve.

    Distinct from None (an explicit JSON null) and from False: a missing field
    is *not* falsy and never passes a check by accident (§9A missing-value
    rule, stated in the module docstring).
    """

    __slots__ = ()
    _instance: "_MissingType | None" = None

    def __new__(cls) -> "_MissingType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"


class _UnknownType:
    """Typed sentinel for an indeterminate comparison result (SQL-style
    three-valued logic). Rendered False at the top level."""

    __slots__ = ()
    _instance: "_UnknownType | None" = None

    def __new__(cls) -> "_UnknownType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNKNOWN>"


MISSING = _MissingType()
UNKNOWN = _UnknownType()

#: The closed set of event fields a predicate may root at (§12B envelope
#: names). "ts" from the implementer brief resolves to the canonical §12B
#: ``timestamp``; per-attempt monotonic order is ``sequence`` (proxy-assigned)
#: but is deliberately not exposed here — the whitelist is closed.
ALLOWED_FIELDS: tuple[str, ...] = (
    "payload",
    "cost",
    "timestamp",
    "provider_request_id",
    "parent_event_id",
    "redaction_state",
)

_Value = Any  # bool | int | float | str | None | datetime | list | dict | MISSING | UNKNOWN


# ---------------------------------------------------------------------------
# Compile-time errors
# ---------------------------------------------------------------------------


class PredicateError(Exception):
    """Base class for predicate compile errors (explicit, never silent)."""


class PredicateSyntaxError(PredicateError):
    """The predicate text is not in the closed grammar."""

    def __init__(self, text: str, position: int, message: str):
        self.text = text
        self.position = position
        self.message = message
        super().__init__(f"{message} (at position {position})")


class PredicateSemanticError(PredicateError):
    """The predicate parsed but violates a semantic rule (unknown field,
    underscore identifier)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "'": "'",
    "\\": "\\",
}


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int):
        self.kind = kind  # ident | number | string | op | dot | lparen | rparen | eof
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, {self.pos})"


def _scan_string(text: str, start: int) -> tuple[str, int]:
    """Scan a quoted string from ``start`` (the opening quote); returns
    (decoded value, index after the closing quote)."""
    quote = text[start]
    i = start + 1
    n = len(text)
    out: list[str] = []
    while i < n:
        c = text[i]
        if c == quote:
            return "".join(out), i + 1
        if c == "\\":
            if i + 1 >= n:
                raise PredicateSyntaxError(text, i, "unterminated escape sequence")
            esc = text[i + 1]
            if esc == "u":
                hex4 = text[i + 2 : i + 6]
                if len(hex4) != 4 or any(ch not in "0123456789abcdefABCDEF" for ch in hex4):
                    raise PredicateSyntaxError(text, i, "\\u escape requires exactly 4 hex digits")
                out.append(chr(int(hex4, 16)))
                i += 6
                continue
            if esc in _ESCAPES:
                out.append(_ESCAPES[esc])
                i += 2
                continue
            raise PredicateSyntaxError(text, i, f"unknown escape sequence \\{esc}")
        out.append(c)
        i += 1
    raise PredicateSyntaxError(text, start, "unterminated string literal")


def _scan_number(text: str, start: int) -> tuple[int | float, int]:
    i = start
    n = len(text)
    if text[i] == "-":
        i += 1
    while i < n and text[i].isdigit():
        i += 1
    is_float = False
    if i < n and text[i] == ".":
        is_float = True
        i += 1
        while i < n and text[i].isdigit():
            i += 1
    if i < n and text[i] in "eE":
        is_float = True
        i += 1
        if i < n and text[i] in "+-":
            i += 1
        while i < n and text[i].isdigit():
            i += 1
    raw = text[start:i]
    return (float(raw) if is_float else int(raw)), i


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        two = text[i : i + 2]
        if two in ("==", "!=", "<=", ">=", "&&", "||"):
            tokens.append(_Token("op", two, i))
            i += 2
            continue
        if c in "<>!":
            tokens.append(_Token("op", c, i))
            i += 1
            continue
        if c in "()":
            tokens.append(_Token("lparen" if c == "(" else "rparen", c, i))
            i += 1
            continue
        if c == ".":
            tokens.append(_Token("dot", ".", i))
            i += 1
            continue
        if c in "\"'":
            value, end = _scan_string(text, i)
            tokens.append(_Token("string", value, i))
            i = end
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            value, end = _scan_number(text, i)
            tokens.append(_Token("number", value, i))
            i = end
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(_Token("ident", text[i:j], i))
            i = j
            continue
        raise PredicateSyntaxError(text, i, f"unexpected character {c!r}")
    tokens.append(_Token("eof", None, n))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


class Node:
    __slots__ = ()

    def evaluate(self, event: TraceEvent) -> _Value:  # pragma: no cover - abstract
        raise NotImplementedError


class LiteralNode(Node):
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value

    def evaluate(self, event: TraceEvent) -> _Value:
        return self.value

    def __repr__(self) -> str:
        return f"Literal({self.value!r})"


class PathNode(Node):
    __slots__ = ("segments",)

    def __init__(self, segments: tuple[str, ...]):
        self.segments = segments

    def evaluate(self, event: TraceEvent) -> _Value:
        value: _Value = getattr(event, self.segments[0], MISSING)
        for seg in self.segments[1:]:
            if value is MISSING:
                return MISSING
            value = _resolve_segment(value, seg)
        return value

    def __repr__(self) -> str:
        return f"Path({'.'.join(self.segments)})"


def _resolve_segment(value: _Value, seg: str) -> _Value:
    """Resolve one dotted segment.

    Dicts are looked up by key; our own pydantic models are resolved through
    declared fields only (no methods, no dunders — the whitelist + the
    no-underscore rule make attribute escape impossible). Anything else (a
    string, a number, a list, a datetime) has no sub-segments → MISSING.
    """
    if isinstance(value, dict):
        return value.get(seg, MISSING)
    if isinstance(value, BaseModel):
        if seg in type(value).model_fields:
            return getattr(value, seg)
        return MISSING
    return MISSING


class CompareNode(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op: str, left: Node, right: Node):
        self.op = op
        self.left = left
        self.right = right

    def evaluate(self, event: TraceEvent) -> _Value:
        return _compare(self.op, self.left.evaluate(event), self.right.evaluate(event))

    def __repr__(self) -> str:
        return f"Compare({self.op!r}, {self.left!r}, {self.right!r})"


class AndNode(Node):
    __slots__ = ("left", "right")

    def __init__(self, left: Node, right: Node):
        self.left = left
        self.right = right

    def evaluate(self, event: TraceEvent) -> _Value:
        return _bool_and(self.left.evaluate(event), self.right.evaluate(event))

    def __repr__(self) -> str:
        return f"And({self.left!r}, {self.right!r})"


class OrNode(Node):
    __slots__ = ("left", "right")

    def __init__(self, left: Node, right: Node):
        self.left = left
        self.right = right

    def evaluate(self, event: TraceEvent) -> _Value:
        return _bool_or(self.left.evaluate(event), self.right.evaluate(event))

    def __repr__(self) -> str:
        return f"Or({self.left!r}, {self.right!r})"


class NotNode(Node):
    __slots__ = ("child",)

    def __init__(self, child: Node):
        self.child = child

    def evaluate(self, event: TraceEvent) -> _Value:
        return _bool_not(self.child.evaluate(event))

    def __repr__(self) -> str:
        return f"Not({self.child!r})"


# ---------------------------------------------------------------------------
# Three-valued evaluation core
# ---------------------------------------------------------------------------


def _typed_eq(a: _Value, b: _Value) -> bool:
    """Typed equality for present values: bools compare only with bools,
    numbers numerically, ISO 8601 strings compare with datetimes (mirroring
    ``_order``), everything else by identical type."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b  # 5 == 5.0
    if isinstance(a, datetime) and isinstance(b, str):
        parsed = _parse_datetime(b)
        return parsed is not UNKNOWN and a == parsed
    if isinstance(a, str) and isinstance(b, datetime):
        parsed = _parse_datetime(a)
        return parsed is not UNKNOWN and parsed == b
    return type(a) is type(b) and a == b


def _order(a: _Value, b: _Value) -> int | _UnknownType:
    """Return -1/0/1 when orderable, UNKNOWN otherwise. ISO 8601 strings
    compare with datetimes (so ``timestamp >= \"2026-01-01T00:00:00Z\"``
    works); anything else is a type mismatch → UNKNOWN."""
    if isinstance(a, bool) or isinstance(b, bool):
        return UNKNOWN
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    if isinstance(a, datetime) and isinstance(b, str):
        return _order(a, _parse_datetime(b))
    if isinstance(a, str) and isinstance(b, datetime):
        return _order(_parse_datetime(a), b)
    if isinstance(a, datetime) and isinstance(b, datetime):
        return (a > b) - (a < b)
    return UNKNOWN


def _parse_datetime(text: str) -> datetime | _UnknownType:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return UNKNOWN


def _compare(op: str, a: _Value, b: _Value) -> _Value:
    """Comparison with the missing-value rule: any comparison against MISSING
    is UNKNOWN (→ False at top level), EXCEPT ``x == null`` with x missing
    (True)."""
    if a is MISSING or b is MISSING:
        # `x == null` (either order) is True for a missing x; every other
        # comparison against missing is UNKNOWN — including `x != null`.
        if op == "==" and (a is None or b is None):
            return True
        return UNKNOWN
    if op == "==":
        return _typed_eq(a, b)
    if op == "!=":
        return not _typed_eq(a, b)
    ordered = _order(a, b)
    if ordered is UNKNOWN:
        return UNKNOWN
    if op == "<":
        return ordered < 0
    if op == "<=":
        return ordered <= 0
    if op == ">":
        return ordered > 0
    if op == ">=":
        return ordered >= 0
    raise AssertionError(f"unreachable comparison operator {op!r}")


def _as_truth(value: _Value) -> bool | _UnknownType:
    """A value in boolean position: True/False pass through; MISSING,
    UNKNOWN, and non-booleans are UNKNOWN — never coerced to truthiness."""
    if value is True:
        return True
    if value is False:
        return False
    return UNKNOWN


def _bool_and(a: _Value, b: _Value) -> _Value:
    ta, tb = _as_truth(a), _as_truth(b)
    if ta is False or tb is False:
        return False
    if ta is UNKNOWN or tb is UNKNOWN:
        return UNKNOWN
    return True


def _bool_or(a: _Value, b: _Value) -> _Value:
    ta, tb = _as_truth(a), _as_truth(b)
    if ta is True or tb is True:
        return True
    if ta is UNKNOWN or tb is UNKNOWN:
        return UNKNOWN
    return False


def _bool_not(a: _Value) -> _Value:
    ta = _as_truth(a)
    if ta is UNKNOWN:
        return UNKNOWN
    return not ta


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------

#: Mirrors spec._MAX_RULE_DEPTH (and trace_rules._MAX_DEPTH): the recursive
#: descent never descends past this many nesting levels — a clean compile
#: error at the cap, never a RecursionError.
_MAX_NESTING_DEPTH = 100


class _Parser:
    def __init__(self, text: str, tokens: list[_Token]):
        self.text = text
        self.tokens = tokens
        self.pos = 0
        self._depth = 0

    def peek(self) -> _Token:
        return self.tokens[self.pos]

    def advance(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _error(self, tok: _Token, message: str) -> PredicateSyntaxError:
        return PredicateSyntaxError(self.text, tok.pos, message)

    def _enter_nesting(self, tok: _Token) -> None:
        """Guard the recursion points (parens, ``!``) against pathological
        nesting: a clean compile error at ``_MAX_NESTING_DEPTH``, never a
        RecursionError."""
        if self._depth >= _MAX_NESTING_DEPTH:
            raise self._error(
                tok, f"predicate nesting exceeds {_MAX_NESTING_DEPTH} levels"
            )
        self._depth += 1

    def parse(self) -> Node:
        node = self._parse_or()
        tok = self.peek()
        if tok.kind != "eof":
            raise self._error(tok, f"unexpected trailing input {tok.value!r}")
        return node

    def _parse_or(self) -> Node:
        node = self._parse_and()
        while self.peek().kind == "op" and self.peek().value == "||":
            self.advance()
            node = OrNode(node, self._parse_and())
        return node

    def _parse_and(self) -> Node:
        node = self._parse_not()
        while self.peek().kind == "op" and self.peek().value == "&&":
            self.advance()
            node = AndNode(node, self._parse_not())
        return node

    def _parse_not(self) -> Node:
        if self.peek().kind == "op" and self.peek().value == "!":
            tok = self.advance()
            self._enter_nesting(tok)
            node = self._parse_not()
            self._depth -= 1
            return NotNode(node)
        return self._parse_comparison()

    def _parse_comparison(self) -> Node:
        left = self._parse_primary()
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("==", "!=", "<", "<=", ">", ">="):
            self.advance()
            right = self._parse_primary()
            return CompareNode(tok.value, left, right)
        return left

    def _parse_primary(self) -> Node:
        tok = self.peek()
        if tok.kind == "number":
            self.advance()
            return LiteralNode(tok.value)
        if tok.kind == "string":
            self.advance()
            return LiteralNode(tok.value)
        if tok.kind == "ident":
            if tok.value == "true":
                self.advance()
                return LiteralNode(True)
            if tok.value == "false":
                self.advance()
                return LiteralNode(False)
            if tok.value == "null":
                self.advance()
                return LiteralNode(None)
            return self._parse_path()
        if tok.kind == "lparen":
            self.advance()
            self._enter_nesting(tok)
            node = self._parse_or()
            self._depth -= 1
            close = self.peek()
            if close.kind != "rparen":
                raise self._error(close, "expected ')' to close the parenthesized expression")
            self.advance()
            return node
        raise self._error(tok, f"expected an expression, found {tok.value!r}")

    def _parse_path(self) -> Node:
        first = self.advance()
        name = first.value
        if name.startswith("_"):
            raise PredicateSemanticError(
                f"identifier {name!r} starts with '_' — attribute smuggling is not part of "
                "the predicate language"
            )
        if name not in ALLOWED_FIELDS:
            raise PredicateSemanticError(
                f"unknown field {name!r}; a predicate path must root at one of the "
                f"whitelisted event fields: {ALLOWED_FIELDS}"
            )
        segments = [name]
        while self.peek().kind == "dot":
            self.advance()
            seg = self.peek()
            if seg.kind != "ident":
                raise self._error(seg, "expected an identifier after '.'")
            if seg.value.startswith("_"):
                raise PredicateSemanticError(
                    f"identifier {seg.value!r} starts with '_' — attribute smuggling is not "
                    "part of the predicate language"
                )
            if seg.value in ("true", "false", "null"):
                # Reserved words are literals, not path segments.
                raise self._error(seg, f"reserved word {seg.value!r} cannot be a path segment")
            self.advance()
            segments.append(seg.value)
        return PathNode(tuple(segments))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Predicate:
    """A compiled §9A scalar predicate.

    Usage::

        p = Predicate.compile('payload.output.eligible == true')
        p.evaluate(event)  # -> bool

    ``compile`` (and the constructor) raise :class:`PredicateError` with an
    explicit position on any grammar violation — compilation is never silent.
    ``evaluate`` is total: it always returns a bool (UNKNOWN renders False),
    and a missing field never passes a check by accident (module docstring).
    """

    def __init__(self, text: str):
        if not isinstance(text, str):
            raise TypeError(f"predicate text must be a str, got {type(text).__name__}")
        if not text.strip():
            raise PredicateSyntaxError(text, 0, "predicate is empty")
        self.text = text
        self._tokens = _tokenize(text)
        self._ast = _Parser(text, self._tokens).parse()

    @classmethod
    def compile(cls, text: str) -> "Predicate":
        """Compile ``text`` into a Predicate; errors are explicit."""
        return cls(text)

    def evaluate(self, event: TraceEvent) -> bool:
        """Evaluate against one TraceEvent. Returns True only for True;
        UNKNOWN and False both return False. A missing field never passes."""
        if not isinstance(event, TraceEvent):
            raise TypeError(
                f"Predicate.evaluate expects a TraceEvent, got {type(event).__name__}"
            )
        return self._ast.evaluate(event) is True

    @property
    def tree(self) -> Node:
        """The compiled AST (introspection/debugging)."""
        return self._ast

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f"Predicate({self.text!r})"
