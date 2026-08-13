# Research: Evaluation v1 candidate preflight

Status: implementation preflight for [#618](https://github.com/ConnorGriffin/agentflow/issues/618).
It creates no Evaluation artifact, fixture, code, CI change, or GitHub state.

## Authority and extraction boundary

The only product authority is [#583](https://github.com/ConnorGriffin/agentflow/issues/583)
and [ADR 605](../adr/adr-605-canonical-evaluation-rulebook.md) plus
[ADR 606](../adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md).
Their requirements below are located and summarized, not copied into a second semantic
rulebook. Existing code and tests establish mechanical implementation choices only.
Comments, prior payloads, and this report are provenance, never product authority.

### Durable source snapshots

The extractor is bound to this immutable source set. The #583 source bytes are the
UTF-8 issue-body string returned by GitHub followed by exactly one `LF` byte (`0x0a`),
with SHA-256
`cdbaa62e34b3943fbbd2f3f63edf0b0cf17b00e3632983f8ab31506b89238c9d`.
The ADR source bytes are their complete repository files at source revision
`f5580b55cf373a7e9de47d99e617b08256b7647d`:

| Source | Whole-file SHA-256 |
| --- | --- |
| `docs/adr/adr-605-canonical-evaluation-rulebook.md` | `6977d6e1ce0bf5ebcaaff4fb2f47112dd59208705fd739ab394aa26bc589e70f` |
| `docs/adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md` | `4bde5dd87bcf4002de60c5a7a07f366fdea274e628dd24604ce5fd2495e4967b` |

Before extracting or accepting a candidate, run these exact rechecks from the
repository root. Any command failure, a different revision, or a different digest is
`E_SOURCE_DRIFT`; extraction and candidate acceptance stop.

```text
source_revision=f5580b55cf373a7e9de47d99e617b08256b7647d
test "$(git rev-parse "$source_revision^{commit}")" = "$source_revision"
test "$(gh api repos/ConnorGriffin/agentflow/issues/583 --jq .body | shasum -a 256 | awk '{print $1}')" = cdbaa62e34b3943fbbd2f3f63edf0b0cf17b00e3632983f8ab31506b89238c9d
test "$(git show "$source_revision:docs/adr/adr-605-canonical-evaluation-rulebook.md" | shasum -a 256 | awk '{print $1}')" = 6977d6e1ce0bf5ebcaaff4fb2f47112dd59208705fd739ab394aa26bc589e70f
test "$(git show "$source_revision:docs/adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md" | shasum -a 256 | awk '{print $1}')" = 4bde5dd87bcf4002de60c5a7a07f366fdea274e628dd24604ce5fd2495e4967b
```

`gh api ... --jq .body` supplies the body stream and its single terminating LF for
the issue binding. The command does not accept a comment, rendered HTML, title,
metadata, or a body with an additional trailing byte as a substitute source.

### Closed source-locator grammar

The extractor accepts exactly the following locators; any other heading, paragraph,
list item, or revision is an `E_SOURCE_LOCATOR` error.

```text
locator       = issue-locator / adr-locator
issue-locator = outcome / scope / versioned-contract / eligibility-gate /
                acceptance / out-of-scope
outcome       = "issue/583/outcome/p1"
scope         = "issue/583/scope/p1" / "issue/583/scope/p2"
versioned-contract = "issue/583/versioned-contract/b" ("1" / "2" / "3" / "4" / "5" / "6")
eligibility-gate = "issue/583/eligibility-gates/n" ("1" / "2" / "3" / "4" / "5")
acceptance    = "issue/583/acceptance/a" ("1" / "2" / "3" / "4" / "5" / "6" / "7" /
                "8" / "9" / "10" / "11" / "12" / "13" / "14")
out-of-scope  = "issue/583/out-of-scope/o" ("1" / "2" / "3" / "4" / "5")
adr-locator   = "adr/605/decision/p" decision-605 / "adr/606/decision/p" decision-606
decision-605  = "1" / "2" / "3"
decision-606  = "1" / "2" / "3" / "4" / "5"
```

Under Outcome, `p1` is its sole prose sentence; under Scope, `p1` and `p2` are
its two prose sentences. `b` denotes Versioned-contract bullets, `n` numbered eligibility gates, `a`
acceptance bullets, and `o` out-of-scope bullets. `adr/605/decision/p1` through
`p3` are the three sentences in that Decision paragraph; `adr/606/decision/p1`
through `p5` are its five sentences. It parses only the source set bound above; it
must reject source drift rather than silently reread a changed source.

### Deterministic extraction

For each locator in the grammar order, the extractor emits one record:

```text
requirement_id = "eval-v1:" + locator
sort_key       = grammar position (source order, then item number)
text           = the complete selected sentence, bullet, or numbered item
applicability  = all | corpus | holdout | semantic-case | implementation-boundary
disposition    = settled | mechanical-choice | not-applicable | unresolved
owner          = canonical-contract | checker | fixture-author | runner | product-owner
```

The ID is stable even if the selected text changes; the captured source digest makes
such a change visible. Extraction preserves source order. The result is valid only
when its ID list exactly equals the grammar's 41 locators, with no duplicate ID or
source locator. A duplicate source item or a second record for one locator is
`E_REQUIREMENT_DUPLICATE`; a missing locator is `E_REQUIREMENT_MISSING`. A source
item that genuinely applies in more than one place remains one record with a
multi-value `applicability`; it is not copied. The inventory is therefore both a
complete source map and the only candidate planning checklist.

## Requirement inventory

Each row owns exactly one authoritative source item. “Settled” is a product rule
the candidate must encode once in the versioned canonical contract. “Mechanical
choice” is an executable implementation default below, not a new Evaluation rule.

| ID | Disposition / owner | Requirement and reason |
| --- | --- | --- |
| `eval-v1:issue/583/outcome/p1` | settled / canonical-contract | Frozen versioned contract, sanitized corpus/holdouts, scoring schema, and promotion eligibility let an isolated paired runner execute without inventing metrics or thresholds. |
| `eval-v1:issue/583/scope/p1` | settled / canonical-contract | Define and validate manifests, fixtures, rubrics, answer keys, scorecards, contamination rules, and eligibility. |
| `eval-v1:issue/583/scope/p2` | not-applicable / runner | The executable multi-process runner is dependent work, so this candidate does not execute provider arms. |
| `eval-v1:issue/583/versioned-contract/b1` | settled / canonical-contract | Manifest pins every named input, method, environment, schedule, scorer/judge, and Evidence-policy fact. |
| `eval-v1:issue/583/versioned-contract/b2` | settled / canonical-contract | Quality is normalized in `[0,1]`; unavailable quality is zero only for eligibility and required hard/holdout semantic cases need adjudication. |
| `eval-v1:issue/583/versioned-contract/b3` | settled / canonical-contract | Paired difference, five-repetition case mean, seeded 10,000-draw case bootstrap, and interpolated 2.5th percentile define the lower bound. |
| `eval-v1:issue/583/versioned-contract/b4` | settled / canonical-contract | Critical misses are paired new candidate misses; grounded false positives are all-attempt mean validated counts and cannot increase. |
| `eval-v1:issue/583/versioned-contract/b5` | settled / canonical-contract | Every named threshold is inclusive at its stated value. |
| `eval-v1:issue/583/versioned-contract/b6` | settled / canonical-contract | The named metrics use all attempted runs; dollars remain present-only, un-imputed, and non-improving. |
| `eval-v1:issue/583/eligibility-gates/n1` | settled / canonical-contract | Independently on corpus and holdouts, every hard case needs 4/5 candidate passes and no new paired critical miss. |
| `eval-v1:issue/583/eligibility-gates/n2` | settled / canonical-contract | Independently on corpus and holdouts, lower bound is at least `-0.05` and grounded false positives do not increase. |
| `eval-v1:issue/583/eligibility-gates/n3` | settled / canonical-contract | Independently on corpus and holdouts, one stated quality/tokens/rounds improvement is required, with complete paired reports for the latter two. |
| `eval-v1:issue/583/eligibility-gates/n4` | settled / canonical-contract | Independently on corpus and holdouts, completion may fall at most two points and malformed/verification and unjudged rates may not rise. |
| `eval-v1:issue/583/eligibility-gates/n5` | settled / canonical-contract | Blinded human review of named scorecard material is required before promotion eligibility. |
| `eval-v1:issue/583/acceptance/a1` | settled / canonical-contract | Machine-readable, versioned manifest, fixture, rubric, answer-key, scorecard, and eligibility schemas implement the contract. |
| `eval-v1:issue/583/acceptance/a2` | settled / fixture-author | Corpus has sanitized/synthetic cases for six failure classes and the planted review-round scenario, plus an independent frozen holdout set. |
| `eval-v1:issue/583/acceptance/a3` | settled / fixture-author | A motivating incident cannot be its candidate's holdout; edits to fixture/rubric/answer-key require a new corpus version. |
| `eval-v1:issue/583/acceptance/a4` | settled / checker | Method sources require clean, resolved immutable revisions and materialized digests; dirty or unresolved sources fail. |
| `eval-v1:issue/583/acceptance/a5` | settled / canonical-contract | Prefer mechanical answer keys; semantic judging is blinded, version-pinned, rationale-bearing, and records human adjudication state. |
| `eval-v1:issue/583/acceptance/a6` | settled / canonical-contract | Scorecards report every named quality, defect, cost, completion, verification, missingness, and adjudication measure separately. |
| `eval-v1:issue/583/acceptance/a7` | settled / canonical-contract | Reports keep missing values `null`; eligibility uses its explicit zero-quality and all-attempt rules. |
| `eval-v1:issue/583/acceptance/a8` | settled / checker | Calculation implements all five gates, pairing, inclusive thresholds, 10,000-draw bootstrap, and token completeness; absent adjudication/manifest facts block it. |
| `eval-v1:issue/583/acceptance/a9` | settled / fixture-author | Truth tables cover swapped misses, equality thresholds, bootstrap replay, and partial tokens and rounds. |
| `eval-v1:issue/583/acceptance/a10` | settled / implementation-boundary | Results use #580's `evaluate`; eligibility may call `nominate`; no direct Evidence-table or parallel result store exists. |
| `eval-v1:issue/583/acceptance/a11` | settled / fixture-author | Production telemetry and raw finding counts are never fixture truth or automatic labels. |
| `eval-v1:issue/583/acceptance/a12` | settled / checker | Contract tests cover every named mismatch, calculation, missingness, and contamination condition. |
| `eval-v1:issue/583/acceptance/a13` | settled / implementation-boundary | Create and index ADR 583 in the stated location. |
| `eval-v1:issue/583/acceptance/a14` | settled / checker | The repository-wide `uv run pytest -q` is the required final test command. |
| `eval-v1:issue/583/out-of-scope/o1` | not-applicable / runner | Provider-arm execution and process/cache/oracle isolation are deferred. |
| `eval-v1:issue/583/out-of-scope/o2` | not-applicable / product-owner | Promotion/canary behavior is deferred. |
| `eval-v1:issue/583/out-of-scope/o3` | not-applicable / product-owner | Automatic method mutation is deferred. |
| `eval-v1:issue/583/out-of-scope/o4` | not-applicable / fixture-author | Private transcript ingestion is deferred. |
| `eval-v1:issue/583/out-of-scope/o5` | not-applicable / product-owner | UI is deferred. |
| `eval-v1:adr/605/decision/p1` | settled / canonical-contract | Evaluation v1 has one versioned canonical data contract as its semantic authority. |
| `eval-v1:adr/605/decision/p2` | settled / implementation-boundary | AgentFlow and its independent verifier consume that contract. |
| `eval-v1:adr/605/decision/p3` | settled / implementation-boundary | Runtime owns execution mechanics; fixtures and locks prove conformance without restating semantic rules. |
| `eval-v1:adr/606/decision/p1` | settled / canonical-contract | `missing_metric_names` is the exact sorted set of null arm-metric fields. |
| `eval-v1:adr/606/decision/p2` | settled / canonical-contract | A wholly unavailable result names all seven stated metrics in lexicographic order: `duration_ms`, `fix_introduced_defect_count`, `grounded_false_positive_count`, `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`. |
| `eval-v1:adr/606/decision/p3` | settled / canonical-contract | A reported result may name only its null optional metrics in lexicographic order: `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`; its other three metrics are required. |
| `eval-v1:adr/606/decision/p4` | settled / checker | An adjudication is valid only when its case ID, exact case-manifest digest, and answer-key digest match the answer-key reference reached through the canonical validated case record. |
| `eval-v1:adr/606/decision/p5` | settled / checker | The adjudication digest is the canonical digest of the receipt with its own digest field omitted. |

The grammar and table contain exactly 41 source locators: one Outcome sentence, two
Scope sentences, six Versioned-contract bullets, five eligibility gates, fourteen
acceptance bullets, five out-of-scope bullets, and eight ADR Decision sentences. An
extractor seeing `a15` must fail `E_SOURCE_LOCATOR`; this prevents an invented
acceptance item.

## Mechanical contract for the fresh candidate

These defaults are fail-closed implementation choices. They do not alter a metric,
threshold, failure class, or authority boundary settled above.

### Bytes, JSON, IDs, and digests

1. Every candidate JSON file is one UTF-8 byte sequence, without BOM, with LF line
   endings, exactly one trailing LF, and no other leading/trailing whitespace outside JSON.
   Decode rejects invalid UTF-8, BOM, CR, duplicate object member names at every
   depth, non-finite numbers, and unpaired Unicode surrogates.
2. Canonical JSON is ASCII-only. Objects use Unicode-code-point ascending member names;
   arrays retain declared order. Quote and reverse solidus are `\"` and `\\`; every
   control code point is `\u00xx` with lowercase hex; every BMP scalar `U+0080`–`U+FFFF`
   is `\u` followed by four lowercase hex digits; every non-BMP scalar is its UTF-16
   high-surrogate `\uXXXX` followed by low-surrogate `\uXXXX`, both lowercase hex.
   No literal non-ASCII byte and no alternative short escape is permitted. Integers use
   `0` or `-?[1-9][0-9]*`. Decimal values use `-?(0|[1-9][0-9]*)\.[0-9]+` with no
   exponent and no redundant trailing zero. Booleans and `null` use lowercase JSON spellings.
3. Candidate contracts use scaled integers for every computed quantity except a
   source field expressly requiring a JSON decimal. This prevents host-language
   float formatting from changing an artifact digest. The one canonical serializer
   is reused for input bytes, expected bytes, and digest preimages; parsed-and-
   reserialized input must byte-equal its original bytes.
4. Every SHA-256 value is exactly 64 lowercase hexadecimal characters
   (`^[a-f0-9]{64}$`), without an algorithm prefix. The conformance report records
   the whole-file SHA-256 of `contract-v1.candidate.json`. A record's own digest
   instead covers its canonical digest-free projection: the same object with only
   its top-level `digest` member removed. Reject a missing or non-string required
   digest, nested members named `digest` that purport to identify their enclosing
   object, or a mismatch. Bundle digests cover a separate digest-free bundle object
   containing sorted `{path,digest}` entries. No digest includes itself, a mutable
   ref, current time, absolute path, or process environment.
5. Stable IDs are ASCII `^[a-z][a-z0-9-]{0,47}$`, unique within their declared
   collection. Declared case IDs are source-controlled, never generated from a
   mutable title. Generated test IDs are `g-` plus the first 24 lowercase hex
   characters of SHA-256 over `canonical_json({"generator":"evaluation-v1-casegen",
   "seed":seed,"ordinal":ordinal,"template":template_id})`; collision is an error.

### Closed schema and reference grammar

The candidate supports this deliberately small schema language, and rejects every
other keyword rather than silently ignoring it:

```text
schema         = object with exactly "schema_version", "root", "definitions"
schema_version = "evaluation-schema-v1"
root           = schema-node
definitions    = object mapping schema-id to schema-node
schema-id      = /^[a-z][a-z0-9-]{0,47}$/
schema-node    = {"type": type, allowed node members for type}
type           = "object" / "array" / "string" / "integer" / "boolean" / "null" / "ref"
ref            = {"type":"ref","ref":"#/definitions/" schema-id}
```

Object nodes may contain exactly `type`, `required`, `properties`, and
`additional_properties`; `additional_properties` must be `false`; `required` is
sorted, unique, and exactly a subset of `properties`. Array nodes may contain exactly
`type`, `items`, `min_items`, `max_items`; string nodes exactly `type`, `pattern`,
`min_length`, `max_length`; integer nodes exactly `type`, `minimum`, `maximum`.
Bounds are non-negative integers, and min is at most max. Boolean/null/ref nodes use
only the members shown. Every reference resolves in the local `definitions` object;
no URI, file, fragment other than the exact local form, or remote retrieval exists.
The definition-reference graph must be acyclic (`E_REF_CYCLE`), and unused definitions
are rejected (`E_REF_UNUSED`).

### Paths, resources, and deterministic generation

All candidate paths are POSIX, repository-relative, nonempty, at most 160 bytes, and
match `^[a-z0-9][a-z0-9._/-]*$`; they contain no `//`, leading slash, trailing slash,
`.` segment, `..` segment, or symlink component. The checker validates declared paths
lexically; it opens only the two fixed candidate data files, from the repository root,
with no-follow semantics and accepts only regular files.

| Limit | Exact default |
| --- | ---: |
| one JSON artifact | 1 MiB |
| aggregate candidate input | 16 MiB |
| JSON nesting | 32 containers |
| object members / array entries | 256 each |
| definitions | 64 |
| references per schema | 64 |
| path depth | 12 |
| generated cases per declared generator | 256 |
| generated case bytes | 64 KiB each |
| generated corpus bytes | 8 MiB |
| checker stdout or stderr | 4 KiB each |

### Generated-case byte mapping

The candidate may declare one bounded generic `generation` object, with this exact
shape. It contains no Evaluation rule, label, metric, threshold, or expected result.

```text
generation = {"generator":"evaluation-v1-casegen", "seed": uint64, "templates": [template, ...]}
template   = {"base_case_id": id, "id": id, "operation": operation,
              "operand": canonical-json-value, "target": target}
target     = {"json_pointer": pointer} / {"raw_byte_offset": uint}
pointer    = "" / ("/" reference-token)*
reference-token = *(unescaped / "~0" / "~1")
operation  = "json_replace" / "json_remove" / "json_object_insert" /
             "json_array_insert" / "raw_truncate" / "raw_byte_replace" /
             "raw_duplicate_key_inject"
```

The object members above are exact, `uint64` is `0..18446744073709551615`, and a
template's `operand` is the canonical value already present in the exact opened
candidate bytes. Templates contain `1..256` unique IDs in ascending bytewise ID order;
their zero-based ordinal is that order. `base_case_id` resolves to exactly one declared,
non-generated base case. The checker retains both its parsed `input` value and the exact
byte slice of that `input` JSON token from the already opened candidate bytes. A JSON
pointer follows RFC 6901 token decoding (`~0` → `~`, `~1` → `/`); any other `~` escape,
missing path, or wrong target type fails.

For each template, the checker first copies the base value deeply and the base token
bytes byte-for-byte. It derives the generated ID from the already-set rule:
`g-` plus the first 24 lowercase hex characters of SHA-256 over
`canonical_json({"generator":"evaluation-v1-casegen","seed":seed,"ordinal":ordinal,"template":template_id})`.
It rejects an ID collision. The closed operations are:

| Operation | Target and canonical operand | Preconditions and exact result |
| --- | --- | --- |
| `json_replace` | `json_pointer`; any JSON value | Pointer resolves to an existing value, including root. Replace that value in the deep copy; result bytes are `canonical_json(copy) + LF`. |
| `json_remove` | `json_pointer`; `null` | Pointer is non-root and resolves to an existing object member or array element. Delete the member, or delete the indexed element and shift later elements left; result bytes are `canonical_json(copy) + LF`. |
| `json_object_insert` | `json_pointer`; exactly `{"key": string, "value": JSON-value}` | Pointer resolves to an object and `key` is absent. Insert the member, then canonical serialization supplies bytewise member ordering; result bytes are `canonical_json(copy) + LF`. |
| `json_array_insert` | `json_pointer`; exactly `{"index": uint, "value": JSON-value}` | Pointer resolves to an array and `0 <= index <= length`. Insert before that index (or append at length); result bytes are `canonical_json(copy) + LF`. |
| `raw_truncate` | `raw_byte_offset`; `null` | Offset is less than the base token-byte length. Copy bytes `[0:offset]`; no parsing or reserialization follows. |
| `raw_byte_replace` | `raw_byte_offset`; integer `0..255` | Offset is less than the base token-byte length. Copy the bytes and replace exactly that zero-based byte with the operand byte; no parsing or reserialization follows. |
| `raw_duplicate_key_inject` | `raw_byte_offset`; exactly `{"key": string, "value": JSON-value}` | Offset addresses the closing `}` of one directly delimited, nonempty object in the base token bytes, and that object already has direct member `key`. Insert immediately before `}` the ASCII bytes `,` + `canonical_json(key)` + `:` + `canonical_json(value)`. No parsing or reserialization follows. |

For raw duplicate injection, the checker finds the directly delimited object by a
bytewise JSON token scan of the copied base token bytes; the target byte must be its
matching closing brace, not a brace inside a string or nested value. The insertion is
therefore always one additional direct occurrence of the named member. A raw result is
allowed to be invalid JSON: it is a generic rejection input, not a canonical semantic
case. JSON-operation payload bytes are canonical JSON; raw-operation payload bytes are
the exact specified byte mutation. If a generated-case record is emitted or persisted,
it is canonical ASCII JSON containing the generated ID and the payload as lowercase
two-hex-digit-per-byte `input_bytes_hex`; this record serialization, not an intentionally
malformed raw payload, is what “emits canonical bytes” means.

Validate templates in sorted order: exact shape and canonical operand, base-case
resolution, target syntax/range, operation preconditions, generated-ID collision, then
per-case and aggregate size limits. Respectively fail `E_GENERATOR_TEMPLATE`,
`E_GENERATOR_TARGET`, `E_GENERATOR_PRECONDITION`, `E_GENERATOR_COLLISION`, or
`E_GENERATOR_LIMIT`; a later template is never considered after the first failure.
No system RNG, clock, filesystem enumeration, provider, or network input is permitted.
Generated cases exercise only generic bytes, parser, reference, and bound mechanics;
they cannot replace the required frozen corpus, independent holdouts, six failure-class
coverage, or planted review-round scenario.

### Validation order and checker interface

The zero-argument public checker is exactly:

```text
uv run python scripts/check-evaluation-contract-candidate.py
```

It is a standalone stdlib script: it imports no `agentflow.*` module, finds the
repository root from its own checked-in `scripts/` location rather than the current
directory, and opens only these two Evaluation candidate data files:

```text
docs/evaluation/design/contract-v1.candidate.json
docs/evaluation/design/contract-v1.conformance.json
```

Runtime modules and runtime artifacts are #614 work and are deliberately outside
this checker interface.

It validates the two files in lexicographic path order and stops at the first failing
artifact: `E_ROOT`, `E_IO`, `E_LIMIT`, `E_UTF8`, `E_JSON`,
`E_DUPLICATE_KEY`, `E_CANONICAL`, `E_SCHEMA`, `E_REF`, `E_REF_CYCLE`,
`E_REF_UNUSED`, `E_PATH`, `E_DIGEST`, `E_ID`, `E_CROSS_REFERENCE`, `E_LINEAGE`,
`E_ORACLE`, `E_GENERATOR_TEMPLATE`, `E_GENERATOR_TARGET`,
`E_GENERATOR_PRECONDITION`, `E_GENERATOR_COLLISION`, `E_GENERATOR_LIMIT`,
`E_SEMANTIC`. Within one artifact, the first rule in that order wins; within a
collection, sort IDs/paths bytewise. It validates the
candidate's declared semantic rules, mappings, cases, and bounds as data, then applies
an independent mechanical interpreter to every declared case; it never uses an
expected result as a rule source.

On success stdout is precisely one canonical ASCII JSON line and stderr is empty:

```json
{"checked":2,"format":"evaluation-contract-candidate-check-v1","status":"ok"}
```

On failure stdout is empty; stderr is precisely one canonical JSON line containing
only `code`, `format`, `path`, and `status`, e.g.

```json
{"code":"E_DIGEST","format":"evaluation-contract-candidate-check-v1","path":"docs/evaluation/design/contract-v1.candidate.json","status":"error"}
```

It exits `0` for success, `1` for a validation failure, and `2` only for an
internal checker fault. It neither repairs nor writes a candidate file.

## Independent case oracle

The checker has two deliberately separate roles. The candidate file is the sole data
plane for Evaluation semantics: its closed rules, thresholds, mappings, cases, and
expected outcomes are the candidate under review. The standalone checker supplies only
an independent mechanical interpreter for the candidate's declared grammar, arithmetic,
reference resolution, canonical bytes, and digest framing. It does not import AgentFlow
product code or carry a second production registry of Evaluation thresholds, metrics,
or eligibility semantics. The interpreter computes a declared case before it reads,
deserializes, or hashes that case's `expected_result`.

For each semantic case, the oracle resolves `case_id` in the validated case map, then
requires the exact case-manifest digest and the answer-key digest reached from that
case record. It recomputes the adjudication receipt preimage with the receipt digest
omitted and accepts the receipt only if all four values agree. This is the ADR 606
lineage join; a candidate cannot change a case, its answer key, and its expected result
into a self-consistent but unbound fiction.

The conformance report maps each applicable authoritative requirement to exactly one
candidate rule ID and at least one declared positive or negative case ID. It records
the candidate's whole-file SHA-256, so mutating a semantic field, case expectation, or
mapping without changing the reviewed report fails before comparison. The required
swapped critical-miss, equality-threshold, bootstrap-replay, partial-token, and
partial-round cases remain candidate data; the checker verifies their structure and
independently interprets their declared mechanics. Checker tests protect that generic
interpreter and its rejection behavior, not a duplicate production semantic registry.
The report may not claim zero `unresolved` until the failure-class decision below is
made; #617's acceptance condition then requires zero unresolved, unmapped applicable
requirements, and duplicate owners.

## Captured verification results

These are the captured results for commit verification. The intentionally untracked
handoff file is outside #618 and excluded; this report makes no whole-worktree-clean
claim.

| Gate | Captured command | Exit / result |
| --- | --- | --- |
| Commit diff | `git diff --check HEAD^ HEAD` | Exit `0`. |
| Changed path | `git diff-tree --no-commit-id --name-only -r HEAD` | Only `docs/research/evaluation-candidate-preflight.md`. |
| Public links | `uv run python scripts/check-public-doc-links.py` | Exit `0`; `Documentation link check passed for 13 relative link(s).` |
| Focused precedents | `uv run pytest -q tests/test_evidence_contract.py tests/test_reviewer.py` | Exit `0`; `80 passed`. |
| DCO | `python3 scripts/check-dco.py HEAD^ HEAD` | Exit `0`; `DCO check passed for 1 commit(s).` |

### Future acceptance commands — unexecuted

These commands apply only after #617 creates its deliverables; they are not evidence
for this preflight commit.

| Future gate | Command | Required result |
| --- | --- | --- |
| Candidate paths | `git ls-files --error-unmatch docs/evaluation/design/contract-v1.candidate.json docs/evaluation/design/contract-v1.conformance.json scripts/check-evaluation-contract-candidate.py tests/test_evaluation_contract_candidate.py` | Exactly the four #617 deliverable paths print. |
| Candidate checker | `uv run python scripts/check-evaluation-contract-candidate.py` | Exact zero-argument success line, exit `0`; no `agentflow.*` import occurs. |
| Focused candidate tests | `uv run pytest -q tests/test_evaluation_contract_candidate.py` | Candidate data coverage, generic interpreter, byte, bound, lineage, and rejection-case tests pass. |
| Repository acceptance | `uv run pytest -q` | Entire Python suite passes. |

## Unresolved product decisions

1. What are the six failure-class identifiers required for the v1 corpus? #583
   requires all six but does not name them; no fail-closed mechanical default can
   select product labels or their case semantics. The product owner must add them to
   a reviewed canonical contract version before fixtures claim coverage.

All absent field shapes, artifacts, bindings, and computations fail closed. Any later
request to add a metric, threshold, case class, or promotion behavior requires a new
reviewed canonical contract version under ADR 605; it is not a preflight default.
