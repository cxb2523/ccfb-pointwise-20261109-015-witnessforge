"""End-to-end ETL scenarios on the eligibility example: generation coverage, and verify
catching realistic pipeline defects (duplicates, arrays, objects, rejects, filters)."""

import json
from pathlib import Path

import pytest

from witnessforge import forge, load_copybook, load_mapping, verify
from witnessforge.mapping import REJECTED, get_path
from witnessforge.mutants import OPERATORS
from witnessforge.pipeline import json_ready

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "eligibility"


@pytest.fixture(scope="module")
def spec():
    layout = load_copybook(EXAMPLE / "member.cpy")
    return layout, load_mapping(EXAMPLE / "mapping.yaml", layout)


@pytest.fixture(scope="module")
def result(spec):
    return forge(*spec)


@pytest.fixture
def outputs(result):
    loaded = [json_ready(w.expected) for w in result.witnesses if w.outcome == "loaded"]
    rejects = [json_ready(w.expected) for w in result.witnesses if w.outcome == "rejected"]
    return b"".join(w.data for w in result.witnesses), loaded, rejects


def _check(spec, data, loaded, rejects=None):
    return verify(*spec, data, loaded, actual_rejects=rejects)


def test_full_coverage(result):
    assert result.score == 1.0, [m.id for m in result.survivors]
    outcomes = {w.outcome for w in result.witnesses}
    assert outcomes == {"loaded", "rejected", "filtered"}


def test_every_operator_is_exercised(result):
    used = {m.operator for m in result.mutants}
    assert set(OPERATORS) <= used, set(OPERATORS) - used


def test_loaded_keys_are_unique(result):
    keys = [w.expected["memberId"] for w in result.witnesses if w.outcome == "loaded"]
    assert len(keys) == len(set(keys))


def test_correct_pipeline_passes(spec, outputs):
    data, loaded, rejects = outputs
    assert _check(spec, data, loaded, rejects).ok


def test_duplicate_document(spec, outputs):
    data, loaded, _ = outputs
    res = _check(spec, data, loaded + [loaded[0]])
    assert any("duplicate output" in p for p in res.problems)
    assert any(m.detail == "duplicate value across records" for m in res.mismatches)


def test_missing_document(spec, outputs):
    data, loaded, _ = outputs
    res = _check(spec, data, loaded[1:])
    assert any("missing from the output" in p for p in res.problems)


def test_rejected_record_was_loaded(spec, outputs):
    data, loaded, rejects = outputs
    leaked = next(r for r in rejects if r.get("memberId"))
    bogus = dict(loaded[0], memberId=leaked["memberId"])
    res = _check(spec, data, loaded + [bogus])
    assert any("should be rejected" in p for p in res.problems)


def test_reject_file_incomplete(spec, outputs):
    data, loaded, rejects = outputs
    res = _check(spec, data, loaded, rejects[1:])
    assert any(p.startswith("reject file") for p in res.problems)


def _mutate(loaded, fn):
    docs = json.loads(json.dumps(loaded))
    target = next(d for d in docs if len(d["programs"]) >= 2 and len(d["diagnoses"]) >= 2)
    fn(target)
    return docs


def _details(res):
    return {(m.rule_id, m.detail) for m in res.mismatches}


def test_duplicate_array_item(spec, outputs):
    data, loaded, _ = outputs
    docs = _mutate(loaded, lambda d: d["programs"].append(d["programs"][0]))
    details = _details(_check(spec, data, docs))
    assert ("E15", "item count differs") in details
    assert any(r == "E15" and d.startswith("duplicate item") for r, d in details)


def test_array_item_missing_and_order(spec, outputs):
    data, loaded, _ = outputs
    details = _details(_check(spec, data, _mutate(loaded, lambda d: d["programs"].pop())))
    assert ("E15", "item missing") in details
    details = _details(_check(spec, data, _mutate(loaded, lambda d: d["programs"].reverse())))
    assert ("E15", "same items in a different order") in details


def test_object_member_and_object_count(spec, outputs):
    data, loaded, _ = outputs

    def swap(d):
        d["diagnoses"][0]["qualifier"], d["diagnoses"][1]["qualifier"] = (
            d["diagnoses"][1]["qualifier"], d["diagnoses"][0]["qualifier"])

    details = _details(_check(spec, data, _mutate(loaded, swap)))
    assert ("E16.qualifier", "") in details
    details = _details(_check(spec, data, _mutate(loaded, lambda d: d["diagnoses"].pop())))
    assert ("E16", "item count differs") in details and ("E16", "item missing") in details


def test_missing_path_extra_path_and_type(spec, outputs):
    data, loaded, _ = outputs

    def damage(d):
        del d["address"]
        d["extra"] = 1
        d["household"]["size"] = str(d["household"]["size"])

    details = _details(_check(spec, data, _mutate(loaded, damage)))
    assert ("E05", "path missing from output") in details
    assert (None, "unexpected path in output") in details
    assert ("E13", "type int expected, got str") in details


def test_rejects_carry_reasons_and_key(result):
    rejects = [w.expected for w in result.witnesses if w.outcome == "rejected"]
    assert all(r[REJECTED] for r in rejects)
    assert all(get_path(r, "memberId") is not None for r in rejects)
