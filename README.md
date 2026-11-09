# WitnessForge

**Generate the smallest test dataset that provably catches broken data-mapping rules in your ETL pipeline.**

WitnessForge reads a COBOL copybook and a source-to-target mapping specification, generates realistic
faults for every mapping rule, and builds a small set of copybook-conformant EBCDIC records
("witnesses") that detects each fault. It then reports, rule by rule, which faults your test data would
catch, gates your CI/CD pipeline on that score, and verifies your real pipeline's output: documents,
rejects, filters, duplicates, arrays and nested objects.

```
$ witnessforge generate examples/eligibility/member.cpy examples/eligibility/mapping.yaml -o out
WitnessForge 0.1.0: 27 witness records detect 163/163 mapping faults (score 100%).
  15 should load, 11 should be rejected, 1 should be filtered out.
```

27 synthetic records cover 17 mapping rules and 37 kinds of fault, with no production data.

---

## The problem

Teams migrating mainframe data (fixed-width EBCDIC files described by COBOL copybooks) into modern
stores such as JSON documents usually test their transformation jobs in one of two ways:

| Approach | Problem |
|---|---|
| Copy of production data | Millions of records, slow CI runs, high compute cost, and real personal or health data in test environments |
| Hand-made test files | Fast, but nobody knows whether they exercise the tricky cases |

Either way, there is no **evidence of coverage**. A test suite can be green while a broken rule ships,
because no test record happens to trigger it.

Example: `BILLED-AMT PIC S9(7)V99 COMP-3` should become `amounts.billed = 12345.67`. A job that ignores
the implied decimal produces `1234567`. If every test record has an amount of zero, the broken job and
the correct one produce identical output, and the bug ships.

## How it works

1. **Parse** the copybook into a byte-level layout (text, zoned decimal, COMP-3, binary COMP, OCCURS).
2. **Generate faults (mutants)**: for every rule, create realistic faulty versions of the pipeline.
   Each one contains exactly one fault in exactly one rule (see the fault catalogue below).
3. **Build candidate records** from what each rule needs: parseable and unparseable formats, validation
   boundaries (min - 1, min, max, max + 1, lengths, item counts), pattern matches and near misses,
   allowed and disallowed codes, blanks, sentinel values, duplicates and element layouts for arrays,
   and values that switch conditions and filters on and off. Every candidate is valid for the copybook
   by construction.
4. **Execute** the reference pipeline and every fault on every candidate at the byte level. A candidate
   *detects* a fault when the outputs differ.
5. **Minimize** with greedy set cover: keep the fewest records that detect every detectable fault.
6. **Report and gate**: per-rule score, undetected faults, and a non-zero exit code below your threshold.

Then feed `witnesses.dat` to your real pipeline (AWS Glue, Spark, anything) and run `witnessforge verify`
on what it loaded and what it rejected.

## ETL scenario coverage

| Area | Scenario | How WitnessForge covers it |
|---|---|---|
| **Field decoding** | Text, zoned decimal, packed decimal (COMP-3), binary (COMP/COMP-4/COMP-5/BINARY) | Encoder and decoder; faults: offset shift, lost implied decimal, lost sign, little-endian binary |
| | Wrong source field, truncation | `wrong_source`, `truncate` faults |
| **Transformations** | Trim, case, substring, padding, leading zeros | `strip` `rstrip` `lstrip` `upper` `lower` `substring` `pad` `lstrip_zeros` |
| | Type conversion | `number` (decimal places kept), `boolean` (Y/N codes) |
| | Dates | `date` with input/output formats and sentinel nulls (`00000000`); faults: day/month swap, sentinel ignored |
| | Code lookups and defaults | `map` with default, `default` |
| | Arithmetic | `multiply`, `divide`, `round` (for example cents to dollars) |
| | Reformatting | `replace` (regular expression, for example phone formatting) |
| | Null handling | `null_if_blank`; fault: blank loaded as `""` instead of null |
| | Constants and derived fields | `value:` constants, multi-field concatenation with separator |
| | Conditional mapping | `when:` with `else:` |
| **Validations** | Required / not null | `required` |
| | Length | `min_length`, `max_length` (boundary values tested) |
| | Numeric range | `min`, `max` (boundaries tested at output precision, after scaling and rounding) |
| | Format | `pattern` (matching values and near misses generated automatically) |
| | Allowed values / domain | `allowed` |
| | Invalid input handling | `on_error: reject`; fault: invalid input silently loaded as null |
| **Records** | Filters (which records load) | mapping-level `filter:`; faults: filter ignored or inverted |
| | Rejects | rejected records carry reasons; `verify --rejects` checks the reject file |
| | Missing, extra and duplicate output documents | `verify`, matched by key |
| | Duplicate values across records | `unique` check, verified across the whole output |
| **Arrays (OCCURS)** | Blank elements, duplicates | `drop_blank`, `distinct`; faults: blanks or duplicates kept |
| | Element count | `min_items`, `max_items`, `required` (non-empty) |
| | Lost or misaligned elements | faults: last element lost, only first kept, element i reads i+1 |
| | Duplicate items | `unique_items`; verify reports duplicate, missing and extra items, and order changes |
| **Objects** | Nested objects | dotted targets (`member.address.zip5`); verify reports missing and extra paths |
| | Arrays of objects | `items:` builds `[{code, qualifier}, ...]` from parallel OCCURS fields; faults: members swapped, misaligned elements, blank objects kept |

### Not covered by this version

These need a different kind of test than per-record mapping checks, and are on the roadmap:

- **Cross-table referential integrity** (every claim's member exists in the member table)
- **Aggregates and reconciliation** (totals and counts per batch, source-to-target control totals)
- **Change data capture and history** (SCD type 2, incremental loads, deletes)
- **Cross-field rules** (end date after start date), **REDEFINES**, **OCCURS DEPENDING ON**
- **Performance and volume** testing

Copybook constructs that are not supported fail loudly instead of being misread.

## Install

```bash
pip install witnessforge          # once published to PyPI
# or from source
git clone https://github.com/skavuri143/witnessforge.git
cd witnessforge
pip install -e ".[dev]"
```

Requires Python 3.9+ and PyYAML.

## Quick start

```bash
# 1. See which faults will be checked
witnessforge mutants examples/eligibility/member.cpy examples/eligibility/mapping.yaml

# 2. Build the witness dataset and coverage report
witnessforge generate examples/eligibility/member.cpy examples/eligibility/mapping.yaml -o out

# 3. Run your real pipeline on out/witnesses.dat, then check what it loaded and rejected:
witnessforge verify examples/eligibility/member.cpy examples/eligibility/mapping.yaml \
    --input out/witnesses.dat --actual loaded.jsonl --rejects rejects.jsonl
```

`generate` writes:

| File | Contents |
|---|---|
| `witnesses.dat` | Fixed-length EBCDIC records (RECFM=F), ready to land in S3 or a mainframe dataset |
| `witnesses.jsonl` | The same records, decoded and human-readable |
| `expected.jsonl` | The documents a correct pipeline must load |
| `expected_rejects.jsonl` | Records a correct pipeline must reject, with the failing checks |
| `expected_filtered.jsonl` | Records a correct pipeline must skip |
| `report.json` / `report.md` | Score per rule, undetected faults, what each record detects |

`verify` output for a pipeline with five defects: a document written twice, a record loaded that should
have been rejected (and is missing from the reject file), array items not de-duplicated, and two
diagnosis objects with their qualifiers misaligned:

```
PROBLEM: duplicate output: memberId = 'M012345082' appears 2 times
PROBLEM: record 2: memberId = 'HIJKLMN030' was loaded but should be rejected (E01:pattern, E05:pattern, E11:min, E16.qualifier:allowed)
PROBLEM: reject file: memberId = 'HIJKLMN030' expected 1 time(s), found 0
MISMATCH record 0 diagnoses[0].qualifier (rule E16.qualifier): expected 'AB', got 'BK'
MISMATCH record 0 diagnoses[1].qualifier (rule E16.qualifier): expected 'BK', got 'AB'
MISMATCH record 0 programs (rule E15): expected '3 items', got '4 items' [item count differs]
MISMATCH record 0 programs (rule E15): expected "'CHP' x1", got "'CHP' x2" [duplicate item: appears 2 times, expected 1]
MISMATCH memberId (rule E01): expected 'unique values', got "'M012345082' in 2 documents" [duplicate value across records]
FAIL: 5 mismatches; rules involved: E01, E15, E16.qualifier
```

It also reports missing documents, unexpected documents, filtered records that were loaded, missing or
extra fields and objects, wrong types (`"12"` instead of `12`), and array items that are missing or
out of order.

Exit codes: `0` pass, `1` score below threshold or verification failed, `2` invalid input.

## Mapping specification

```yaml
name: member-eligibility
key: E01                                 # rule identifying a document (unique per witness)
filter: {field: REC-TYPE, equals: D}     # only these records are loaded

rules:
  - id: E01
    source: MEMBER-ID                    # copybook field
    target: memberId                     # dotted JSON path
    steps: [strip]                       # transformations, in order
    checks: [required, {pattern: '^M[0-9]{9}$'}, unique]

  - id: E02                              # concatenation
    source: [FIRST-NAME, LAST-NAME]
    separator: " "
    target: name.full

  - id: E04                              # dates, sentinel nulls, reject invalid input
    source: BIRTH-DATE
    target: birthDate
    steps: [{date: {in: "%Y%m%d", out: "%Y-%m-%d", nulls: ["00000000"]}}]
    checks: [required]
    on_error: reject

  - id: E10                              # binary COMP cents to dollars, range
    source: PREMIUM-CENTS
    target: premium.monthly
    steps: [{divide: 100}, number]
    checks: [{min: 0}, {max: 5000}]

  - id: E14                              # conditional mapping
    source: TERM-DATE
    target: coverage.termDate
    when: {field: ACTIVE-FLAG, equals: N}     # also: in: [...], not_blank: true, blank: true
    else: null
    steps: [{date: {in: "%Y%m%d", out: "%Y-%m-%d"}}]

  - id: E15                              # OCCURS -> array
    source: PROGRAM-CODE
    target: programs
    steps: [strip, upper]
    drop_blank: true
    distinct: true
    checks: [required, {max_items: 3}, {allowed: [CHP, MED, LTC]}]

  - id: E16                              # parallel OCCURS -> array of objects
    target: diagnoses
    items:
      code: {source: DIAG-CODE, steps: [strip, upper]}
      qualifier: {source: DIAG-QUAL, steps: [strip], checks: [{allowed: [AB, BK]}]}
    drop_blank: true
    checks: [unique_items]

  - id: E17                              # constant
    value: MMIS
    target: sourceSystem
```

The complete example, with every feature, is in [`examples/eligibility`](examples/eligibility).

### Steps

| Step | Behaviour |
|---|---|
| `strip`, `rstrip`, `lstrip`, `upper`, `lower` | String operations |
| `substring: {start, length}` | 1-based, like SQL `SUBSTR` |
| `pad: {length, char, side}` | Pad to a length on the `left` or `right` |
| `lstrip_zeros` | `000120` becomes `120`, `000` becomes `0` |
| `null_if_blank` | Blank becomes `null` |
| `number` | Decimal to JSON number (decimal places kept) |
| `boolean: {true: [...], false: [...], default}` | Code to `true`/`false` |
| `date: {in, out, nulls}` | Reformat a date (`strftime` formats); blanks and `nulls` become `null` |
| `map: {values, default}` | Code lookup |
| `multiply: n`, `divide: n`, `round: places` | Arithmetic (put `number` last) |
| `replace: {pattern, with}` | Regular-expression replace |
| `default: value` | Replace `null` with a value |

### Checks

A record that fails any check is **rejected**; it does not load, and it appears in
`expected_rejects.jsonl` with the failing checks.

| Check | Applies to |
|---|---|
| `required` | Value is not blank or null (arrays: at least one item) |
| `min_length`, `max_length` | Text length |
| `min`, `max` | Numeric value |
| `pattern` | Regular expression (full match) |
| `allowed: [...]` | Domain of valid values |
| `min_items`, `max_items`, `unique_items` | Arrays |
| `unique` | Value is unique across all output documents (checked by `verify`) |

Only `required` fails on a null value; the others skip nulls. Use `on_error: reject` on a rule to reject
records whose input cannot be transformed (for example an invalid date) instead of failing the job.

Mapping files are read with YAML 1.2 boolean rules, so codes such as `NO`, `YES`, `ON` and `OFF` stay text.

## Fault catalogue

| Area | Faults |
|---|---|
| Decoding | `offset_shift`, `scale_loss`, `sign_loss`, `byte_order`, `wrong_source`, `truncate` |
| Transformations | `drop_step`, `case_flip`, `date_swap`, `date_nulls_ignored`, `map_default`, `substring_shift`, `substring_short`, `pad_side`, `boolean_invert`, `arith_swap`, `round_places`, `blank_as_empty`, `constant_null` |
| Concatenation | `concat_separator`, `concat_order`, `concat_missing_part`, `concat_untrimmed` |
| Conditions and filters | `condition_ignored`, `condition_negated`, `filter_ignored`, `filter_negated` |
| Validations | `drop_check`, `bound_shift` (off by one on every boundary), `allowed_shrunk`, `error_swallowed` |
| Arrays and objects | `array_drop_last`, `array_first_only`, `keep_blanks`, `keep_duplicates`, `index_shift`, `item_field_swap` |

A fault that no valid input can expose is reported as *undetected*. It is either an equivalent mutant
(for example, a check that can never fail given the field size) or a sign that the specification has a
gap. Both are worth a look.

## Using it in CI/CD

```yaml
# GitHub Actions
- run: pip install witnessforge
- run: witnessforge generate mapping/member.cpy mapping/member.yaml -o witness --min-score 1.0
- run: ./run-etl-job.sh witness/witnesses.dat --out loaded.jsonl --rejects rejects.jsonl
- run: witnessforge verify mapping/member.cpy mapping/member.yaml --input witness/witnesses.dat --actual loaded.jsonl --rejects rejects.jsonl
```

The same commands work as AWS CodePipeline / CodeBuild steps.

## Python API

```python
from witnessforge import load_copybook, load_mapping, forge, verify

layout = load_copybook("member.cpy")
mapping = load_mapping("mapping.yaml", layout)
result = forge(layout, mapping)

print(result.score, len(result.witnesses))
for witness in result.witnesses:
    witness.data        # EBCDIC bytes
    witness.outcome     # "loaded", "rejected" or "filtered"
    witness.expected    # expected document, or reject/filter marker
    witness.kills       # faults this record detects
```

## Roadmap

- Referential integrity and control totals across files
- Cross-field rules, REDEFINES and variable-length records
- SMT-solver backend for witness search on complex conditional rules
- Optional AI-proposed domain faults, filtered by a deterministic validity check
- Glue/PySpark adapter that runs `verify` directly against DocumentDB

## Development

```bash
pip install -e ".[dev]"
pytest
```

Contributions are welcome. Please open an issue to discuss larger changes first.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Citation

If you use WitnessForge in research, please cite it using [CITATION.cff](CITATION.cff).

Copyright 2026 Srikanth Kavuri.
