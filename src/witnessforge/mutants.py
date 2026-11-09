"""Mutation operators: realistic faults derived from each mapping rule.

Every mutant is a full (layout, mapping) pair that differs from the specification
in exactly one fault, so a mutant that survives points at one rule.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Iterator, List, Optional, Tuple

from .copybook import Field, Layout
from .mapping import Mapping, Rule

FILTER_RULE_ID = "FILTER"


@dataclass(frozen=True)
class Mutant:
    id: str
    rule_id: str
    operator: str
    description: str
    layout: Layout
    mapping: Mapping


OPERATORS = {
    # field decoding
    "offset_shift": "Field read one byte before or after its copybook offset",
    "scale_loss": "Implied decimal (V) ignored, so 123.45 becomes 12345",
    "sign_loss": "Sign of a signed numeric dropped",
    "byte_order": "Binary (COMP) field read little-endian",
    "wrong_source": "Rule reads a neighbouring field of the same type",
    "truncate": "Value cut one character short",
    # transformations
    "drop_step": "A transformation step is missing",
    "case_flip": "upper and lower swapped",
    "date_swap": "Day and month swapped in the input date format",
    "date_nulls_ignored": "Sentinel dates such as 00000000 are not turned into null",
    "map_default": "Every code falls through to the lookup default",
    "substring_shift": "Substring starts one position early or late",
    "substring_short": "Substring one character too short",
    "pad_side": "Padding added on the wrong side",
    "boolean_invert": "Boolean true and false values swapped",
    "arith_swap": "multiply and divide swapped",
    "round_places": "Rounded to one decimal place fewer",
    "blank_as_empty": "Blank values become \"\" instead of null",
    "constant_null": "Constant value not populated",
    "concat_separator": "Concatenated without the separator",
    "concat_order": "Concatenated parts in the wrong order",
    "concat_missing_part": "Last concatenated part missing",
    "concat_untrimmed": "Concatenated parts not trimmed",
    # conditions and records
    "condition_ignored": "Conditional rule applied to every record",
    "condition_negated": "Conditional rule applied to the wrong records",
    "filter_ignored": "Records that should be excluded are loaded",
    "filter_negated": "The wrong records are loaded",
    # validations
    "drop_check": "A validation check is missing",
    "bound_shift": "A boundary is off by one (min/max, length, item count)",
    "allowed_shrunk": "A valid code is rejected",
    "error_swallowed": "Invalid input loaded as null instead of rejected",
    # arrays, objects and duplicates
    "array_drop_last": "Last OCCURS element lost",
    "array_first_only": "Only the first OCCURS element kept",
    "keep_blanks": "Blank OCCURS elements or objects not removed",
    "keep_duplicates": "Duplicate array items not removed",
    "index_shift": "OCCURS elements misaligned (element i reads i+1)",
    "item_field_swap": "Two members of an array object swapped",
}

Change = Tuple[str, str, Optional[Rule], Optional[Layout]]  # operator, detail, rule, layout


def _swap_day_month(fmt: str) -> Optional[str]:
    if "%d" not in fmt or "%m" not in fmt:
        return None
    return fmt.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m")


def _nearest_same_type(layout: Layout, field: Field) -> Optional[Field]:
    candidates = [
        f for f in layout.fields
        if f.name != field.name and f.kind == field.kind and f.occurs == field.occurs
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.offset - field.offset))


def _with_step(rule: Rule, index: int, step) -> Rule:
    steps = list(rule.steps)
    steps[index] = step
    return replace(rule, steps=tuple(steps))


def _step_changes(rule: Rule) -> Iterator[Change]:
    steps = list(rule.steps)
    for i, (op, arg) in enumerate(steps):
        yield "drop_step", f"step '{op}' removed", replace(rule, steps=tuple(steps[:i] + steps[i + 1:])), None
        if op in ("upper", "lower"):
            yield "case_flip", f"'{op}' replaced by its opposite", _with_step(
                rule, i, ("lower" if op == "upper" else "upper", None)), None
        elif op == "date":
            swapped = _swap_day_month(arg["in"])
            if swapped:
                yield "date_swap", f"input format {arg['in']} read as {swapped}", _with_step(
                    rule, i, ("date", dict(arg, **{"in": swapped}))), None
            if arg["nulls"]:
                yield "date_nulls_ignored", f"sentinels {arg['nulls']} not mapped to null", _with_step(
                    rule, i, ("date", dict(arg, nulls=[]))), None
        elif op == "map" and arg["values"]:
            yield "map_default", "lookup table ignored", _with_step(
                rule, i, ("map", {"values": {}, "default": arg["default"]})), None
        elif op == "substring":
            if arg["start"] > 1:
                yield "substring_shift", "starts one position early", _with_step(
                    rule, i, ("substring", dict(arg, start=arg["start"] - 1))), None
            yield "substring_shift", "starts one position late", _with_step(
                rule, i, ("substring", dict(arg, start=arg["start"] + 1))), None
            if arg["length"] > 1:
                yield "substring_short", "one character too short", _with_step(
                    rule, i, ("substring", dict(arg, length=arg["length"] - 1))), None
        elif op == "pad":
            other = "right" if arg["side"] == "left" else "left"
            yield "pad_side", f"padded on the {other}", _with_step(rule, i, ("pad", dict(arg, side=other))), None
        elif op == "boolean":
            yield "boolean_invert", "true and false swapped", _with_step(
                rule, i, ("boolean", dict(arg, true=arg["false"], false=arg["true"]))), None
        elif op in ("multiply", "divide") and arg != 1:
            other = "divide" if op == "multiply" else "multiply"
            yield "arith_swap", f"{op} {arg} became {other} {arg}", _with_step(rule, i, (other, arg)), None
        elif op == "round" and arg > 0:
            yield "round_places", f"rounded to {arg - 1} places", _with_step(rule, i, ("round", arg - 1)), None
    ops = [op for op, _ in steps]
    required = any(op == "required" for op, _ in rule.checks)
    if ("null_if_blank" in ops or "date" in ops) and not required:  # "" and null both fail required
        yield "blank_as_empty", "blank becomes \"\" instead of null", replace(
            rule, steps=tuple(steps) + (("_blank_as_empty", None),)), None


def output_quantum(rule: Rule, field: Optional[Field]) -> Decimal:
    """Smallest step between output values of a numeric rule (after scaling and rounding)."""
    places = field.scale if field is not None and field.is_numeric else 0
    for op, arg in rule.steps:
        if op in ("multiply", "divide"):
            factor = arg if op == "multiply" else Decimal(1) / arg
            exponent = factor.normalize().as_tuple().exponent
            places += max(0, -exponent) if isinstance(exponent, int) else 4
            places = min(places, 12)
        elif op == "round":
            places = arg
    return Decimal(1).scaleb(-places)


def _bound_shift(arg, op: str, quantum: Decimal):
    if op in ("min_length", "max_length", "min_items", "max_items"):
        return [(arg - 1, "lower"), (arg + 1, "higher")] if arg > 0 else [(arg + 1, "higher")]
    return [(arg - quantum, "lower"), (arg + quantum, "higher")]


def _check_changes(rule: Rule, field: Optional[Field]) -> Iterator[Change]:
    checks = list(rule.checks)
    for i, (op, arg) in enumerate(checks):
        rest = tuple(checks[:i] + checks[i + 1:])
        if op != "unique":  # dataset-level, checked by verify
            yield "drop_check", f"check '{op}' removed", replace(rule, checks=rest), None
        if op in ("min", "max", "min_length", "max_length", "min_items", "max_items"):
            quantum = output_quantum(rule, field)
            for new, direction in _bound_shift(arg, op, quantum):
                shifted = list(checks)
                shifted[i] = (op, new)
                yield "bound_shift", f"{op} {arg} shifted {direction} to {new}", replace(
                    rule, checks=tuple(shifted)), None
        elif op == "allowed" and len(arg) > 1:
            for dropped in {arg[0], arg[-1]}:
                shrunk = list(checks)
                shrunk[i] = (op, tuple(a for a in arg if a != dropped))
                yield "allowed_shrunk", f"{dropped!r} no longer allowed", replace(
                    rule, checks=tuple(shrunk)), None


def _field_changes(rule: Rule, layout: Layout) -> Iterator[Change]:
    field = layout.field(rule.source)
    used = field.item_length
    for op, arg in rule.steps:
        if op == "substring":
            used = min(used, arg["start"] - 1 + arg["length"])
            break
    if field.kind == "X" and field.item_length > 1 and used >= field.item_length:
        yield "truncate", f"value cut to {field.item_length - 1} characters", replace(
            rule, steps=(("_truncate", field.item_length - 1),) + rule.steps), None
    if field.is_numeric and field.scale > 0:
        yield "scale_loss", f"{field.describe()} read with no implied decimal", None, layout.with_field(
            field.name, int_digits=field.total_digits, scale=0)
    if field.is_numeric and field.signed:
        yield "sign_loss", "sign dropped", replace(rule, steps=(("_abs", None),) + rule.steps), None
    if field.kind == "B":
        yield "byte_order", "read little-endian", None, layout.with_field(field.name, little_endian=True)
    if field.offset + field.length < layout.record_length:
        yield "offset_shift", f"read at offset {field.offset + 1} instead of {field.offset}", None, \
            layout.with_field(field.name, offset=field.offset + 1)
    if field.offset > 0:
        yield "offset_shift", f"read at offset {field.offset - 1} instead of {field.offset}", None, \
            layout.with_field(field.name, offset=field.offset - 1)
    neighbour = _nearest_same_type(layout, field)
    if neighbour is not None:
        yield "wrong_source", f"reads {neighbour.name} instead of {field.name}", replace(
            rule, sources=(neighbour.name,)), None
    yield from _step_changes(rule)
    yield from _check_changes(rule, field)


def _array_changes(rule: Rule) -> Iterator[Change]:
    yield "array_drop_last", "last element lost", replace(rule, array_op="drop_last"), None
    yield "array_first_only", "only first element kept", replace(rule, array_op="first_only"), None
    if rule.drop_blank:
        yield "keep_blanks", "blank elements kept", replace(rule, drop_blank=False), None
    if rule.distinct:
        yield "keep_duplicates", "duplicate items kept", replace(rule, distinct=False), None


def _rule_changes(rule: Rule, layout: Layout) -> Iterator[Change]:
    if rule.when is not None:
        yield "condition_ignored", f"applied even when not {rule.when.describe()}", replace(rule, when=None), None
        yield "condition_negated", f"applied when {replace(rule.when, negate=True).describe()}", replace(
            rule, when=replace(rule.when, negate=True)), None
    if rule.on_error == "reject":
        yield "error_swallowed", "invalid input loaded as null", replace(rule, on_error="null"), None

    if rule.items:
        for name, sub in rule.items:
            prefix = f"[{name}] "

            def wrap(changes, name=name, prefix=prefix):
                for operator, detail, new_sub, new_layout in changes:
                    yield operator, prefix + detail, (
                        rule.with_item(name, new_sub) if new_sub is not None else None), new_layout

            yield from wrap(_field_changes(sub, layout))
            yield "index_shift", f"{prefix}element i reads element i+1", rule.with_item(
                name, replace(sub, shift=1)), None
        same_kind = {}
        for name, sub in rule.items:
            same_kind.setdefault(layout.field(sub.source).kind, []).append((name, sub))
        for group in same_kind.values():
            if len(group) >= 2:
                (n1, s1), (n2, s2) = group[0], group[1]
                swapped = rule.with_item(n1, replace(s1, sources=s2.sources)).with_item(
                    n2, replace(s2, sources=s1.sources))
                yield "item_field_swap", f"'{n1}' and '{n2}' swapped", swapped, None
        yield from _array_changes(rule)
        yield from _check_changes(rule, None)
        return

    if rule.is_constant:
        if rule.value is not None:
            yield "constant_null", f"constant {rule.value!r} not populated", replace(rule, value=None), None
        yield from _step_changes(rule)
        yield from _check_changes(rule, None)
        return

    if rule.is_concat:
        if rule.separator:
            yield "concat_separator", f"separator {rule.separator!r} missing", replace(rule, separator=""), None
        yield "concat_order", "parts in reverse order", replace(rule, parts_op="reverse"), None
        yield "concat_missing_part", f"{rule.sources[-1]} missing", replace(rule, parts_op="drop_last"), None
        if rule.trim_parts:
            yield "concat_untrimmed", "parts not trimmed", replace(rule, trim_parts=False), None
        yield from _step_changes(rule)
        yield from _check_changes(rule, None)
        return

    yield from _field_changes(rule, layout)
    if layout.field(rule.source).occurs > 1:
        yield from _array_changes(rule)
        if rule.drop_blank or rule.distinct or rule.checks:
            yield "index_shift", "element i reads element i+1", replace(rule, shift=1), None


def generate_mutants(layout: Layout, mapping: Mapping) -> List[Mutant]:
    mutants: List[Mutant] = []
    used_ids = set()

    def add(rule_id: str, operator: str, detail: str, new_mapping: Mapping, new_layout: Layout):
        base_id = f"{rule_id}:{operator}"
        mutant_id, n = base_id, 2
        while mutant_id in used_ids:
            mutant_id, n = f"{base_id}#{n}", n + 1
        used_ids.add(mutant_id)
        mutants.append(Mutant(mutant_id, rule_id, operator, detail, new_layout, new_mapping))

    if mapping.filter is not None:
        f = mapping.filter
        add(FILTER_RULE_ID, "filter_ignored", f"every record loaded, not only {f.describe()}",
            replace(mapping, filter=None), layout)
        add(FILTER_RULE_ID, "filter_negated", f"loads records where {replace(f, negate=True).describe()}",
            replace(mapping, filter=replace(f, negate=True)), layout)

    for rule in mapping.rules:
        for operator, detail, new_rule, new_layout in _rule_changes(rule, layout):
            add(rule.id, operator, detail,
                mapping.with_rule(new_rule) if new_rule is not None else mapping,
                new_layout or layout)
    return mutants
