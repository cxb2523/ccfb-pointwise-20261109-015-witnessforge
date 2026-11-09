from decimal import Decimal

import pytest

from witnessforge import load_mapping
from witnessforge.copybook import Field, parse_copybook
from witnessforge.mapping import (
    FILTERED, REJECTED, MappingError, apply_mapping, apply_step, check_passes, mapping_from_dict,
)
from witnessforge.regexgen import matching, near_miss

TEXT = Field("T", "X", 10)


@pytest.mark.parametrize(
    "value,op,arg,expected",
    [
        ("  ab  ", "lstrip", None, "ab  "),
        ("ABCDEFGH", "substring", {"start": 2, "length": 3}, "BCD"),
        ("000120", "lstrip_zeros", None, "120"),
        ("000", "lstrip_zeros", None, "0"),
        ("42", "pad", {"length": 5, "char": "0", "side": "left"}, "00042"),
        ("42", "pad", {"length": 4, "char": "*", "side": "right"}, "42**"),
        ("Y", "boolean", {"true": ["Y"], "false": ["N"], "default": None}, True),
        ("N", "boolean", {"true": ["Y"], "false": ["N"], "default": None}, False),
        ("X", "boolean", {"true": ["Y"], "false": ["N"], "default": None}, None),
        (Decimal("1234"), "divide", Decimal(100), Decimal("12.34")),
        (Decimal("2.5"), "multiply", Decimal(4), Decimal("10.0")),
        (Decimal("2.345"), "round", 2, Decimal("2.35")),
        ("5551234567", "replace", {"pattern": r"(\d{3})(\d{3})(\d{4})", "with": r"\1-\2-\3"},
         "555-123-4567"),
        ("00000000", "date", {"in": "%Y%m%d", "out": "%Y-%m-%d", "nulls": ["00000000"]}, None),
    ],
)
def test_steps(value, op, arg, expected):
    assert apply_step(value, op, arg, TEXT) == expected


@pytest.mark.parametrize(
    "value,op,arg,ok",
    [
        (None, "required", None, False),
        ("  ", "required", None, False),
        ("x", "required", None, True),
        ("abc", "min_length", 3, True),
        ("ab", "min_length", 3, False),
        ("abcd", "max_length", 3, False),
        (5, "min", Decimal(5), True),
        (4.99, "min", Decimal(5), False),
        (10.01, "max", Decimal(10), False),
        ("M123", "pattern", r"M\d+", True),
        ("X123", "pattern", r"M\d+", False),
        ("IP", "allowed", ("IP", "OP"), True),
        ("RX", "allowed", ("IP", "OP"), False),
        (None, "pattern", r"M\d+", True),  # only 'required' fails on null
    ],
)
def test_checks(value, op, arg, ok):
    assert check_passes(value, op, arg) is ok


LAYOUT = parse_copybook(
    """
    01 R.
       05 TYPE   PIC X(1).
       05 ID     PIC X(5).
       05 FIRST  PIC X(6).
       05 LAST   PIC X(6).
       05 CODE   PIC X(3) OCCURS 3 TIMES.
       05 QUAL   PIC X(1) OCCURS 3 TIMES.
       05 AMT    PIC S9(5)V99 COMP-3.
    """
)


def _record(**values):
    base = {"TYPE": "D", "ID": "A1", "FIRST": "ann", "LAST": "lee", "CODE": ["X", "Y", ""],
            "QUAL": ["P", "S", ""], "AMT": Decimal("10.00")}
    base.update(values)
    return base


def _mapping(rules, **extra):
    return mapping_from_dict(dict({"rules": rules}, **extra), LAYOUT)


def test_concat_constant_condition_and_filter():
    mapping = _mapping(
        [
            {"id": "A", "source": ["FIRST", "LAST"], "separator": " ", "target": "name", "steps": ["upper"]},
            {"id": "B", "value": "MMIS", "target": "system"},
            {"id": "C", "source": "AMT", "target": "amt", "steps": ["number"],
             "when": {"field": "TYPE", "in": ["D"]}, "else": 0},
        ],
        filter={"field": "ID", "not_blank": True},
    )
    assert apply_mapping(_record(), mapping, LAYOUT) == {"name": "ANN LEE", "system": "MMIS", "amt": 10.0}
    assert apply_mapping(_record(TYPE="T"), mapping, LAYOUT)["amt"] == 0
    assert FILTERED in apply_mapping(_record(ID=""), mapping, LAYOUT)


def test_array_of_objects_dedupe_and_item_checks():
    mapping = _mapping([
        {"id": "D", "target": "dx", "items": {"code": "CODE", "qual": {"source": "QUAL"}},
         "drop_blank": True, "checks": [{"max_items": 2}, "unique_items"]},
        {"id": "P", "source": "CODE", "target": "codes", "steps": ["strip"], "drop_blank": True,
         "distinct": True},
    ])
    doc = apply_mapping(_record(), mapping, LAYOUT)
    assert doc["dx"] == [{"code": "X", "qual": "P"}, {"code": "Y", "qual": "S"}]
    assert apply_mapping(_record(CODE=["X", "X", "Y"]), mapping, LAYOUT)[REJECTED] == ["D:max_items"]
    duplicate = apply_mapping(_record(CODE=["X", "X", ""], QUAL=["P", "P", ""]), mapping, LAYOUT)
    assert duplicate[REJECTED] == ["D:unique_items"]
    assert apply_mapping(_record(CODE=["X", "X", "Y"], QUAL=["P", "Q", "R"]), _mapping([
        {"id": "P", "source": "CODE", "target": "codes", "steps": ["strip"], "distinct": True}
    ]), LAYOUT) == {"codes": ["X", "Y"]}


def test_on_error_reject_and_key_on_markers():
    mapping = _mapping(
        [
            {"id": "K", "source": "ID", "target": "id", "steps": ["strip"]},
            {"id": "D", "source": "FIRST", "target": "d", "on_error": "reject",
             "steps": [{"date": {"in": "%Y%m%d", "out": "%Y"}}]},
        ],
        key="K",
    )
    assert apply_mapping(_record(), mapping, LAYOUT) == {REJECTED: ["D:invalid"], "id": "A1"}


@pytest.mark.parametrize(
    "rules",
    [
        [{"id": "A", "source": "ID", "target": "x", "checks": [{"max_items": 2}]}],
        [{"id": "A", "source": "ID", "target": "x", "distinct": True}],
        [{"id": "A", "source": ["CODE", "ID"], "target": "x"}],
        [{"id": "A", "target": "x", "items": {"a": "CODE", "b": "ID"}}],
        [{"id": "A", "source": "ID", "target": "x", "when": {"field": "CODE", "equals": "X"}}],
        [{"id": "A", "source": "ID", "target": "x", "checks": [{"pattern": "("}]}],
        [{"id": "A", "target": "x"}],
    ],
)
def test_invalid_rule_shapes(rules):
    with pytest.raises(MappingError):
        _mapping(rules)


def test_yaml_codes_like_no_stay_strings(tmp_path):
    spec = tmp_path / "m.yaml"
    spec.write_text(
        "rules:\n"
        "  - id: A\n    source: ID\n    target: x\n"
        "    steps: [{map: {values: {NO: norway, YES: y, ON: on}}}]\n"
        "  - id: B\n    source: TYPE\n    target: y\n"
        "    steps: [{boolean: {true: [Y], false: [N]}}]\n"
    )
    mapping = load_mapping(spec, LAYOUT)
    assert set(mapping.rule("A").steps[0][1]["values"]) == {"NO", "YES", "ON"}


@pytest.mark.parametrize("pattern", [r"^\d{10}$", r"[A-Z]{2}\d{3}", r"(IP|OP)", r"\(\d{3}\) \d{3}-\d{4}"])
def test_regex_sampler(pattern):
    import re
    sample = matching(pattern)
    assert sample and re.fullmatch(pattern, sample)
    miss = near_miss(pattern, sample)
    assert miss and not re.fullmatch(pattern, miss)
