from decimal import Decimal

import pytest

from witnessforge.codec import encode_record
from witnessforge.mapping import MappingError, apply_mapping, mapping_from_dict
from witnessforge.mutants import generate_mutants
from witnessforge.pipeline import run_pipeline


def test_reference_mapping(layout, mapping):
    data = encode_record(
        layout,
        {
            "CLAIM-ID": "C100",
            "MEMBER-ID": "",
            "CLAIM-TYPE": "IP",
            "SERVICE-DATE": "20240131",
            "BILLED-AMT": Decimal("1234.50"),
            "UNITS": 3,
            "DIAG-CODE": ["e119", "", "i10"],
            "PROVIDER-NAME": "General Hospital",
        },
    )
    doc = run_pipeline(data, layout, mapping)
    assert doc == {
        "claimId": "C100",
        "member": {"id": None},
        "claimType": "inpatient",
        "service": {"date": "2024-01-31"},
        "amounts": {"billed": 1234.5, "units": 3},
        "diagnoses": ["E119", "I10"],
        "provider": {"name": "GENERAL HOSPITAL"},
    }


def test_mutants_cover_expected_operators(layout, mapping):
    operators = {(m.rule_id, m.operator) for m in generate_mutants(layout, mapping)}
    for expected in [
        ("R1", "drop_step"),
        ("R4", "date_swap"),
        ("R5", "scale_loss"),
        ("R5", "sign_loss"),
        ("R7", "array_drop_last"),
        ("R7", "keep_blanks"),
        ("R8", "case_flip"),
        ("R1", "wrong_source"),
        ("R6", "offset_shift"),
        ("R3", "map_default"),
    ]:
        assert expected in operators


def test_mutant_ids_are_unique(layout, mapping):
    ids = [m.id for m in generate_mutants(layout, mapping)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"rules": [{"id": "R1", "source": "NOPE", "target": "x"}]},
        {"rules": [{"id": "R1", "source": "CLAIM-ID", "target": "x", "steps": ["explode"]}]},
        {"rules": [{"id": "R1", "source": "CLAIM-ID", "target": "x", "steps": [{"date": {}}]}]},
        {
            "rules": [
                {"id": "R1", "source": "CLAIM-ID", "target": "x"},
                {"id": "R2", "source": "MEMBER-ID", "target": "x"},
            ]
        },
    ],
)
def test_invalid_mappings_are_rejected(layout, data):
    with pytest.raises(MappingError):
        mapping_from_dict(data, layout)


def test_target_path_collision(layout):
    mapping = mapping_from_dict(
        {
            "rules": [
                {"id": "A", "source": "CLAIM-ID", "target": "x"},
                {"id": "B", "source": "MEMBER-ID", "target": "x.y"},
            ]
        },
        layout,
    )
    record = {f.name: ([""] * f.occurs if f.occurs > 1 else "") for f in layout.fields}
    with pytest.raises(MappingError):
        apply_mapping(record, mapping, layout)
