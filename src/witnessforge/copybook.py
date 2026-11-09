"""Parse COBOL copybooks into fixed-length record layouts.

Supported:
  * elementary items with PIC X / A / 9, including S (sign) and V (implied decimal)
  * USAGE DISPLAY (zoned decimal), COMP-3 / PACKED-DECIMAL, and binary
    COMP / COMP-4 / COMP-5 / BINARY (big-endian, up to 18 digits)
  * OCCURS n TIMES on elementary items
  * level-88 condition names (ignored, they occupy no storage)

Not yet supported (raises CopybookError so a layout is never silently misread):
  REDEFINES, OCCURS on group items, OCCURS DEPENDING ON, floating point
  COMP-1/COMP-2, and edited pictures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterator, List, Tuple


class CopybookError(ValueError):
    """Raised when a copybook cannot be parsed into a supported layout."""


_REPEAT = re.compile(r"([XA9])\((\d+)\)")
_ENTRY = re.compile(r"^(\d{1,2})\s+([A-Z0-9][A-Z0-9-]*)(.*)$")
_PIC = re.compile(r"\bPIC(?:TURE)?\s+(?:IS\s+)?(\S+)")
_OCCURS = re.compile(r"\bOCCURS\s+(\d+)")
_PACKED = re.compile(r"\b(?:COMP-3|COMPUTATIONAL-3|PACKED-DECIMAL)\b")
_FLOAT = re.compile(r"\b(?:COMP-[12]|COMPUTATIONAL-[12])\b")
_BINARY = re.compile(r"\b(?:COMP|COMP-[45]|COMPUTATIONAL|COMPUTATIONAL-[45]|BINARY)\b")


@dataclass(frozen=True)
class Field:
    """One elementary item of a fixed-length record."""

    name: str
    kind: str  # "X" alphanumeric, "9" zoned decimal, "P" packed (COMP-3), "B" binary (COMP)
    int_digits: int  # for "X": the character length
    scale: int = 0
    signed: bool = False
    occurs: int = 1
    offset: int = 0
    little_endian: bool = False  # only set by the byte-order mutant

    @property
    def total_digits(self) -> int:
        return self.int_digits + self.scale

    @property
    def item_length(self) -> int:
        """Bytes used by one occurrence."""
        if self.kind == "X":
            return self.int_digits
        if self.kind == "9":
            return self.total_digits
        if self.kind == "B":
            return 2 if self.total_digits <= 4 else 4 if self.total_digits <= 9 else 8
        return self.total_digits // 2 + 1

    @property
    def length(self) -> int:
        """Bytes used by all occurrences."""
        return self.item_length * self.occurs

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("9", "P", "B")

    def describe(self) -> str:
        if self.kind == "X":
            pic = f"X({self.int_digits})"
        else:
            pic = ("S" if self.signed else "") + f"9({self.int_digits})"
            if self.scale:
                pic += f"V9({self.scale})"
            if self.kind == "P":
                pic += " COMP-3"
            elif self.kind == "B":
                pic += " COMP"
        if self.occurs > 1:
            pic += f" OCCURS {self.occurs}"
        return pic


@dataclass(frozen=True)
class Layout:
    """An ordered set of fields describing one fixed-length record."""

    fields: Tuple[Field, ...]
    record_length: int

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def names(self) -> List[str]:
        return [f.name for f in self.fields]

    def with_field(self, name: str, **changes) -> "Layout":
        """Return a copy of the layout with one field changed (used by mutants)."""
        return Layout(
            tuple(replace(f, **changes) if f.name == name else f for f in self.fields),
            self.record_length,
        )


def _statements(text: str) -> Iterator[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\d{6}", line):  # fixed-format sequence area
            line = line[6:]
        stripped = line.strip()
        if not stripped or stripped[0] in "*/":
            continue
        lines.append(stripped)
    joined = " ".join(lines).upper()
    for stmt in re.split(r"\.(?=\s|$)", joined):
        stmt = " ".join(stmt.split())
        if stmt:
            yield stmt


def _picture(name: str, pic: str, usage: str) -> Tuple[str, int, int, bool]:
    expanded = _REPEAT.sub(lambda m: m.group(1) * int(m.group(2)), pic)
    if expanded and set(expanded) <= {"X", "A"}:
        if usage != "9":
            raise CopybookError(f"{name}: COMP usage is only valid on numeric pictures")
        return "X", len(expanded), 0, False
    signed = expanded.startswith("S")
    body = expanded[1:] if signed else expanded
    if not body.replace("V", "") or set(body) - {"9", "V"} or body.count("V") > 1:
        raise CopybookError(f"{name}: unsupported picture clause {pic!r}")
    left, _, right = body.partition("V")
    if usage == "B" and len(left) + len(right) > 18:
        raise CopybookError(f"{name}: binary fields over 18 digits are not supported")
    return usage, len(left), len(right), signed


def parse_copybook(text: str) -> Layout:
    """Parse copybook source text into a Layout with byte offsets assigned."""
    fields: List[Field] = []
    offset = 0
    fillers = 0
    seen = set()
    for stmt in _statements(text):
        match = _ENTRY.match(stmt)
        if not match:
            raise CopybookError(f"Cannot parse copybook statement: {stmt!r}")
        level, name, rest = int(match.group(1)), match.group(2), match.group(3)
        words = rest.split()
        if level == 88:
            continue
        if level == 66 or "RENAMES" in words:
            raise CopybookError(f"{name}: RENAMES is not supported")
        if "REDEFINES" in words:
            raise CopybookError(f"{name}: REDEFINES is not supported yet")
        if "DEPENDING" in words:
            raise CopybookError(f"{name}: OCCURS DEPENDING ON is not supported yet")
        pic = _PIC.search(rest)
        occurs = _OCCURS.search(rest)
        if pic is None:
            if occurs:
                raise CopybookError(f"{name}: OCCURS on group items is not supported yet")
            continue  # group item, no storage of its own
        if _PACKED.search(rest):
            usage = "P"
        elif _FLOAT.search(rest):
            raise CopybookError(f"{name}: floating point COMP-1/COMP-2 is not supported yet")
        elif _BINARY.search(rest):
            usage = "B"
        else:
            usage = "9"
        kind, int_digits, scale, signed = _picture(name, pic.group(1), usage)
        if name == "FILLER":
            fillers += 1
            name = f"FILLER-{fillers}"
        if name in seen:
            raise CopybookError(f"Duplicate field name {name}")
        seen.add(name)
        field = Field(
            name=name,
            kind=kind,
            int_digits=int_digits,
            scale=scale,
            signed=signed,
            occurs=int(occurs.group(1)) if occurs else 1,
            offset=offset,
        )
        if field.occurs < 1:
            raise CopybookError(f"{name}: OCCURS must be at least 1")
        fields.append(field)
        offset += field.length
    if not fields:
        raise CopybookError("Copybook contains no elementary fields")
    return Layout(tuple(fields), offset)


def load_copybook(path) -> Layout:
    with open(path, "r", encoding="utf-8") as handle:
        return parse_copybook(handle.read())
