"""Witness generation and minimization.

1. For every field, build candidate values from the rules that read it: the
   formats it must parse (dates, codes, booleans, numbers in text), the
   boundaries of its validations (lengths, ranges, patterns, allowed values),
   blanks, duplicates and element counts for arrays, and the values that switch
   conditions and filters on and off.
2. Build a valid baseline record (one that loads cleanly and exercises every
   conditional rule it can), plus one baseline per condition outcome, then vary
   one field at a time. Every candidate is valid for the copybook by
   construction, and the reference pipeline must accept, reject or filter it
   without crashing.
3. Run the reference pipeline and every mutant on every candidate. A candidate
   "kills" a mutant when the outputs differ.
4. Select the smallest set of candidates that kills every killable mutant
   (greedy set cover). Those are the witness records.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Set

from . import regexgen
from .codec import CodecError, decode_record, encode_record
from .copybook import Field, Layout
from .mapping import (
    ARRAY_CHECKS, FILTERED, REJECTED, Condition, Mapping, Rule, apply_step, assemble,
    check_passes, condition_matches, is_filtered, rule_outcome,
)
from .mutants import Mutant, generate_mutants, output_quantum
from .pipeline import ERROR_KEY, canonical, is_error, run_pipeline

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SAMPLE_DATES = (date(2024, 1, 31), date(2024, 3, 5), date(1999, 12, 9))


# --------------------------------------------------------------------------- field usage


@dataclass
class FieldUsage:
    rules: List[Rule] = dc_field(default_factory=list)  # rules (or object members) reading it alone
    conditions: List[Condition] = dc_field(default_factory=list)
    in_concat: bool = False


def field_usage(mapping: Mapping) -> Dict[str, FieldUsage]:
    usage: Dict[str, FieldUsage] = defaultdict(FieldUsage)
    if mapping.filter is not None:
        usage[mapping.filter.field].conditions.append(mapping.filter)
    for rule in mapping.rules:
        if rule.when is not None:
            usage[rule.when.field].conditions.append(rule.when)
        if rule.items:
            for _, sub in rule.items:
                usage[sub.source].rules.append(sub)
        elif rule.is_concat:
            for name in rule.sources:
                usage[name].in_concat = True
        elif rule.source:
            usage[rule.source].rules.append(rule)
    return usage


# --------------------------------------------------------------------------- candidate values


def _rotated_text(length: int, seed: int) -> str:
    shift = seed % len(_ALPHABET)
    letters = _ALPHABET[shift:] + _ALPHABET[:shift]
    return (letters * (length // len(letters) + 1))[:length]


def _digits(length: int, seed: int) -> str:
    return "".join(str((seed + i) % 9 + 1) for i in range(length))


def _numeric_pattern(field: Field, seed: int) -> Decimal:
    whole = _digits(field.int_digits, seed) or "0"
    frac = _digits(field.scale, seed + 4)
    return Decimal(f"{whole}.{frac}" if frac else whole)


def _dedupe(values: Sequence[Any]) -> List[Any]:
    seen, out = set(), []
    for value in values:
        key = repr(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _arith_factor(rule: Rule) -> Decimal:
    factor = Decimal(1)
    for op, arg in rule.steps:
        if op == "multiply":
            factor *= arg
        elif op == "divide":
            factor /= arg
    return factor


def _value_ok(value: Any, field: Field, rules: Sequence[Rule]) -> bool:
    """Does a single value transform cleanly and pass every scalar check of its rules?"""
    for rule in rules:
        try:
            out = value
            for op, arg in rule.steps:
                out = apply_step(out, op, arg, field)
        except Exception:
            return False
        for op, arg in rule.checks:
            if op not in ARRAY_CHECKS and op != "required" and not check_passes(out, op, arg):
                return False
            if op == "required" and not check_passes(out, op, arg) and field.occurs == 1:
                return False
    return True


def _text_specials(size: int, usage: FieldUsage, seed: int, variant: int) -> List[str]:
    out: List[str] = []
    for rule in usage.rules:
        for op, arg in rule.steps:
            if op == "date":
                ordered = _SAMPLE_DATES[variant % 3:] + _SAMPLE_DATES[:variant % 3]
                out += [d.strftime(arg["in"]) for d in ordered] + list(arg["nulls"])
            elif op == "map":
                keys = list(arg["values"])
                out += keys[variant % len(keys):] + keys[:variant % len(keys)]
                out.append(_rotated_text(size, seed + 11))
            elif op == "boolean":
                out += arg["true"] + arg["false"] + ["?"]
            elif op == "lstrip_zeros":
                out += ["00" + _digits(max(1, size - 2), seed), "0" * size]
            elif op == "replace":
                sample = regexgen.matching(arg["pattern"], variant)
                if sample:
                    out += [sample, f"{_rotated_text(2, seed)}{sample}"]
            elif op == "number":
                out += [_digits(size, seed), "0" * (size - 1) + "7", "1"]
        factor = _arith_factor(rule)
        for op, arg in rule.checks:
            if op == "pattern":
                match = regexgen.matching(str(arg), variant)
                out += [m for m in (match, regexgen.matching(str(arg), variant + 1),
                                    regexgen.near_miss(str(arg), match)) if m]
            elif op == "allowed":
                values = [str(a) for a in arg]
                out += values[variant % len(values):] + values[:variant % len(values)]
                out.append(_rotated_text(min(size, 3), seed + 13))
            elif op in ("min_length", "max_length"):
                out += [_rotated_text(n, seed) for n in (arg, arg - 1, arg + 1) if 0 < n <= size]
            elif op in ("min", "max") and any(o == "number" for o, _ in rule.steps):
                bound = arg / factor if factor else arg
                for value in (bound, bound - 1, bound + 1):
                    if value >= 0 and value == value.to_integral_value():
                        out.append(str(int(value)).zfill(size))
        ops = [op for op, _ in rule.steps]
        if "upper" in ops:
            out += [v.lower() for v in out if v.lower() != v]
        if "lower" in ops:
            out += [v.upper() for v in out if v.upper() != v]
    for condition in usage.conditions:
        out += [str(v) for v in condition.values] + [""]
    return out


def _numeric_specials(field: Field, usage: FieldUsage, quantum: Decimal) -> List[Decimal]:
    out: List[Decimal] = []
    for rule in usage.rules:
        factor = _arith_factor(rule)
        step = output_quantum(rule, field)
        for op, arg in rule.checks:
            if op in ("min", "max"):
                for target in (arg, arg - step, arg + step, arg - step / 2, arg + step / 2):
                    try:
                        out.append((target / factor).quantize(quantum))
                    except InvalidOperation:
                        continue
            elif op == "allowed":
                out += [Decimal(str(a)) for a in arg if _is_number(a)]
        for op, arg in rule.steps:
            if op in ("map", "boolean"):
                keys = list(arg["values"]) if op == "map" else arg["true"] + arg["false"]
                out += [Decimal(k) for k in keys if _is_number(k)]
    for condition in usage.conditions:
        out += [Decimal(str(v)) for v in condition.values if _is_number(v)]
    return out


def _is_number(value: Any) -> bool:
    try:
        Decimal(str(value))
        return True
    except InvalidOperation:
        return False


def item_candidates(field: Field, usage: FieldUsage, seed: int, variant: int = 0) -> List[Any]:
    """Candidate values for one occurrence of a field; the first is the preferred value."""
    if field.kind == "X":
        size = field.item_length
        primary = _rotated_text(size, seed)
        generic = [primary, primary.lower(), _rotated_text(max(1, size // 2), seed + 7), ""]
        values = [v for v in _dedupe(_text_specials(size, usage, seed, variant) + generic)
                  if len(v) <= size]
    else:
        quantum = Decimal(1).scaleb(-field.scale)
        largest = Decimal(10) ** field.int_digits - quantum
        pattern = _numeric_pattern(field, seed)
        generic = [pattern, Decimal(0), Decimal(1) if field.int_digits else quantum, largest]
        if field.signed:
            generic.insert(1, -pattern)
        lowest = -largest if field.signed else Decimal(0)
        values = _dedupe(
            v.quantize(quantum)
            for v in _numeric_specials(field, usage, quantum) + generic
            if lowest <= v <= largest
        )
    valid = [v for v in values if _value_ok(v, field, usage.rules)]
    return _dedupe(valid[:1] + values) if valid else values


def array_structures(field: Field, usage: FieldUsage, seed: int) -> List[List[Any]]:
    """Element layouts for an OCCURS field, always in the same order and count:
    all filled, last blank, every shorter element count, duplicates, duplicate across a gap.
    Parallel OCCURS fields use the same index so their blanks and duplicates line up."""
    items = item_candidates(field, usage, seed)
    blank: Any = "" if field.kind == "X" else Decimal(0).quantize(Decimal(1).scaleb(-field.scale))
    distinct = _dedupe(
        item_candidates(field, usage, seed + 5 * (i + 1), variant=i)[0] for i in range(field.occurs)
    )
    while len(distinct) < field.occurs:  # pad with any other valid items
        extra = [v for v in items if v not in distinct and _value_ok(v, field, usage.rules)]
        distinct.append(extra[0] if extra else blank)
    n = field.occurs
    lists = [distinct, distinct[:-1] + [blank]]
    lists += [distinct[:k] + [blank] * (n - k) for k in range(n - 1)]  # every element count
    lists.append(([distinct[0], distinct[0]] + distinct[1:])[:n])  # adjacent duplicate
    lists.append(([distinct[0], blank, distinct[0]] + distinct[1:])[:n])  # duplicate across a gap
    return lists


def value_candidates(field: Field, usage: FieldUsage, seed: int) -> List[Any]:
    """Candidate values for a whole field (lists for OCCURS fields)."""
    items = item_candidates(field, usage, seed)
    if field.occurs == 1:
        return items
    lists = array_structures(field, usage, seed)
    lists += [[item] * field.occurs for item in items]
    return _dedupe(lists)


# --------------------------------------------------------------------------- pool


def _uniquify(field: Field, value: Any, index: int) -> Any:
    """Make a key value unique per record while keeping its shape (case, padding, sign)."""
    if field.kind == "X":
        core = str(value or "").rstrip()
        if not core.strip():
            return None  # blank keys are not usable
        tag = f"{index:03d}"
        if len(core) > len(tag):
            return core[: len(core) - len(tag)] + tag
        return (core + tag)[: field.item_length]
    return value + index if value >= 0 else value - index


@dataclass
class Candidate:
    record: Dict[str, Any]
    data: bytes
    expected: Dict[str, Any]
    kills: Set[str] = dc_field(default_factory=set)

    @property
    def outcome(self) -> str:
        if FILTERED in self.expected:
            return "filtered"
        if REJECTED in self.expected:
            return "rejected"
        return "loaded"


@dataclass
class ForgeResult:
    layout: Layout
    mapping: Mapping
    mutants: List[Mutant]
    witnesses: List[Candidate]
    survivors: List[Mutant]
    pool_size: int

    @property
    def killed(self) -> Set[str]:
        return set().union(*(w.kills for w in self.witnesses)) if self.witnesses else set()

    @property
    def score(self) -> float:
        return len(self.killed) / len(self.mutants) if self.mutants else 1.0


def _badness(record: Dict[str, Any], layout: Layout, mapping: Mapping) -> float:
    """Lower is better: loads cleanly and exercises as many conditional rules as possible."""
    try:
        data = encode_record(layout, record)
    except CodecError:
        return float("inf")
    doc = run_pipeline(data, layout, mapping)
    if is_error(doc):
        return 1e6
    if FILTERED in doc:
        return 1e4
    score = 100.0 * len(doc.get(REJECTED, []))
    score += sum(1 for r in mapping.rules if r.when is not None and not condition_matches(record, r.when))
    return score


def _repair(base: Dict[str, Any], candidates: Dict[str, List[Any]], layout: Layout,
            mapping: Mapping, fixed: Set[str]) -> Dict[str, Any]:
    best = dict(base)
    best_score = _badness(best, layout, mapping)
    for _ in range(3):
        improved = False
        for name, values in candidates.items():
            if name in fixed or best_score == 0:
                continue
            for value in values:
                trial = dict(best)
                trial[name] = value
                score = _badness(trial, layout, mapping)
                if score < best_score:
                    best, best_score, improved = trial, score, True
        if not improved:
            break
    return best


def _condition_bases(base, candidates, layout, mapping) -> List[Dict[str, Any]]:
    """One extra baseline per condition, with the condition's outcome flipped."""
    conditions = [r.when for r in mapping.rules if r.when is not None]
    if mapping.filter is not None:
        conditions.append(mapping.filter)
    bases = []
    for condition in conditions:
        current = condition_matches(base, condition)
        for value in candidates[condition.field]:
            trial = dict(base)
            trial[condition.field] = value
            if condition_matches(trial, condition) != current:
                bases.append(_repair(trial, candidates, layout, mapping, {condition.field}))
                break
    return bases


def build_pool(layout: Layout, mapping: Mapping) -> List[Candidate]:
    usage = field_usage(mapping)
    candidates = {
        f.name: value_candidates(f, usage[f.name], seed=index * 7)
        for index, f in enumerate(layout.fields)
    }
    base = _repair({n: v[0] for n, v in candidates.items()}, candidates, layout, mapping, set())
    bases = [base] + _condition_bases(base, candidates, layout, mapping)

    records: List[Dict[str, Any]] = list(bases)
    seeds = {f.name: index * 7 for index, f in enumerate(layout.fields)}
    for rule in mapping.rules:  # line up blanks and duplicates across object members
        if not rule.items:
            continue
        members = [sub.source for _, sub in rule.items]
        shapes = {m: array_structures(layout.field(m), usage[m], seeds[m]) for m in members}
        for start in bases:
            for index in range(len(shapes[members[0]])):
                variant = dict(start)
                for member in members:
                    variant[member] = shapes[member][index]
                records.append(variant)
    width = max(len(values) for values in candidates.values())
    for k in range(1, width):  # "rich" records that change every field at once
        records.append({name: values[k % len(values)] for name, values in candidates.items()})
    for start in bases:  # one-field variations of every baseline
        for name, values in candidates.items():
            for value in values:
                if value != start[name]:
                    variant = dict(start)
                    variant[name] = value
                    records.append(variant)

    key_field = layout.field(mapping.key_rule.source) if mapping.key_rule else None
    pool: List[Candidate] = []
    seen, used_keys = set(), set()
    blank_key_loaded = False
    for index, record in enumerate(records):
        blank_key = False
        if key_field is not None:
            record = dict(record)
            unique = _uniquify(key_field, record[key_field.name], index)
            if unique is None:  # blank keys are kept to exercise key validations
                unique, blank_key = "", True
            elif unique in used_keys:
                continue
            record[key_field.name] = unique
        try:
            data = encode_record(layout, record)
        except CodecError:
            continue
        if data in seen:
            continue
        expected = run_pipeline(data, layout, mapping)
        if is_error(expected):  # input the specification itself cannot handle
            continue
        loaded = FILTERED not in expected and REJECTED not in expected
        if blank_key and loaded:  # at most one loaded document may have a blank key
            if blank_key_loaded:
                continue
            blank_key_loaded = True
        seen.add(data)
        if key_field is not None and not blank_key:
            used_keys.add(record[key_field.name])
        pool.append(Candidate(decode_record(layout, data), data, expected))
    return pool


class _CachedRecord:
    """Per-rule outcomes of the specification for one record, reused across mutants.

    Mutants share every unchanged Rule object with the specification, so only rules
    that are not the identical object (and the filter) are evaluated again.
    """

    def __init__(self, record: Dict[str, Any], layout: Layout, mapping: Mapping):
        self.record, self.layout = record, layout
        self.outcomes: Dict[int, Any] = {}
        for rule in mapping.rules:
            self._outcome(rule)

    def _outcome(self, rule: Rule):
        key = id(rule)
        if key not in self.outcomes:
            try:
                self.outcomes[key] = (rule, rule_outcome(self.record, rule, self.layout))
            except Exception as exc:  # noqa: BLE001 - a crash is an observable outcome
                self.outcomes[key] = (rule, exc)
        return self.outcomes[key][1]

    def run(self, mapping: Mapping) -> Dict[str, Any]:
        try:
            filtered = is_filtered(self.record, mapping)
            rules = ([mapping.key_rule] if mapping.key_rule else []) if filtered else mapping.rules
            outcomes = {}
            for rule in rules:
                outcome = self._outcome(rule)
                if isinstance(outcome, Exception):
                    if filtered:  # the key is optional on a filtered marker
                        outcome = (False, None, [])
                    else:
                        raise outcome
                outcomes[rule.id] = outcome
            return assemble(mapping, outcomes, filtered)
        except Exception as exc:  # noqa: BLE001
            return {ERROR_KEY: type(exc).__name__}


def forge(layout: Layout, mapping: Mapping) -> ForgeResult:
    """Generate the minimal witness set for a copybook and mapping."""
    mutants = generate_mutants(layout, mapping)
    pool = build_pool(layout, mapping)
    for candidate in pool:
        reference = canonical(candidate.expected)
        cached = _CachedRecord(candidate.record, layout, mapping)
        for mutant in mutants:
            if mutant.layout is layout:  # same decoding: recompute only the changed rule
                output = cached.run(mutant.mapping)
            else:
                output = run_pipeline(candidate.data, mutant.layout, mutant.mapping)
            if canonical(output) != reference:
                candidate.kills.add(mutant.id)

    remaining = set().union(*(c.kills for c in pool)) if pool else set()
    selected: List[Candidate] = []
    while remaining:
        best = max(pool, key=lambda c: (len(c.kills & remaining), c.outcome == "loaded"))
        gained = best.kills & remaining
        if not gained:
            break
        selected.append(best)
        remaining -= gained

    killed = set().union(*(c.kills for c in selected)) if selected else set()
    survivors = [m for m in mutants if m.id not in killed]
    return ForgeResult(layout, mapping, mutants, selected, survivors, len(pool))
