"""COBOL date fields: PIC 9(8) COMP binary YYYYMMDD and PIC 9(6) zoned YYMMDD.

Covers the three constraints the cobol_date step has to balance:
  * configurable century pivot for 2-digit years (both sides of the boundary)
  * dirty out-of-range digits (20231332-style) handled by on_error: raise/skip/default
    without one bad record taking down the rest of the batch
  * decode -> encode reproduces the original copybook bytes exactly, unless the
    mapped date was explicitly rewritten (including after skip/default fired)
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from witnessforge import load_copybook, load_mapping
from witnessforge.cli import main
from witnessforge.codec import CodecError, decode_record, digits_of, encode_record
from witnessforge.copybook import Field, parse_copybook
from witnessforge.mapping import (
    MappingError, apply_mapping, apply_step, cobol_date_to_raw, mapping_from_dict,
    rebuild_source_record,
)
from witnessforge.mutants import OPERATORS, generate_mutants
from witnessforge.witness import forge

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "claims"

BIN_FIELD = Field("ADMIT-DATE-BIN", "B", 8)       # PIC 9(8) COMP, YYYYMMDD
ZONED_FIELD = Field("DOB-ZONED", "9", 6)          # PIC 9(6), YYMMDD

LAYOUT = parse_copybook(
    """
    01 R.
       05 CLAIM-ID       PIC X(6).
       05 ADMIT-DATE-BIN PIC 9(8) COMP.
       05 DOB-ZONED      PIC 9(6).
    """
)


def _mapping(rules, **extra):
    return mapping_from_dict(dict({"rules": rules}, **extra), LAYOUT)


def _record(**values):
    # DOB-ZONED = 500115 -> yy=50, mm=01, dd=15 (sits exactly on the default pivot)
    base = {"CLAIM-ID": "C0001", "ADMIT-DATE-BIN": Decimal(20240131), "DOB-ZONED": Decimal(500115)}
    base.update(values)
    return base


BIN_ARG = {
    "input_format": "%Y%m%d", "output_format": "%Y-%m-%d", "pivot": 50,
    "on_error": "raise", "default": None, "_tokens": (("Y", 4), ("m", 2), ("d", 2)),
}
ZONED_ARG_PIVOT50 = {
    "input_format": "%y%m%d", "output_format": "%Y-%m-%d", "pivot": 50,
    "on_error": "raise", "default": None, "_tokens": (("y", 2), ("m", 2), ("d", 2)),
}


# --------------------------------------------------------------------------- 1. century pivot


@pytest.mark.parametrize(
    "digits,pivot,expected",
    [
        ("500115", 50, "1950-01-15"),   # yy == pivot -> 19xx
        ("490115", 50, "2049-01-15"),   # yy == pivot - 1 -> 20xx
        ("000101", 50, "2000-01-01"),
        ("990101", 50, "1999-01-01"),
        ("300615", 30, "1930-06-15"),   # a different, configurable pivot
        ("290615", 30, "2029-06-15"),
    ],
)
def test_century_pivot_both_sides_of_the_boundary(digits, pivot, expected):
    arg = dict(ZONED_ARG_PIVOT50, pivot=pivot)
    assert apply_step(Decimal(int(digits)), "cobol_date", arg, ZONED_FIELD) == expected


def test_pivot_is_configurable_per_rule():
    mapping = _mapping([
        {"id": "A", "source": "DOB-ZONED", "target": "a",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d", "pivot": 10}}]},
        {"id": "B", "source": "DOB-ZONED", "target": "b",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d", "pivot": 90}}]},
    ])
    doc = apply_mapping(_record(**{"DOB-ZONED": Decimal(500115)}), mapping, LAYOUT)
    assert doc["a"] == "1950-01-15"  # yy=50 >= pivot 10 -> 19xx
    assert doc["b"] == "2050-01-15"  # yy=50 <  pivot 90 -> 20xx


def test_binary_4_digit_year_needs_no_pivot():
    assert apply_step(Decimal(20240131), "cobol_date", BIN_ARG, BIN_FIELD) == "2024-01-31"


def test_leading_zero_year_is_preserved_through_decimal_decoding():
    # Decimal(10115) would print as "10115" (5 digits); digits_of must zero-pad
    # back to the field's width so "01" the year isn't mistaken for 1 digit.
    assert digits_of(ZONED_FIELD, Decimal(10115)) == "010115"
    assert apply_step(Decimal(10115), "cobol_date", ZONED_ARG_PIVOT50, ZONED_FIELD) == "2001-01-15"


# --------------------------------------------------------------------------- 2. dirty data / on_error


def test_on_error_raise_rejects_one_record_without_crashing_others():
    mapping = _mapping([
        {"id": "K", "source": "CLAIM-ID", "target": "claimId", "steps": ["strip"]},
        {"id": "D", "source": "ADMIT-DATE-BIN", "target": "admission", "on_error": "reject",
         "steps": [{"cobol_date": {"input_format": "%Y%m%d", "output_format": "%Y-%m-%d"}}]},
    ], key="K")
    good = apply_mapping(_record(), mapping, LAYOUT)
    dirty = apply_mapping(_record(**{"ADMIT-DATE-BIN": Decimal(20231332)}), mapping, LAYOUT)
    assert good == {"claimId": "C0001", "admission": "2024-01-31"}
    from witnessforge.mapping import REJECTED
    assert dirty[REJECTED] == ["D:invalid"]
    assert dirty["claimId"] == "C0001"  # the rest of the record is still usable


def test_on_error_raise_without_reject_errors_only_that_record():
    """A dirty cobol_date field with the rule's default on_error ('fail') turns only
    its own record into a pipeline error; sibling records in the same batch decode
    and map normally (checked at the run_pipeline level, which is what a real batch
    driver calls per record)."""
    from witnessforge.pipeline import is_error, run_pipeline

    one_bad_mapping = _mapping([
        {"id": "D", "source": "ADMIT-DATE-BIN", "target": "admission",
         "steps": [{"cobol_date": {"input_format": "%Y%m%d", "output_format": "%Y-%m-%d"}}]},
    ])
    good_bytes = encode_record(LAYOUT, _record())
    bad_bytes = encode_record(LAYOUT, _record(**{"ADMIT-DATE-BIN": Decimal(20231332)}))
    assert not is_error(run_pipeline(good_bytes, LAYOUT, one_bad_mapping))
    assert is_error(run_pipeline(bad_bytes, LAYOUT, one_bad_mapping))
    # the batch keeps going: the next good record is unaffected by the bad one
    assert not is_error(run_pipeline(good_bytes, LAYOUT, one_bad_mapping))


def test_on_error_skip_nulls_the_field_and_keeps_loading():
    mapping = _mapping([
        {"id": "D", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                    "on_error": "skip"}}]},
    ])
    doc = apply_mapping(_record(**{"DOB-ZONED": Decimal(991332)}), mapping, LAYOUT)
    assert doc == {"dob": None}  # no reject marker: the record loaded


def test_on_error_default_substitutes_a_fixed_value():
    mapping = _mapping([
        {"id": "D", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                    "on_error": "default", "default": "1900-01-01"}}]},
    ])
    doc = apply_mapping(_record(**{"DOB-ZONED": Decimal(991332)}), mapping, LAYOUT)
    assert doc == {"dob": "1900-01-01"}
    # a valid date is unaffected by the default
    doc_ok = apply_mapping(_record(), mapping, LAYOUT)
    assert doc_ok == {"dob": "1950-01-15"}  # yy=50 >= pivot 50 -> 19xx


def test_on_error_default_requires_a_default_value():
    with pytest.raises(MappingError):
        _mapping([
            {"id": "D", "source": "DOB-ZONED", "target": "dob",
             "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                        "on_error": "default"}}]},
        ])


def test_on_error_must_be_one_of_three_strategies():
    with pytest.raises(MappingError):
        _mapping([
            {"id": "D", "source": "DOB-ZONED", "target": "dob",
             "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                        "on_error": "ignore"}}]},
        ])


# --------------------------------------------------------------------------- parse-time validation


def test_input_format_must_match_field_width():
    with pytest.raises(MappingError):
        _mapping([
            {"id": "D", "source": "DOB-ZONED", "target": "dob",  # 6-digit field, 8-digit format
             "steps": [{"cobol_date": {"input_format": "%Y%m%d", "output_format": "%Y-%m-%d"}}]},
        ])


def test_input_format_needs_year_month_and_day_tokens():
    with pytest.raises(MappingError):
        _mapping([
            {"id": "D", "source": "DOB-ZONED", "target": "dob",
             "steps": [{"cobol_date": {"input_format": "%y%m", "output_format": "%Y-%m-%d"}}]},
        ])


def test_input_format_rejects_literal_separators():
    with pytest.raises(MappingError):
        _mapping([
            {"id": "D", "source": "ADMIT-DATE-BIN", "target": "d",
             "steps": [{"cobol_date": {"input_format": "%Y-%m-%d", "output_format": "%Y-%m-%d"}}]},
        ])


def test_cobol_date_rejects_signed_or_scaled_or_text_fields():
    signed_layout = parse_copybook("01 R.\n 05 D PIC S9(8) COMP.\n")
    with pytest.raises(MappingError):
        mapping_from_dict({"rules": [
            {"id": "D", "source": "D", "target": "d",
             "steps": [{"cobol_date": {"input_format": "%Y%m%d", "output_format": "%Y-%m-%d"}}]},
        ]}, signed_layout)


# --------------------------------------------------------------------------- 3. round trip


def test_roundtrip_holds_when_the_mapped_date_is_untouched():
    mapping = _mapping([
        {"id": "D1", "source": "ADMIT-DATE-BIN", "target": "admission",
         "steps": [{"cobol_date": {"input_format": "%Y%m%d", "output_format": "%Y-%m-%d"}}]},
        {"id": "D2", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                    "on_error": "skip"}}]},
    ])
    record = _record()
    data = encode_record(LAYOUT, record)
    decoded = decode_record(LAYOUT, data)
    doc = apply_mapping(decoded, mapping, LAYOUT)
    rebuilt = rebuild_source_record(decoded, doc, mapping, LAYOUT)
    assert encode_record(LAYOUT, rebuilt) == data


def test_roundtrip_holds_for_a_skipped_invalid_date():
    """A dirty record that on_error: skip turned into null must still round-trip:
    its original (invalid) digits are kept byte for byte, not replaced by anything
    derived from the null output."""
    mapping = _mapping([
        {"id": "D2", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                    "on_error": "skip"}}]},
    ])
    record = _record(**{"DOB-ZONED": Decimal(991332)})
    data = encode_record(LAYOUT, record)
    decoded = decode_record(LAYOUT, data)
    doc = apply_mapping(decoded, mapping, LAYOUT)
    assert doc == {"dob": None}
    rebuilt = rebuild_source_record(decoded, doc, mapping, LAYOUT)
    assert encode_record(LAYOUT, rebuilt) == data


def test_roundtrip_holds_for_a_defaulted_invalid_date():
    mapping = _mapping([
        {"id": "D2", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d",
                                    "on_error": "default", "default": "1900-01-01"}}]},
    ])
    record = _record(**{"DOB-ZONED": Decimal(991332)})
    data = encode_record(LAYOUT, record)
    decoded = decode_record(LAYOUT, data)
    doc = apply_mapping(decoded, mapping, LAYOUT)
    assert doc == {"dob": "1900-01-01"}
    rebuilt = rebuild_source_record(decoded, doc, mapping, LAYOUT)
    assert encode_record(LAYOUT, rebuilt) == data  # original dirty bytes preserved, not re-derived


def test_explicit_rewrite_reencodes_with_correct_width_and_pivot_truncation():
    mapping = _mapping([
        {"id": "D2", "source": "DOB-ZONED", "target": "dob",
         "steps": [{"cobol_date": {"input_format": "%y%m%d", "output_format": "%Y-%m-%d", "pivot": 50}}]},
    ])
    record = _record()
    data = encode_record(LAYOUT, record)
    decoded = decode_record(LAYOUT, data)
    doc = apply_mapping(decoded, mapping, LAYOUT)
    assert doc == {"dob": "1950-01-15"}  # yy=50 >= pivot 50 -> 19xx

    edited = dict(doc, dob="1987-07-04")  # caller explicitly rewrites the date
    rebuilt = rebuild_source_record(decoded, edited, mapping, LAYOUT)
    new_data = encode_record(LAYOUT, rebuilt)
    assert new_data != data
    assert len(new_data) == len(data)  # width/padding unchanged
    redecoded = decode_record(LAYOUT, new_data)
    assert redecoded["DOB-ZONED"] == Decimal(870704)  # 1987 truncated back to yy=87
    # every other field keeps its original bytes
    assert redecoded["CLAIM-ID"] == decoded["CLAIM-ID"]
    assert redecoded["ADMIT-DATE-BIN"] == decoded["ADMIT-DATE-BIN"]


def test_cobol_date_to_raw_matches_decode_item_width():
    arg = {"output_format": "%Y-%m-%d", "_tokens": (("Y", 4), ("m", 2), ("d", 2))}
    assert cobol_date_to_raw("2024-01-31", arg) == Decimal(20240131)
    arg_yy = {"output_format": "%Y-%m-%d", "_tokens": (("y", 2), ("m", 2), ("d", 2))}
    assert cobol_date_to_raw("1950-01-15", arg_yy) == Decimal(500115)
    assert cobol_date_to_raw("2050-01-15", arg_yy) == Decimal(500115)  # both truncate to yy=50


# --------------------------------------------------------------------------- examples/claims coverage


@pytest.fixture(scope="module")
def dates_spec():
    layout = load_copybook(EXAMPLE / "claim_dates.cpy")
    return layout, load_mapping(EXAMPLE / "mapping_dates.yaml", layout)


@pytest.fixture(scope="module")
def dates_result(dates_spec):
    return forge(*dates_spec)


def test_examples_claim_dates_mapping_loads_and_scores_fully(dates_result):
    assert dates_result.score == 1.0, [m.id for m in dates_result.survivors]
    outcomes = {w.outcome for w in dates_result.witnesses}
    assert "rejected" in outcomes  # the on_error: reject rule (R3) is exercised


def test_cobol_date_operators_are_exercised(dates_spec):
    mutants = generate_mutants(*dates_spec)
    used = {m.operator for m in mutants}
    wanted = {op for op in OPERATORS if op.startswith("cobol_date")}
    assert wanted <= used, wanted - used


def test_cli_verify_roundtrip_mode_passes_on_claim_dates_example(capsys):
    code = main([
        "verify",
        "--copybook", str(EXAMPLE / "claim_dates.cpy"),
        "--mapping", str(EXAMPLE / "mapping_dates.yaml"),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "ROUNDTRIP PASS" in out
    assert "FAIL" not in out
    assert out.strip().splitlines()[-1].startswith("PASS:")


def test_cli_verify_still_requires_both_input_and_actual_together():
    code = main([
        "verify",
        "--copybook", str(EXAMPLE / "claim_dates.cpy"),
        "--mapping", str(EXAMPLE / "mapping_dates.yaml"),
        "--input", "whatever.dat",
    ])
    assert code == 2


def test_invalid_bytes_still_raise_codecerror_for_non_date_use():
    with pytest.raises(CodecError):
        digits_of(Field("N", "X", 2), "ab")
