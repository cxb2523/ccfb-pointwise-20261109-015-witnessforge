"""Produce strings that match (and nearly match) a regular expression.

Used to build witness values for ``pattern`` checks and ``replace`` steps. It
handles the constructs that appear in field-format rules (literals, classes,
ranges, \\d \\w \\s, repeats, groups, alternation, anchors). Anything else
returns None, and the caller falls back to generic values.
"""

from __future__ import annotations

import re
from typing import List, Optional

try:  # Python 3.11+
    import re._parser as _sre_parse  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - older Pythons
    import sre_parse as _sre_parse  # type: ignore[no-redef]

_CATEGORY_CHAR = {
    "CATEGORY_DIGIT": "7",
    "CATEGORY_NOT_DIGIT": "K",
    "CATEGORY_WORD": "k",
    "CATEGORY_NOT_WORD": "-",
    "CATEGORY_SPACE": " ",
    "CATEGORY_NOT_SPACE": "Q",
}
_PROBES = "A9za-_ .#Z05m"


class _Unsupported(Exception):
    pass


def _name(constant) -> str:
    """Opcode name across Python versions (older ones are int subclasses with .name)."""
    return getattr(constant, "name", None) or str(constant)


def _class_char(items, variant: int) -> str:
    negate = any(_name(op) == "NEGATE" for op, _ in items)
    if negate:
        body = [(op, av) for op, av in items if _name(op) != "NEGATE"]
        for probe in _PROBES:
            if not any(_class_member(probe, op, av) for op, av in body):
                return probe
        raise _Unsupported("negated class")
    choices: List[str] = []
    for op, av in items:
        name = _name(op)
        if name == "LITERAL":
            choices.append(chr(av))
        elif name == "RANGE":
            low, high = av
            choices.append(chr(low + variant % (high - low + 1)))
        elif name == "CATEGORY":
            choices.append(_CATEGORY_CHAR.get(_name(av), "a"))
        else:
            raise _Unsupported(name)
    if not choices:
        raise _Unsupported("empty class")
    return choices[variant % len(choices)]


def _class_member(char: str, op, av) -> bool:
    name = _name(op)
    if name == "LITERAL":
        return ord(char) == av
    if name == "RANGE":
        return av[0] <= ord(char) <= av[1]
    if name == "CATEGORY":
        return bool(re.fullmatch({
            "CATEGORY_DIGIT": r"\d", "CATEGORY_NOT_DIGIT": r"\D", "CATEGORY_WORD": r"\w",
            "CATEGORY_NOT_WORD": r"\W", "CATEGORY_SPACE": r"\s", "CATEGORY_NOT_SPACE": r"\S",
        }.get(_name(av), r"(?!)"), char))
    return False


def _emit(parsed, variant: int) -> str:
    out: List[str] = []
    for op, av in parsed:
        name = _name(op)
        if name == "LITERAL":
            out.append(chr(av))
        elif name == "NOT_LITERAL":
            out.append("A" if chr(av) != "A" else "B")
        elif name == "ANY":
            out.append("x")
        elif name == "IN":
            out.append(_class_char(av, variant))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, high, sub = av
            count = low if low > 0 else (1 if high >= 1 else 0)
            out.extend(_emit(sub, variant + i) for i in range(count))
        elif name == "SUBPATTERN":
            out.append(_emit(av[-1], variant))
        elif name == "BRANCH":
            branches = av[1]
            out.append(_emit(branches[variant % len(branches)], variant))
        elif name == "AT":
            continue
        else:
            raise _Unsupported(name)
    return "".join(out)


def matching(pattern: str, variant: int = 0) -> Optional[str]:
    """A string that fully matches ``pattern``, or None if it cannot be built."""
    try:
        text = _emit(_sre_parse.parse(pattern), variant)
    except (_Unsupported, re.error, RecursionError, TypeError, ValueError):
        return None
    return text if re.fullmatch(pattern, text) else None


def near_miss(pattern: str, sample: Optional[str] = None) -> Optional[str]:
    """A string that resembles a match but does not fully match ``pattern``."""
    base = sample if sample is not None else matching(pattern)
    if base is None:
        return None
    attempts = []
    for index in range(len(base)):
        for probe in "#A9a":
            attempts.append(base[:index] + probe + base[index + 1 :])
    attempts += [base[:-1], base + "#", base + "9", "#" + base]
    for attempt in attempts:
        if attempt and re.fullmatch(pattern, attempt) is None:
            return attempt
    return None
