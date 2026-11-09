"""Command-line interface: ``witnessforge generate | mutants | verify``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .codec import CodecError, encode_record
from .copybook import CopybookError, load_copybook
from .mapping import MappingError, apply_mapping, get_path, load_mapping, rebuild_source_record
from .mutants import generate_mutants
from .pipeline import json_ready
from .report import build_report, to_markdown
from .verify import verify
from .witness import build_pool, forge

EXIT_OK, EXIT_FAIL, EXIT_INPUT = 0, 1, 2


def _resolve(positional: Optional[str], option: Optional[str], label: str) -> str:
    value = option or positional
    if not value:
        raise SystemExit(f"error: {label} is required (positional or --{label})")
    return value


def _load(args):
    copybook_path = _resolve(args.copybook, getattr(args, "copybook_opt", None), "copybook")
    mapping_path = _resolve(args.mapping, getattr(args, "mapping_opt", None), "mapping")
    layout = load_copybook(copybook_path)
    return layout, load_mapping(mapping_path, layout)


def _write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(json_ready(row), sort_keys=True) + "\n")


def cmd_generate(args) -> int:
    layout, mapping = _load(args)
    result = forge(layout, mapping)
    report = build_report(result)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    witnesses = result.witnesses
    (out / "witnesses.dat").write_bytes(b"".join(w.data for w in witnesses))
    _write_jsonl(out / "witnesses.jsonl", (w.record for w in witnesses))
    _write_jsonl(out / "expected.jsonl", (w.expected for w in witnesses if w.outcome == "loaded"))
    _write_jsonl(out / "expected_rejects.jsonl", (
        dict(w.expected, record=i) for i, w in enumerate(witnesses) if w.outcome == "rejected"))
    _write_jsonl(out / "expected_filtered.jsonl", (
        dict(w.expected, record=i) for i, w in enumerate(witnesses) if w.outcome == "filtered"))
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(to_markdown(report), encoding="utf-8")

    s = report["summary"]
    print(
        f"WitnessForge {__version__}: {s['witness_records']} witness records detect "
        f"{s['killed']}/{s['mutants']} mapping faults (score {s['score']:.0%})."
    )
    print(f"  {s['loaded']} should load, {s['rejected']} should be rejected, "
          f"{s['filtered']} should be filtered out.")
    for rule in report["rules"]:
        for survivor in rule["survivors"]:
            print(f"  undetected: {survivor['id']} - {survivor['description']}")
    print(f"Output written to {out}/")
    if result.score + 1e-9 < args.min_score:
        print(f"FAIL: score {result.score:.0%} is below --min-score {args.min_score:.0%}",
              file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def cmd_mutants(args) -> int:
    layout, mapping = _load(args)
    for mutant in generate_mutants(layout, mapping):
        print(f"{mutant.id:32} {mutant.description}")
    return EXIT_OK


def cmd_verify_roundtrip(layout, mapping) -> int:
    """Self-contained check (no external pipeline output needed): for a pool of
    copybook-conformant witness records, decode -> map -> rebuild -> encode must
    reproduce the original bytes exactly, unless a cobol_date field is explicitly
    rewritten in the mapped document (which this check never does, so every
    cobol_date field -- including ones skipped or defaulted on invalid digits --
    is expected to round-trip untouched)."""
    pool = build_pool(layout, mapping)
    key = mapping.key_rule.target if mapping.key_rule else None
    failures = 0
    for index, candidate in enumerate(pool):
        record = candidate.record
        doc = apply_mapping(record, mapping, layout)
        rebuilt = rebuild_source_record(record, doc, mapping, layout)
        try:
            data2 = encode_record(layout, rebuilt)
        except CodecError as exc:
            print(f"ROUNDTRIP FAIL record {index}: re-encode raised {exc}")
            failures += 1
            continue
        if data2 != candidate.data:
            print(f"ROUNDTRIP FAIL record {index}: decode->encode changed the bytes")
            failures += 1
            continue
        tag = f" {key}={get_path(doc, key)!r}" if key else ""
        print(
            f"ROUNDTRIP PASS record {index}{tag}: {len(candidate.data)} bytes, "
            f"decode->encode reproduced the original copybook record exactly."
        )
    ok = failures == 0
    print(f"{'PASS' if ok else 'FAIL'}: {len(pool)} witness records round-tripped "
          f"({len(pool) - failures} ok, {failures} failed).")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_verify(args) -> int:
    layout, mapping = _load(args)
    if not args.input and not args.actual:
        return cmd_verify_roundtrip(layout, mapping)
    if not args.input or not args.actual:
        print("error: --input and --actual must be given together", file=sys.stderr)
        return EXIT_INPUT
    data = Path(args.input).read_bytes()
    actual = [
        json.loads(line)
        for line in Path(args.actual).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rejects = None
    if args.rejects:
        rejects = [
            json.loads(line)
            for line in Path(args.rejects).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    result = verify(layout, mapping, data, actual, key=args.key, actual_rejects=rejects)
    for problem in result.problems:
        print(f"PROBLEM: {problem}")
    for m in result.mismatches:
        where = f"record {m.record} " if m.record is not None else ""
        note = f" [{m.detail}]" if m.detail else ""
        print(
            f"MISMATCH {where}{m.path} (rule {m.rule_id or '?'}): "
            f"expected {m.expected!r}, got {m.actual!r}{note}"
        )
    if result.ok:
        print(f"PASS: {result.records} records checked ({result.loaded} loaded, "
              f"{result.rejected} rejected, {result.filtered} filtered) and match the specification.")
        return EXIT_OK
    rules = sorted({m.rule_id for m in result.mismatches if m.rule_id})
    print(f"FAIL: {len(result.mismatches)} mismatches; rules involved: {', '.join(rules) or '-'}")
    return EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="witnessforge",
        description="Generate the smallest copybook-conformant test dataset that provably "
        "detects mapping-rule faults in an ETL pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("copybook", nargs="?",
                        help="COBOL copybook (.cpy) describing the source record")
        p.add_argument("mapping", nargs="?", help="mapping specification (.yaml)")
        p.add_argument("--copybook", dest="copybook_opt", metavar="PATH",
                        help="same as the positional copybook argument")
        p.add_argument("--mapping", dest="mapping_opt", metavar="PATH",
                        help="same as the positional mapping argument")

    gen = sub.add_parser("generate", help="build the witness dataset and coverage report")
    common(gen)
    gen.add_argument("-o", "--out", default="witnessforge-out", help="output directory")
    gen.add_argument("--min-score", type=float, default=1.0,
                     help="fail (exit 1) if the mutation score is below this (0-1, default 1.0)")
    gen.set_defaults(func=cmd_generate)

    mut = sub.add_parser("mutants", help="list the mapping faults that will be checked")
    common(mut)
    mut.set_defaults(func=cmd_mutants)

    ver = sub.add_parser("verify", help="check your pipeline's output for the witness records")
    common(ver)
    ver.add_argument("--input", help="witnesses.dat that was fed to the pipeline "
                     "(omit together with --actual to run a self-contained decode/encode "
                     "round-trip check instead)")
    ver.add_argument("--actual", help="pipeline output, one JSON document per line")
    ver.add_argument("--key", help="dotted path used to match documents "
                     "(default: the mapping's key rule; otherwise by position)")
    ver.add_argument("--rejects", help="the pipeline's reject file, one JSON document per line, "
                     "each holding the key path")
    ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CopybookError, MappingError, CodecError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
