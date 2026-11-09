"""Mapping specifications: how each source record becomes a target JSON document.

A mapping is a YAML file::

    name: claim-header
    key: R1                              # rule that identifies a document
    filter: {field: REC-TYPE, equals: D} # records that are loaded at all
    rules:
      - id: R1
        source: CLAIM-ID                 # copybook field
        target: claimId                  # dotted JSON path in the output document
        steps: [strip]                   # ordered transformations
        checks: [required, {pattern: '^C[0-9]+$'}]   # validations on the result
        on_error: reject                 # invalid input -> reject record (default: fail)

      - id: R9
        source: [FIRST-NAME, LAST-NAME]  # concatenation
        separator: " "
        target: member.fullName

      - id: R10
        value: MEDICAID                  # constant
        target: sourceSystem

      - id: R11
        source: ADMIT-DATE
        when: {field: CLAIM-TYPE, equals: IP}   # conditional mapping
        else: null
        target: admission.date
        steps: [{date: {in: "%Y%m%d", out: "%Y-%m-%d", nulls: ["00000000"]}}]

This module also holds the reference implementation of the mapping, which is
what WitnessForge treats as the specification of correct behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .copybook import Field, Layout

PUBLIC_STEPS = {
    "strip", "rstrip", "lstrip", "upper", "lower", "number", "null_if_blank", "date", "map",
    "default", "substring", "lstrip_zeros", "pad", "boolean", "multiply", "divide", "round",
    "replace",
}
CHECKS = {
    "required", "min_length", "max_length", "min", "max", "pattern", "allowed",
    "min_items", "max_items", "unique_items", "unique",
}
ARRAY_CHECKS = {"min_items", "max_items", "unique_items"}
CONDITION_OPS = ("equals", "in", "not_blank", "blank")
REJECTED = "__rejected__"
FILTERED = "__filtered__"

Step = Tuple[str, Any]


class MappingError(ValueError):
    """Raised for an invalid mapping specification or a failing transformation."""


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    values: Tuple[Any, ...] = ()
    negate: bool = False  # set only by mutants

    def describe(self) -> str:
        text = {
            "equals": f"{self.field} = {self.values[0]!r}" if self.values else self.field,
            "in": f"{self.field} in {list(self.values)}",
            "not_blank": f"{self.field} is not blank",
            "blank": f"{self.field} is blank",
        }[self.op]
        return f"NOT ({text})" if self.negate else text


@dataclass(frozen=True)
class Rule:
    id: str
    target: str
    sources: Tuple[str, ...] = ()
    steps: Tuple[Step, ...] = ()
    checks: Tuple[Step, ...] = ()
    drop_blank: bool = False
    value: Any = None  # constant, used when there are no sources
    separator: str = ""
    trim_parts: bool = True
    when: Optional[Condition] = None
    when_else: Any = None
    on_error: str = "fail"  # fail | reject   ("null" is used only by mutants)
    distinct: bool = False  # remove duplicate array items
    items: Tuple[Tuple[str, "Rule"], ...] = ()  # array of objects from parallel OCCURS fields
    shift: int = 0  # set only by mutants: read element i+shift (misaligned OCCURS)
    array_op: Optional[str] = None  # set only by mutants
    parts_op: Optional[str] = None  # set only by mutants

    @property
    def source(self) -> Optional[str]:
        return self.sources[0] if len(self.sources) == 1 else None

    @property
    def is_constant(self) -> bool:
        return not self.sources and not self.items

    @property
    def all_sources(self) -> Tuple[str, ...]:
        """Every copybook field this rule reads (including object-array members)."""
        if self.items:
            return tuple(sub.source for _, sub in self.items)
        return self.sources

    def with_item(self, name: str, sub: "Rule") -> "Rule":
        return replace(self, items=tuple((n, sub if n == name else r) for n, r in self.items))

    @property
    def is_concat(self) -> bool:
        return len(self.sources) > 1


@dataclass(frozen=True)
class Mapping:
    name: str
    rules: Tuple[Rule, ...]
    key: Optional[str] = None  # id of the rule whose output identifies a document
    filter: Optional[Condition] = None

    @property
    def key_rule(self) -> Optional[Rule]:
        return self.rule(self.key) if self.key else None

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    def with_rule(self, new_rule: Rule) -> "Mapping":
        return replace(
            self, rules=tuple(new_rule if r.id == new_rule.id else r for r in self.rules)
        )


# --------------------------------------------------------------------------- parsing


def _one_key(rule_id: str, raw: Any, what: str) -> Tuple[str, Any]:
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, dict) and len(raw) == 1:
        return next(iter(raw.items()))
    raise MappingError(f"{rule_id}: each {what} must be a name or a one-key mapping, got {raw!r}")


def _normalize_step(rule_id: str, raw: Any) -> Step:
    op, arg = _one_key(rule_id, raw, "step")
    if op not in PUBLIC_STEPS:
        raise MappingError(f"{rule_id}: unknown step {op!r} (known: {sorted(PUBLIC_STEPS)})")
    if op == "date":
        if not (isinstance(arg, dict) and "in" in arg and "out" in arg):
            raise MappingError(f"{rule_id}: date step needs 'in' and 'out' formats")
        arg = {"in": arg["in"], "out": arg["out"], "nulls": [str(n) for n in arg.get("nulls") or []]}
    elif op == "map":
        if not (isinstance(arg, dict) and isinstance(arg.get("values"), dict)):
            raise MappingError(f"{rule_id}: map step needs a 'values' dictionary")
        arg = {"values": {str(k): v for k, v in arg["values"].items()}, "default": arg.get("default")}
    elif op == "substring":
        if not (isinstance(arg, dict) and int(arg.get("start", 0)) >= 1 and int(arg.get("length", 0)) >= 1):
            raise MappingError(f"{rule_id}: substring needs start >= 1 and length >= 1")
        arg = {"start": int(arg["start"]), "length": int(arg["length"])}
    elif op == "pad":
        if not (isinstance(arg, dict) and int(arg.get("length", 0)) >= 1):
            raise MappingError(f"{rule_id}: pad needs a length")
        side = arg.get("side", "left")
        char = str(arg.get("char", " "))
        if side not in ("left", "right") or len(char) != 1:
            raise MappingError(f"{rule_id}: pad side must be left/right and char one character")
        arg = {"length": int(arg["length"]), "char": char, "side": side}
    elif op == "boolean":
        if isinstance(arg, dict):  # YAML reads the keys true/false as booleans
            arg = {str(k).lower(): v for k, v in arg.items()}
        if not (isinstance(arg, dict) and arg.get("true") and arg.get("false")):
            raise MappingError(f"{rule_id}: boolean needs 'true' and 'false' value lists")
        arg = {
            "true": [str(v) for v in _as_list(arg["true"])],
            "false": [str(v) for v in _as_list(arg["false"])],
            "default": arg.get("default"),
        }
    elif op in ("multiply", "divide"):
        try:
            arg = Decimal(str(arg))
        except Exception as exc:
            raise MappingError(f"{rule_id}: {op} needs a number") from exc
        if op == "divide" and arg == 0:
            raise MappingError(f"{rule_id}: divide by zero")
    elif op == "round":
        if not isinstance(arg, int) or arg < 0:
            raise MappingError(f"{rule_id}: round needs a number of decimal places")
    elif op == "replace":
        if not (isinstance(arg, dict) and "pattern" in arg):
            raise MappingError(f"{rule_id}: replace needs 'pattern' (and 'with')")
        _compile(rule_id, arg["pattern"])
        arg = {"pattern": arg["pattern"], "with": str(arg.get("with", ""))}
    return op, arg


def _normalize_check(rule_id: str, raw: Any) -> Step:
    op, arg = _one_key(rule_id, raw, "check")
    if op not in CHECKS:
        raise MappingError(f"{rule_id}: unknown check {op!r} (known: {sorted(CHECKS)})")
    if op in ("required", "unique_items", "unique"):
        return op, None
    if op in ("min_length", "max_length", "min_items", "max_items"):
        if not isinstance(arg, int) or arg < 0:
            raise MappingError(f"{rule_id}: {op} needs a non-negative integer")
    elif op in ("min", "max"):
        try:
            arg = Decimal(str(arg))
        except Exception as exc:
            raise MappingError(f"{rule_id}: {op} needs a number") from exc
    elif op == "pattern":
        _compile(rule_id, arg)
    elif op == "allowed":
        if not isinstance(arg, list) or not arg:
            raise MappingError(f"{rule_id}: allowed needs a non-empty list")
        arg = tuple(arg)
    return op, arg


def _compile(rule_id: str, pattern: Any) -> None:
    try:
        re.compile(str(pattern))
    except re.error as exc:
        raise MappingError(f"{rule_id}: invalid regular expression {pattern!r}: {exc}") from exc


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _condition(owner: str, raw: Any, layout: Optional[Layout]) -> Condition:
    if not isinstance(raw, dict) or "field" not in raw:
        raise MappingError(f"{owner}: a condition needs 'field' and one of {CONDITION_OPS}")
    ops = [op for op in CONDITION_OPS if op in raw]
    if len(ops) != 1:
        raise MappingError(f"{owner}: a condition needs exactly one of {CONDITION_OPS}")
    field_name = str(raw["field"]).upper()
    if layout is not None:
        if field_name not in layout.names():
            raise MappingError(f"{owner}: condition field {field_name} is not in the copybook")
        if layout.field(field_name).occurs > 1:
            raise MappingError(f"{owner}: condition field {field_name} cannot be an OCCURS field")
    op = ops[0]
    values: Tuple[Any, ...] = ()
    if op == "equals":
        values = (raw["equals"],)
    elif op == "in":
        values = tuple(_as_list(raw["in"]))
    return Condition(field=field_name, op=op, values=values)


def _parse_items(rule_id: str, target: str, raw: Any, layout: Optional[Layout]):
    if not isinstance(raw, dict) or not raw:
        raise MappingError(f"{rule_id}: 'items' must map object keys to source fields")
    items = []
    occurs = set()
    for name, spec in raw.items():
        if isinstance(spec, str):
            spec = {"source": spec}
        if not isinstance(spec, dict) or not spec.get("source"):
            raise MappingError(f"{rule_id}.{name}: needs a 'source' field")
        source = str(spec["source"]).upper()
        sub_id = f"{rule_id}.{name}"
        if layout is not None:
            if source not in layout.names():
                raise MappingError(f"{sub_id}: source field {source} is not in the copybook")
            occurs.add(layout.field(source).occurs)
        sub = Rule(
            id=sub_id,
            target=f"{target}.{name}",
            sources=(source,),
            steps=tuple(_normalize_step(sub_id, s) for s in spec.get("steps") or []),
            checks=tuple(_normalize_check(sub_id, c) for c in spec.get("checks") or []),
        )
        if any(op in ARRAY_CHECKS or op == "unique" for op, _ in sub.checks):
            raise MappingError(f"{sub_id}: array and dataset checks belong on the parent rule")
        items.append((str(name), sub))
    if layout is not None and (len(occurs) != 1 or occurs.pop() < 2):
        raise MappingError(f"{rule_id}: item sources must be OCCURS fields with the same count")
    return tuple(items)


def mapping_from_dict(data: Dict[str, Any], layout: Optional[Layout] = None) -> Mapping:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise MappingError("Mapping must contain a non-empty 'rules' list")
    rules: List[Rule] = []
    ids, targets = set(), set()
    for index, raw in enumerate(data["rules"], start=1):
        if not isinstance(raw, dict):
            raise MappingError(f"Rule #{index} must be a mapping")
        rule_id = str(raw.get("id") or f"R{index}")
        if rule_id in ids:
            raise MappingError(f"Duplicate rule id {rule_id}")
        if not raw.get("target"):
            raise MappingError(f"{rule_id}: missing 'target'")
        target = str(raw["target"])
        if target in targets:
            raise MappingError(f"{rule_id}: target {target} is mapped twice")

        items: Tuple[Tuple[str, Rule], ...] = ()
        source = raw.get("source")
        if raw.get("items") is not None:
            items = _parse_items(rule_id, target, raw["items"], layout)
            sources = ()
        elif source is None:
            if "value" not in raw:
                raise MappingError(f"{rule_id}: needs a 'source' field or a constant 'value'")
            sources: Tuple[str, ...] = ()
        else:
            sources = tuple(str(s).upper() for s in _as_list(source))
            if not sources:
                raise MappingError(f"{rule_id}: 'source' is empty")
        if layout is not None:
            for name in sources:
                if name not in layout.names():
                    raise MappingError(f"{rule_id}: source field {name} is not in the copybook")
            if len(sources) > 1 and any(layout.field(s).occurs > 1 for s in sources):
                raise MappingError(f"{rule_id}: OCCURS fields cannot be concatenated")

        steps = raw.get("steps") or []
        checks = raw.get("checks") or []
        if not isinstance(steps, list) or not isinstance(checks, list):
            raise MappingError(f"{rule_id}: 'steps' and 'checks' must be lists")
        on_error = str(raw.get("on_error", "fail"))
        if on_error not in ("fail", "reject"):
            raise MappingError(f"{rule_id}: on_error must be 'fail' or 'reject'")

        checks_parsed = tuple(_normalize_check(rule_id, c) for c in checks)
        is_array = bool(items) or (
            layout is not None and len(sources) == 1 and layout.field(sources[0]).occurs > 1
        )
        if layout is not None and not is_array:
            bad = [op for op, _ in checks_parsed if op in ARRAY_CHECKS]
            if bad or raw.get("distinct"):
                raise MappingError(f"{rule_id}: {bad or 'distinct'} only applies to array rules")

        ids.add(rule_id)
        targets.add(target)
        rules.append(
            Rule(
                id=rule_id,
                target=target,
                sources=sources,
                steps=tuple(_normalize_step(rule_id, s) for s in steps),
                checks=checks_parsed,
                drop_blank=bool(raw.get("drop_blank", False)),
                distinct=bool(raw.get("distinct", False)),
                items=items,
                value=raw.get("value"),
                separator=str(raw.get("separator", "")),
                trim_parts=bool(raw.get("trim", True)),
                when=_condition(rule_id, raw["when"], layout) if raw.get("when") else None,
                when_else=raw.get("else"),
                on_error=on_error,
            )
        )

    key = data.get("key")
    if key is not None:
        key = str(key)
        if key not in ids:
            raise MappingError(f"key {key!r} is not a rule id")
        key_rule = next(r for r in rules if r.id == key)
        if key_rule.source is None:
            raise MappingError(f"key rule {key} must read exactly one source field")
        if layout is not None and layout.field(key_rule.source).occurs > 1:
            raise MappingError(f"key rule {key} cannot read an OCCURS field")
    record_filter = _condition("filter", data["filter"], layout) if data.get("filter") else None
    return Mapping(
        name=str(data.get("name") or "mapping"), rules=tuple(rules), key=key, filter=record_filter
    )


class _SpecLoader(yaml.SafeLoader):
    """YAML loader where only true/false are booleans (YAML 1.2 style).

    Plain YAML 1.1 turns codes such as NO (Norway), YES, ON and OFF into booleans,
    which silently corrupts lookup tables and allowed-value lists.
    """


_SpecLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_mapping(path, layout: Optional[Layout] = None) -> Mapping:
    data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_SpecLoader)  # noqa: S506
    return mapping_from_dict(data, layout)


# --------------------------------------------------------------------------- reference engine


class RuleRejected(Exception):
    """A rule with on_error: reject could not transform its input."""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _norm(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _equal(a: Any, b: Any) -> bool:
    try:
        return Decimal(_norm(a)) == Decimal(_norm(b))
    except Exception:
        return _norm(a) == _norm(b)


def condition_matches(record: Dict[str, Any], condition: Condition) -> bool:
    value = record[condition.field]
    if condition.op == "equals":
        result = _equal(value, condition.values[0])
    elif condition.op == "in":
        result = any(_equal(value, v) for v in condition.values)
    elif condition.op == "not_blank":
        result = not _is_blank(value)
    else:
        result = _is_blank(value)
    return not result if condition.negate else result


def _arith(value: Any, fn) -> Any:
    if _is_blank(value):
        return None
    was_float = isinstance(value, float)
    was_int = isinstance(value, int) and not isinstance(value, bool)
    result = fn(Decimal(str(value).strip()))
    if was_float:
        return float(result)
    if was_int:
        return int(result) if result == result.to_integral_value() else float(result)
    return result


def apply_step(value: Any, op: str, arg: Any, field: Optional[Field]) -> Any:
    if op in ("strip", "rstrip", "lstrip", "upper", "lower"):
        return getattr(value, op)() if isinstance(value, str) else value
    if op == "null_if_blank":
        return None if _is_blank(value) else value
    if op == "number":
        if _is_blank(value):
            return None
        number = Decimal(value.strip() if isinstance(value, str) else str(value))
        if field is not None and field.is_numeric and field.scale > 0:
            return float(number)
        return int(number) if number == number.to_integral_value() else float(number)
    if op == "date":
        if _is_blank(value) or _norm(value) in arg["nulls"]:
            return None
        return datetime.strptime(_norm(value), arg["in"]).strftime(arg["out"])
    if op == "map":
        return arg["values"].get(_norm(value), arg["default"])
    if op == "default":
        return arg if value is None else value
    if op == "substring":
        if not isinstance(value, str):
            return value
        start = arg["start"] - 1
        return value[start : start + arg["length"]]
    if op == "lstrip_zeros":
        if not isinstance(value, str):
            return value
        stripped = value.lstrip("0")
        return "0" if value and not stripped else stripped
    if op == "pad":
        if value is None:
            return None
        text = str(value)
        if arg["side"] == "left":
            return text.rjust(arg["length"], arg["char"])
        return text.ljust(arg["length"], arg["char"])
    if op == "boolean":
        key = _norm(value)
        if key in arg["true"]:
            return True
        if key in arg["false"]:
            return False
        return arg["default"]
    if op == "multiply":
        return _arith(value, lambda d: d * arg)
    if op == "divide":
        return _arith(value, lambda d: d / arg)
    if op == "round":
        return _arith(value, lambda d: d.quantize(Decimal(1).scaleb(-arg), rounding=ROUND_HALF_UP))
    if op == "replace":
        return None if value is None else re.sub(arg["pattern"], arg["with"], str(value))
    if op == "_truncate":
        return value[:arg] if isinstance(value, str) else value
    if op == "_abs":
        return abs(value) if isinstance(value, (Decimal, int, float)) else value
    if op == "_blank_as_empty":
        return "" if value is None else value
    raise MappingError(f"Unknown step {op}")


def check_passes(value: Any, op: str, arg: Any) -> bool:
    """True when ``value`` satisfies one check. Only ``required`` fails on a null."""
    if op == "required":
        return not _is_blank(value)
    if _is_blank(value):
        return True
    if op == "min_length":
        return len(str(value)) >= arg
    if op == "max_length":
        return len(str(value)) <= arg
    if op in ("min", "max"):
        try:
            number = Decimal(str(value).strip())
        except Exception:
            return False
        return number >= arg if op == "min" else number <= arg
    if op == "pattern":
        return re.fullmatch(str(arg), str(value)) is not None
    if op in ("unique", "min_items", "max_items", "unique_items"):
        return True  # evaluated on arrays or across records
    if op == "allowed":
        return any(_equal(value, a) if not isinstance(value, bool) else value == a for a in arg)
    raise MappingError(f"Unknown check {op}")


def _run_steps(value: Any, rule: Rule, field: Optional[Field]) -> Any:
    for op, arg in rule.steps:
        value = apply_step(value, op, arg, field)
    return value


def _transform(value: Any, rule: Rule, field: Optional[Field]) -> Any:
    try:
        return _run_steps(value, rule, field)
    except Exception:
        if rule.on_error == "reject":
            raise RuleRejected(rule.id)
        if rule.on_error == "null":
            return None
        raise


def _identity(value: Any) -> str:
    return repr(sorted(value.items())) if isinstance(value, dict) else repr(value)


def _dedupe(values: List[Any]) -> List[Any]:
    seen, out = set(), []
    for value in values:
        key = _identity(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _finish_array(rule: Rule, items: List[Any], blank) -> List[Any]:
    if rule.drop_blank:
        items = [item for item in items if not blank(item)]
    if rule.distinct:
        items = _dedupe(items)
    if rule.array_op == "drop_last":
        items = items[:-1]
    elif rule.array_op == "first_only":
        items = items[:1]
    return items


def _failed_checks(rule: Rule, value: Any) -> List[str]:
    if isinstance(value, list):
        failed = []
        for op, arg in rule.checks:
            if op == "required":
                ok = bool(value)
            elif op == "min_items":
                ok = len(value) >= arg
            elif op == "max_items":
                ok = len(value) <= arg
            elif op == "unique_items":
                ok = len({_identity(v) for v in value}) == len(value)
            else:
                ok = all(check_passes(item, op, arg) for item in value)
            if not ok:
                failed.append(f"{rule.id}:{op}")
        return failed
    return [f"{rule.id}:{op}" for op, arg in rule.checks if not check_passes(value, op, arg)]


def evaluate_rule(record: Dict[str, Any], rule: Rule, layout: Layout) -> Tuple[Any, List[str]]:
    """Return (output value, failed check ids). Raises RuleRejected for rejected input."""
    if rule.when is not None and not condition_matches(record, rule.when):
        return rule.when_else, []

    if rule.items:
        return _evaluate_items(record, rule, layout)
    if rule.is_constant:
        value: Any = _transform(rule.value, rule, None)
    elif rule.is_concat:
        parts = [_norm(record[s]) if rule.trim_parts else str(record[s]) for s in rule.sources]
        if rule.parts_op == "reverse":
            parts = parts[::-1]
        elif rule.parts_op == "drop_last":
            parts = parts[:-1]
        value = _transform(rule.separator.join(parts), rule, None)
    else:
        field = layout.field(rule.source)
        raw = record[rule.source]
        if field.occurs > 1:
            count = len(raw)
            items = [_transform(raw[(i + rule.shift) % count], rule, field) for i in range(count)]
            value = _finish_array(rule, items, lambda v: v in (None, ""))
        else:
            value = _transform(raw, rule, field)
    return value, _failed_checks(rule, value)


def _evaluate_items(record: Dict[str, Any], rule: Rule, layout: Layout) -> Tuple[Any, List[str]]:
    count = layout.field(rule.items[0][1].source).occurs
    objects: List[Dict[str, Any]] = []
    failed: List[str] = []
    members = [
        (name, replace(sub, on_error=rule.on_error), layout.field(sub.source))
        for name, sub in rule.items
    ]
    for index in range(count):
        obj: Dict[str, Any] = {}
        for name, sub, field in members:
            raw = record[sub.source][(index + sub.shift) % count]
            obj[name] = _transform(raw, sub, field)
        objects.append(obj)
    objects = _finish_array(rule, objects, lambda o: all(v in (None, "") for v in o.values()))
    for obj in objects:
        for name, sub in rule.items:
            failed.extend(_failed_checks(sub, obj[name]))
    failed.extend(_failed_checks(rule, objects))
    return objects, sorted(set(failed))


def _set_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise MappingError(f"Target path {path} collides with a value at {part}")
        node = child
    if parts[-1] in node:
        raise MappingError(f"Target path {path} is written twice")
    node[parts[-1]] = value


def get_path(doc: Dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


Outcome = Tuple[bool, Any, List[str]]  # (input accepted, value, failed check ids)


def rule_outcome(record: Dict[str, Any], rule: Rule, layout: Layout) -> Outcome:
    """Evaluate one rule. Unexpected errors propagate (they are pipeline crashes)."""
    try:
        value, failed = evaluate_rule(record, rule, layout)
    except RuleRejected:
        return False, None, [f"{rule.id}:invalid"]
    return True, value, failed


def assemble(mapping: Mapping, outcomes: Dict[str, Outcome], filtered: bool) -> Dict[str, Any]:
    """Build the output document (or marker) from per-rule outcomes."""
    key_rule = mapping.key_rule

    def marker(doc: Dict[str, Any]) -> Dict[str, Any]:
        if key_rule is not None and outcomes[key_rule.id][0]:
            _set_path(doc, key_rule.target, outcomes[key_rule.id][1])
        return doc

    if filtered:
        return marker({FILTERED: True})
    doc: Dict[str, Any] = {}
    reasons: List[str] = []
    for rule in mapping.rules:
        accepted, value, failed = outcomes[rule.id]
        reasons.extend(failed)
        if accepted:
            _set_path(doc, rule.target, value)
    if reasons:
        return marker({REJECTED: sorted(reasons)})
    return doc


def is_filtered(record: Dict[str, Any], mapping: Mapping) -> bool:
    return mapping.filter is not None and not condition_matches(record, mapping.filter)


def apply_mapping(record: Dict[str, Any], mapping: Mapping, layout: Layout) -> Dict[str, Any]:
    """Transform one decoded source record into a target document.

    Returns the document, or a marker document:
      ``{"__filtered__": true, <key>}``           record is excluded by the mapping filter
      ``{"__rejected__": ["R2:required"], <key>}`` record fails a validation
    """
    filtered = is_filtered(record, mapping)
    if filtered:
        rules = [mapping.key_rule] if mapping.key_rule else []
        outcomes = {}
        for rule in rules:
            try:
                outcomes[rule.id] = rule_outcome(record, rule, layout)
            except Exception:
                outcomes[rule.id] = (False, None, [])
        return assemble(mapping, outcomes, True)
    outcomes = {rule.id: rule_outcome(record, rule, layout) for rule in mapping.rules}
    return assemble(mapping, outcomes, False)
