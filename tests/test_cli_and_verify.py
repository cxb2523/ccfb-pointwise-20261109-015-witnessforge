import json

from witnessforge.cli import main


def _generate(example_paths, tmp_path, *extra):
    cpy, yml = example_paths
    return main(["generate", str(cpy), str(yml), "-o", str(tmp_path), *extra])


def test_generate_writes_all_outputs(example_paths, tmp_path):
    assert _generate(example_paths, tmp_path) == 0
    for name in ["witnesses.dat", "witnesses.jsonl", "expected.jsonl", "report.json", "report.md"]:
        assert (tmp_path / name).exists()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"]["score"] == 1.0


def test_verify_passes_on_correct_output(example_paths, tmp_path, capsys):
    _generate(example_paths, tmp_path)
    cpy, yml = example_paths
    code = main(["verify", str(cpy), str(yml), "--input", str(tmp_path / "witnesses.dat"),
                 "--actual", str(tmp_path / "expected.jsonl")])
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_verify_attributes_a_bug_to_its_rule(example_paths, tmp_path, capsys):
    _generate(example_paths, tmp_path)
    docs = [json.loads(l) for l in (tmp_path / "expected.jsonl").read_text().splitlines()]
    for doc in docs:  # simulate a pipeline that loses the implied decimal
        billed = doc["amounts"]["billed"]
        doc["amounts"]["billed"] = round(billed * 100) if billed else billed
    buggy = tmp_path / "actual.jsonl"
    buggy.write_text("\n".join(json.dumps(d) for d in docs) + "\n")
    cpy, yml = example_paths
    code = main(["verify", str(cpy), str(yml), "--input", str(tmp_path / "witnesses.dat"),
                 "--actual", str(buggy)])
    out = capsys.readouterr().out
    assert code == 1
    assert "rule R5" in out


def test_verify_with_key_handles_reordering(example_paths, tmp_path):
    _generate(example_paths, tmp_path)
    lines = (tmp_path / "expected.jsonl").read_text().splitlines()
    shuffled = tmp_path / "shuffled.jsonl"
    shuffled.write_text("\n".join(reversed(lines)) + "\n")
    cpy, yml = example_paths
    for extra in ([], ["--key", "claimId"]):  # mapping key is the default
        assert main(["verify", str(cpy), str(yml), "--input", str(tmp_path / "witnesses.dat"),
                     "--actual", str(shuffled), *extra]) == 0


def test_verify_reports_missing_documents(example_paths, tmp_path, capsys):
    _generate(example_paths, tmp_path)
    lines = (tmp_path / "expected.jsonl").read_text().splitlines()
    partial = tmp_path / "partial.jsonl"
    partial.write_text("\n".join(lines[1:]) + "\n")
    cpy, yml = example_paths
    assert main(["verify", str(cpy), str(yml), "--input", str(tmp_path / "witnesses.dat"),
                 "--actual", str(partial)]) == 1
    assert "PROBLEM" in capsys.readouterr().out


def test_min_score_gate_and_input_errors(example_paths, tmp_path):
    cpy, _ = example_paths
    weak = tmp_path / "weak.yaml"
    weak.write_text("rules:\n  - {id: R1, source: CLAIM-ID, target: id, steps: [upper, upper]}\n")
    assert main(["generate", str(cpy), str(weak), "-o", str(tmp_path / "o")]) == 1
    assert main(["generate", str(cpy), str(weak), "-o", str(tmp_path / "o"), "--min-score", "0.5"]) == 0
    assert main(["generate", str(cpy), str(tmp_path / "missing.yaml")]) == 2
