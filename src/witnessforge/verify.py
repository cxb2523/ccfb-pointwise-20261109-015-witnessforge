"""Check a real pipeline's output for the witness records against the specification.

What is checked:
  * every record that should load is present, and nothing else is loaded
  * records that should be rejected or filtered out are not loaded
  * (optional) the reject file holds exactly the records that should be rejected
  * duplicate output documents, and duplicate values for rules with a ``unique`` check
  * every field, array and nested object: values, types, missing or extra paths,
    item counts, missing, extra and duplicate items, item order
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .codec import split_records
from .copybook import Layout
from .mapping import FILTERED, REJECTED, Mapping, get_path
from .pipeline import json_ready, run_pipeline

_MISSING = object()


@dataclass
class Mismatch:
    record: Optional[int]
    path: str
    rule_id: Optional[str]
    expected: Any
    actual: Any
    detail: str = ""


@dataclass
class VerifyResult:
    records: int
    loaded: int = 0
    rejected: int = 0
    filtered: int = 0
    mismatches: List[Mismatch] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.problems


def _same(expected: Any, actual: Any) -> bool:
    """Equal values of compatible types (12 == 12.0 is fine; "12" vs 12 or True vs 1 is not)."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    return type(expected) is type(actual) and expected == actual


def _token(value: Any) -> str:
    return repr(sorted(value.items())) if isinstance(value, dict) else repr(value)


def _show(value: Any) -> Any:
    return "<missing>" if value is _MISSING else value


def _diff(expected: Any, actual: Any, path: str, out: List[Tuple[str, Any, Any, str]]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            _diff(expected.get(key, _MISSING), actual.get(key, _MISSING),
                  f"{path}.{key}" if path else key, out)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        _diff_list(expected, actual, path, out)
        return
    if expected is _MISSING or actual is _MISSING:
        detail = "path missing from output" if actual is _MISSING else "unexpected path in output"
        out.append((path, _show(expected), _show(actual), detail))
    elif not _same(expected, actual):
        detail = "" if type(expected) is type(actual) else (
            f"type {type(expected).__name__} expected, got {type(actual).__name__}")
        out.append((path, expected, actual, detail))


def _diff_list(expected: list, actual: list, path: str, out) -> None:
    if len(expected) != len(actual):
        out.append((path, f"{len(expected)} items", f"{len(actual)} items", "item count differs"))
    if any(isinstance(v, (dict, list)) for v in expected + actual):
        for index in range(max(len(expected), len(actual))):
            e = expected[index] if index < len(expected) else _MISSING
            a = actual[index] if index < len(actual) else _MISSING
            if e is _MISSING or a is _MISSING:
                out.append((f"{path}[{index}]", _show(e), _show(a),
                            "item missing" if a is _MISSING else "unexpected item"))
            else:
                _diff(e, a, f"{path}[{index}]", out)
        return
    want, got = Counter(_token(v) for v in expected), Counter(_token(v) for v in actual)
    by_token = {_token(v): v for v in expected + actual}
    for token in sorted(set(want) | set(got)):
        if want[token] == got[token]:
            continue
        value = by_token[token]
        if got[token] > want[token] and want[token] > 0:
            detail = f"duplicate item: appears {got[token]} times, expected {want[token]}"
        elif got[token] > want[token]:
            detail = "unexpected item"
        else:
            detail = "item missing"
        out.append((path, f"{value!r} x{want[token]}", f"{value!r} x{got[token]}", detail))
    if want == got and [_token(v) for v in expected] != [_token(v) for v in actual]:
        out.append((path, expected, actual, "same items in a different order"))


def _rule_for(path: str, mapping: Mapping) -> Optional[str]:
    plain = re.sub(r"\[\d+\]", "", path)
    best, best_len = None, -1
    for rule in mapping.rules:
        if plain == rule.target or plain.startswith(rule.target + "."):
            rule_id = rule.id
            if rule.items and plain.startswith(rule.target + "."):
                member = plain[len(rule.target) + 1:].split(".")[0]
                if any(name == member for name, _ in rule.items):
                    rule_id = f"{rule.id}.{member}"
            if len(rule.target) > best_len:
                best, best_len = rule_id, len(rule.target)
    if best is None:  # a whole object is missing or extra: name the rules writing inside it
        inside = [r.id for r in mapping.rules if r.target.startswith(plain + ".")]
        if inside:
            return ",".join(inside)
    return best


def _compare(result: VerifyResult, mapping: Mapping, index: int, expected, actual) -> None:
    diffs: List[Tuple[str, Any, Any, str]] = []
    _diff(expected, actual, "", diffs)
    for path, e, a, detail in diffs:
        result.mismatches.append(Mismatch(index, path, _rule_for(path, mapping), e, a, detail))


def verify(
    layout: Layout,
    mapping: Mapping,
    data: bytes,
    actual_docs: List[Dict[str, Any]],
    key: Optional[str] = None,
    actual_rejects: Optional[List[Dict[str, Any]]] = None,
) -> VerifyResult:
    """Compare a pipeline's loaded documents (and optionally its rejects) with the specification.

    Documents are matched by the dotted ``key`` path (default: the target of the
    mapping's ``key`` rule), or by position when no key is available.
    """
    if key is None and mapping.key_rule is not None:
        key = mapping.key_rule.target
    records = split_records(layout, data)
    expected = [json_ready(run_pipeline(r, layout, mapping)) for r in records]
    loaded = [(i, e) for i, e in enumerate(expected) if REJECTED not in e and FILTERED not in e]
    rejected = [(i, e) for i, e in enumerate(expected) if REJECTED in e]
    filtered = [(i, e) for i, e in enumerate(expected) if FILTERED in e]
    result = VerifyResult(len(records), len(loaded), len(rejected), len(filtered))

    if key:
        index: Dict[str, List[Dict[str, Any]]] = {}
        for doc in actual_docs:
            index.setdefault(_token(get_path(doc, key, _MISSING)), []).append(doc)
        for token, docs in index.items():
            if len(docs) > 1:
                result.problems.append(
                    f"duplicate output: {key} = {get_path(docs[0], key, '<missing>')!r} "
                    f"appears {len(docs)} times")
        expected_tokens = set()
        for i, doc in loaded:
            token = _token(get_path(doc, key, _MISSING))
            expected_tokens.add(token)
            if token not in index:
                result.problems.append(
                    f"record {i}: document {key} = {get_path(doc, key)!r} is missing from the output")
            else:
                _compare(result, mapping, i, doc, index[token][0])
        for i, doc in rejected + filtered:
            token = _token(get_path(doc, key, _MISSING))
            if token in index and token not in expected_tokens:
                why = (f"rejected ({', '.join(doc[REJECTED])})" if REJECTED in doc
                       else "filtered out by the mapping filter")
                result.problems.append(
                    f"record {i}: {key} = {get_path(doc, key)!r} was loaded but should be {why}")
                expected_tokens.add(token)
        for token, docs in index.items():
            if token not in expected_tokens:
                result.problems.append(
                    f"unexpected document in output: {key} = {get_path(docs[0], key, '<missing>')!r}")
    else:
        if len(actual_docs) != len(loaded):
            result.problems.append(
                f"expected {len(loaded)} loaded documents ({len(rejected)} rejected, "
                f"{len(filtered)} filtered), got {len(actual_docs)}")
        want = Counter(_token(e) for _, e in loaded)
        got = Counter(_token(a) for a in actual_docs)
        for token, count in got.items():
            if count > max(want[token], 1):
                result.problems.append(f"duplicate output: the same document appears {count} times")
        for (i, e), a in zip(loaded, actual_docs):
            _compare(result, mapping, i, e, a)

    if actual_rejects is not None:
        if key:
            want_rejects = Counter(_token(get_path(e, key, _MISSING)) for _, e in rejected)
            got_rejects = Counter(_token(get_path(r, key, _MISSING)) for r in actual_rejects)
            for token in sorted(set(want_rejects) | set(got_rejects)):
                if want_rejects[token] != got_rejects[token]:
                    shown = token if token != repr(_MISSING) else "<no key>"
                    result.problems.append(
                        f"reject file: {key} = {shown} expected {want_rejects[token]} time(s), "
                        f"found {got_rejects[token]}")
        elif len(actual_rejects) != len(rejected):
            result.problems.append(
                f"reject file: expected {len(rejected)} rejected records, found {len(actual_rejects)}")

    for rule in mapping.rules:  # dataset-level uniqueness
        if not any(op == "unique" for op, _ in rule.checks):
            continue
        values = Counter(
            _token(v) for v in (get_path(d, rule.target, None) for d in actual_docs) if v is not None
        )
        for token, count in values.items():
            if count > 1:
                result.mismatches.append(Mismatch(
                    None, rule.target, rule.id, "unique values", f"{token} in {count} documents",
                    "duplicate value across records"))
    return result
