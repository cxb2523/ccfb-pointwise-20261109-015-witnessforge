from witnessforge.codec import decode_record
from witnessforge.mapping import mapping_from_dict
from witnessforge.pipeline import canonical, run_pipeline
from witnessforge.report import build_report, to_markdown
from witnessforge.witness import forge


def test_example_reaches_full_score_with_few_records(layout, mapping):
    result = forge(layout, mapping)
    assert result.score == 1.0
    assert not result.survivors
    assert 0 < len(result.witnesses) <= 6
    assert len(result.witnesses) < result.pool_size


def test_every_mutant_is_really_killed(layout, mapping):
    result = forge(layout, mapping)
    by_id = {m.id: m for m in result.mutants}
    for witness in result.witnesses:
        expected = canonical(run_pipeline(witness.data, layout, mapping))
        for mutant_id in witness.kills:
            mutant = by_id[mutant_id]
            assert canonical(run_pipeline(witness.data, mutant.layout, mutant.mapping)) != expected


def test_key_values_are_unique(layout, mapping):
    ids = [w.expected["claimId"] for w in forge(layout, mapping).witnesses]
    assert len(ids) == len(set(ids))


def test_witnesses_are_copybook_conformant(layout, mapping):
    for witness in forge(layout, mapping).witnesses:
        assert len(witness.data) == layout.record_length
        assert decode_record(layout, witness.data) == witness.record


def test_equivalent_mutant_is_reported_as_survivor(layout):
    # The first 'upper' is redundant, so removing or flipping it changes nothing.
    mapping = mapping_from_dict(
        {"rules": [{"id": "R1", "source": "CLAIM-ID", "target": "id", "steps": ["upper", "upper"]}]},
        layout,
    )
    result = forge(layout, mapping)
    assert result.score < 1.0
    assert {s.operator for s in result.survivors} <= {"drop_step", "case_flip"}
    assert "R1:truncate" not in {s.id for s in result.survivors}
    report = build_report(result)
    assert report["rules"][0]["survivors"]
    assert "Undetected faults" in to_markdown(report)
