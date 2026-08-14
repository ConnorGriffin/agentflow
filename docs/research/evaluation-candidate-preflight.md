# Research: Evaluation v1 candidate preflight

Status: implementation preflight for [#617](https://github.com/ConnorGriffin/agentflow/issues/617).
It creates no Evaluation artifact, fixture, code, CI change, or GitHub state.

## Authority and extraction boundary

The product authority is [#583](https://github.com/ConnorGriffin/agentflow/issues/583),
[ADR 605](../adr/adr-605-canonical-evaluation-rulebook.md), and
[ADR 606](../adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md).
[ADR 620](../adr/adr-620-evaluation-failure-classes.md) is the separately pinned,
narrow closure of the six-class vocabulary; it adds no other product authority.
[ADR 626](../adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md) supersedes
only ADR 605's one-data-file boundary: the one authority is now a manifest-rooted
bundle of declarative candidate JSON and one digest-bound pure semantic module.
Their requirements below are located and summarized, not copied into a second semantic
rulebook. Existing code and tests establish mechanical implementation choices only.
Comments, prior payloads, and this report are provenance, never product authority.

### Durable source snapshots

The extractor is bound to this immutable source set. The #583 source bytes are the
UTF-8 issue-body string returned by GitHub followed by exactly one `LF` byte (`0x0a`),
with SHA-256
`cdbaa62e34b3943fbbd2f3f63edf0b0cf17b00e3632983f8ab31506b89238c9d`.
The ADR 605 and ADR 606 source bytes are their complete repository files at source
revision `f5580b55cf373a7e9de47d99e617b08256b7647d`. ADR 620 source bytes are its
complete repository file at immutable merge
`3cd31b7d5528a6bb5bb322334a32a25ac13991b5`. ADR 626 source bytes are its
complete repository file at immutable commit
`c13cc6b77bac94ab71b4c689aeef7f2eaa242be3`:

| Source | Whole-file SHA-256 |
| --- | --- |
| `docs/adr/adr-605-canonical-evaluation-rulebook.md` | `6977d6e1ce0bf5ebcaaff4fb2f47112dd59208705fd739ab394aa26bc589e70f` |
| `docs/adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md` | `4bde5dd87bcf4002de60c5a7a07f366fdea274e628dd24604ce5fd2495e4967b` |
| `docs/adr/adr-620-evaluation-failure-classes.md` | `7aed248b63d8035364114a28eb184c0aa839b55c627f5de3d9d17e1af1b1cb9a` |
| `docs/adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md` | `6bccfb0848fd1ad985c1442e1cb8dbb633ded8bd0f10878e80b0876c9e17a13e` |

Before extracting or accepting a candidate, run these exact rechecks from the
repository root. Any command failure, a different revision, or a different digest is
`E_SOURCE_DRIFT`; extraction and candidate acceptance stop.

```text
source_revision=f5580b55cf373a7e9de47d99e617b08256b7647d
test "$(git rev-parse "$source_revision^{commit}")" = "$source_revision"
test "$(git rev-parse "3cd31b7d5528a6bb5bb322334a32a25ac13991b5^{commit}")" = 3cd31b7d5528a6bb5bb322334a32a25ac13991b5
test "$(git rev-parse "c13cc6b77bac94ab71b4c689aeef7f2eaa242be3^{commit}")" = c13cc6b77bac94ab71b4c689aeef7f2eaa242be3
test "$(gh api repos/ConnorGriffin/agentflow/issues/583 --jq .body | shasum -a 256 | awk '{print $1}')" = cdbaa62e34b3943fbbd2f3f63edf0b0cf17b00e3632983f8ab31506b89238c9d
test "$(git show "$source_revision:docs/adr/adr-605-canonical-evaluation-rulebook.md" | shasum -a 256 | awk '{print $1}')" = 6977d6e1ce0bf5ebcaaff4fb2f47112dd59208705fd739ab394aa26bc589e70f
test "$(git show "$source_revision:docs/adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md" | shasum -a 256 | awk '{print $1}')" = 4bde5dd87bcf4002de60c5a7a07f366fdea274e628dd24604ce5fd2495e4967b
test "$(git show "3cd31b7d5528a6bb5bb322334a32a25ac13991b5:docs/adr/adr-620-evaluation-failure-classes.md" | shasum -a 256 | awk '{print $1}')" = 7aed248b63d8035364114a28eb184c0aa839b55c627f5de3d9d17e1af1b1cb9a
test "$(git show "c13cc6b77bac94ab71b4c689aeef7f2eaa242be3:docs/adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md" | shasum -a 256 | awk '{print $1}')" = 6bccfb0848fd1ad985c1442e1cb8dbb633ded8bd0f10878e80b0876c9e17a13e
```

`gh api ... --jq .body` supplies the body stream and its single terminating LF for
the issue binding. The command does not accept a comment, rendered HTML, title,
metadata, or a body with an additional trailing byte as a substitute source.

### Closed source-locator grammar

The extractor accepts exactly the following locators; any other heading, paragraph, or
list item is an `E_SOURCE_LOCATOR` error. A source revision or whole-file-byte change
is `E_SOURCE_DRIFT`.

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
adr-locator   = "adr/605/decision/p" decision-605 / "adr/606/decision/p" decision-606 /
                adr-620-locator / adr-626-locator
decision-605  = "1" / "2" / "3"
decision-606  = "1" / "2" / "3" / "4" / "5"
adr-620-locator = "adr/620/decision/intro/p1" /
                  "adr/620/decision/class/r" ("1" / "2" / "3" / "4" / "5" / "6") /
                  "adr/620/decision/orthogonality/p1" /
                  "adr/620/decision/governance/p1"
adr-626-locator = "adr/626/decision/bundle/p1" /
                  "adr/626/decision/binding/intro-p1" /
                  "adr/626/decision/binding/code1" /
                  "adr/626/decision/interface/intro-p1" /
                  "adr/626/decision/interface/code1" /
                  "adr/626/decision/operation/p1" /
                  "adr/626/decision/purity/p1" /
                  "adr/626/decision/open-set/intro-p1" /
                  "adr/626/decision/open-set/n" ("1" / "2" / "3") /
                  "adr/626/decision/locks/p1" /
                  "adr/626/decision/loading/p1" /
                  "adr/626/decision/checker/p1" /
                  "adr/626/decision/independence/p1" /
                  "adr/626/decision/supersession/p1"
```

Under Outcome, `p1` is its sole prose sentence; under Scope, `p1` and `p2` are
its two prose sentences. `b` denotes Versioned-contract bullets, `n` numbered eligibility gates, `a`
acceptance bullets, and `o` out-of-scope bullets. `adr/605/decision/p1` through
`p3` are the three sentences in that Decision paragraph; `adr/606/decision/p1`
through `p5` are its five sentences. It parses only the source set bound above; it
must reject source drift rather than silently reread a changed source.

For ADR 620, `intro/p1` is the Decision sentence introducing the exact six values;
`class/r1` through `r6` are the six Decision table rows in source order;
`orthogonality/p1` is the full paragraph beginning `validation_state`; and
`governance/p1` is the full paragraph beginning `The six identifiers`. These nine
locators are closed: a changed heading, row, paragraph, table order, or source revision
is `E_SOURCE_LOCATOR` (or `E_SOURCE_DRIFT` for bytes/revision), never an invitation to
infer a replacement meaning. They are the ADR 620 closure and extend the pinned
extraction universe from the 41 #583/ADR 605/ADR 606 locators to 50 locators.

For ADR 626, `bundle/p1` is the first Decision paragraph; `binding/intro-p1` and
`binding/code1` are its binding-introduction paragraph and complete JSON block;
`interface/intro-p1` and `interface/code1` are its interface-introduction paragraph and
complete signature block; `operation/p1` and `purity/p1` are the next two complete
paragraphs; `open-set/intro-p1` and `open-set/n1` through `n3` are the checker-open
introduction and three numbered paths; and `locks/p1`, `loading/p1`, `checker/p1`,
`independence/p1`, and `supersession/p1` are the remaining five Decision paragraphs.
These 16 locators extend the complete pinned extraction universe to 66 locators.

### Deterministic extraction

For each locator in the grammar order, the extractor emits one record:

```text
requirement_id = "eval-v1:" + locator
sort_key       = grammar position (source order, then item number)
text           = the complete selected sentence, bullet, or numbered item
applicability  = all | corpus | holdout | semantic-case | implementation-boundary
disposition    = settled | mechanical-choice | not-applicable | unresolved
owner          = semantic-bundle | canonical-contract | semantic-module | checker |
                 fixture-author | runner | product-owner | implementation-boundary
```

The ID is stable even if the selected text changes; the captured source digest makes
such a change visible. Extraction preserves source order. The result is valid only
when its ID list exactly equals the grammar's 66 locators, with no duplicate ID or
source locator. A duplicate source item or a second record for one locator is
`E_REQUIREMENT_DUPLICATE`; a missing locator is `E_REQUIREMENT_MISSING`. A source
item that genuinely applies in more than one place remains one record with a
multi-value `applicability`; it is not copied. The inventory is therefore both a
complete source map and the only candidate planning checklist.

## Requirement inventory

Each row owns exactly one authoritative source item. `semantic-bundle` owns only a
cross-member authority or binding rule; `canonical-contract` owns declarative policy;
and `semantic-module` owns procedural Evaluation semantics. `checker` owns only
independent structure, byte, reference, coverage, digest, and source/AST validation,
safe dispatch through the bound module, and expected-result comparison. “Mechanical
choice” is an executable implementation default below, not a new Evaluation rule.

| ID | Disposition / owner | Requirement and reason |
| --- | --- | --- |
| `eval-v1:issue/583/outcome/p1` | settled / canonical-contract | Frozen versioned contract, sanitized corpus/holdouts, scoring schema, and promotion eligibility let an isolated paired runner execute without inventing metrics or thresholds. |
| `eval-v1:issue/583/scope/p1` | settled / canonical-contract | Define and validate manifests, fixtures, rubrics, answer keys, scorecards, contamination rules, and eligibility. |
| `eval-v1:issue/583/scope/p2` | not-applicable / runner | The executable multi-process runner is dependent work, so this candidate does not execute provider arms. |
| `eval-v1:issue/583/versioned-contract/b1` | settled / canonical-contract | Manifest pins every named input, method, environment, schedule, scorer/judge, and Evidence-policy fact. |
| `eval-v1:issue/583/versioned-contract/b2` | settled / semantic-module | The contract supplies quality and adjudication policy; the bound module applies zero-for-eligibility and required-adjudication branches. |
| `eval-v1:issue/583/versioned-contract/b3` | settled / semantic-module | The bound module owns paired differences, five-repetition means, seeded 10,000-draw bootstrap, and percentile interpolation from contract parameters. |
| `eval-v1:issue/583/versioned-contract/b4` | settled / semantic-module | The bound module calculates new paired critical misses and all-attempt grounded-false-positive means from contract-declared metric identities. |
| `eval-v1:issue/583/versioned-contract/b5` | settled / canonical-contract | Every named threshold is inclusive at its stated value. |
| `eval-v1:issue/583/versioned-contract/b6` | settled / semantic-module | The bound module applies all-attempt and present-only aggregation from contract-declared metric and missingness policy. |
| `eval-v1:issue/583/eligibility-gates/n1` | settled / semantic-module | The bound module evaluates the contract-declared hard-case pass and paired-critical-miss gate independently per partition. |
| `eval-v1:issue/583/eligibility-gates/n2` | settled / semantic-module | The bound module evaluates the contract-declared lower-bound and grounded-false-positive gate independently per partition. |
| `eval-v1:issue/583/eligibility-gates/n3` | settled / semantic-module | The bound module evaluates the contract-declared quality, token, and round improvement alternatives and completeness branches. |
| `eval-v1:issue/583/eligibility-gates/n4` | settled / semantic-module | The bound module evaluates the contract-declared completion and failure-rate comparisons independently per partition. |
| `eval-v1:issue/583/eligibility-gates/n5` | settled / semantic-module | The bound module evaluates the contract-declared authority and blinded-review transition before eligibility. |
| `eval-v1:issue/583/acceptance/a1` | settled / canonical-contract | Machine-readable, versioned manifest, fixture, rubric, answer-key, scorecard, and eligibility schemas implement the contract. |
| `eval-v1:issue/583/acceptance/a2` | settled / fixture-author | Corpus has sanitized/synthetic cases for six failure classes and the planted review-round scenario, plus an independent frozen holdout set. |
| `eval-v1:issue/583/acceptance/a3` | settled / fixture-author | A motivating incident cannot be its candidate's holdout; edits to fixture/rubric/answer-key require a new corpus version. |
| `eval-v1:issue/583/acceptance/a4` | settled / semantic-module | The checker validates source-record structure and digest bindings; the bound module applies the contract's clean, resolved, immutable-source and materialization predicates to its input facts. |
| `eval-v1:issue/583/acceptance/a5` | settled / semantic-module | The bound module applies the contract-declared mechanical-key preference and blinded, version-pinned adjudication transitions. |
| `eval-v1:issue/583/acceptance/a6` | settled / semantic-module | The bound module constructs the contract-declared separate scorecard projections. |
| `eval-v1:issue/583/acceptance/a7` | settled / semantic-module | The bound module preserves report nulls and applies the contract-declared zero-quality and all-attempt eligibility branches. |
| `eval-v1:issue/583/acceptance/a8` | settled / semantic-module | The bound module owns all five gates, pairing, inclusive comparison, bootstrap, token-completeness, and missing-fact blocking calculations. The checker only dispatches vectors and compares results. |
| `eval-v1:issue/583/acceptance/a9` | settled / fixture-author | Truth tables cover swapped misses, equality thresholds, bootstrap replay, and partial tokens and rounds. |
| `eval-v1:issue/583/acceptance/a10` | settled / semantic-module | The bound module owns the contract-declared Evidence constructor projections and operation ordering; downstream production invokes the resulting exact values through #580 without a parallel store. |
| `eval-v1:issue/583/acceptance/a11` | settled / fixture-author | Production telemetry and raw finding counts are never fixture truth or automatic labels. |
| `eval-v1:issue/583/acceptance/a12` | settled / checker | Checker tests cover structural rejection plus safe vector dispatch and expected-result comparison for every named case; calculations remain module-owned. |
| `eval-v1:issue/583/acceptance/a13` | settled / implementation-boundary | Create and index ADR 583 in the stated location. |
| `eval-v1:issue/583/acceptance/a14` | settled / implementation-boundary | The repository-wide `uv run pytest -q` remains the downstream final test command; it is not checker semantic ownership. |
| `eval-v1:issue/583/out-of-scope/o1` | not-applicable / runner | Provider-arm execution and process/cache/oracle isolation are deferred. |
| `eval-v1:issue/583/out-of-scope/o2` | not-applicable / product-owner | Promotion/canary behavior is deferred. |
| `eval-v1:issue/583/out-of-scope/o3` | not-applicable / product-owner | Automatic method mutation is deferred. |
| `eval-v1:issue/583/out-of-scope/o4` | not-applicable / fixture-author | Private transcript ingestion is deferred. |
| `eval-v1:issue/583/out-of-scope/o5` | not-applicable / product-owner | UI is deferred. |
| `eval-v1:adr/605/decision/p1` | settled / semantic-bundle | ADR 626 supersedes only the data-only form: Evaluation v1 has one versioned manifest-rooted semantic bundle as its authority. |
| `eval-v1:adr/605/decision/p2` | settled / implementation-boundary | ADR 626 narrows consumption: production calls the exact bound module, while the checker validates the bundle and safely dispatches conformance vectors. |
| `eval-v1:adr/605/decision/p3` | settled / semantic-module | ADR 626 supersedes runtime ownership of procedural semantics: the bound module owns them; fixtures and locks remain conformance evidence without restating policy. |
| `eval-v1:adr/606/decision/p1` | settled / canonical-contract | `missing_metric_names` is the exact sorted set of null arm-metric fields. |
| `eval-v1:adr/606/decision/p2` | settled / canonical-contract | A wholly unavailable result names all seven stated metrics in lexicographic order: `duration_ms`, `fix_introduced_defect_count`, `grounded_false_positive_count`, `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`. |
| `eval-v1:adr/606/decision/p3` | settled / canonical-contract | A reported result may name only its null optional metrics in lexicographic order: `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`; its other three metrics are required. |
| `eval-v1:adr/606/decision/p4` | settled / semantic-module | The bound module resolves and evaluates the canonical adjudication-lineage join; the checker independently validates its input references and compares vector results. |
| `eval-v1:adr/606/decision/p5` | settled / semantic-module | The bound module constructs the adjudication digest from the contract-declared canonical preimage; the checker validates digest bindings and expected results. |
| `eval-v1:adr/620/decision/intro/p1` | settled / canonical-contract | Evaluation v1 uses exactly the six failure-class identifiers in ADR 620. |
| `eval-v1:adr/620/decision/class/r1` | settled / canonical-contract | `original_defect` is an artifact violation of a product, acceptance, security, or charter requirement before review. |
| `eval-v1:adr/620/decision/class/r2` | settled / canonical-contract | `plan_gap` is a plan or acceptance criteria omission, contradiction, or failure to operationalize required behavior. |
| `eval-v1:adr/620/decision/class/r3` | settled / canonical-contract | `slice_scope_error` is a wrong decomposition, ownership, or implementation boundary. |
| `eval-v1:adr/620/decision/class/r4` | settled / canonical-contract | `reviewer_false_claim` is a reviewer assertion disproved by source, tests, or reproduction. |
| `eval-v1:adr/620/decision/class/r5` | settled / canonical-contract | `speculative_preference` lacks product, acceptance, or charter grounding, or targets an unreachable non-trust-boundary state. |
| `eval-v1:adr/620/decision/class/r6` | settled / canonical-contract | `fix_introduced_defect` was absent at the reviewed head and appeared in a later reviewer/reviser change. |
| `eval-v1:adr/620/decision/orthogonality/p1` | settled / canonical-contract | `validation_state`, review action, and severity are independent of failure class and cannot select, alias, change, or imply it. |
| `eval-v1:adr/620/decision/governance/p1` | settled / canonical-contract | The six identifiers are complete; aliases and merging are rejected, and classification cannot automatically mutate policy. |
| `eval-v1:adr/626/decision/bundle/p1` | settled / semantic-bundle | One manifest-rooted bundle owns Evaluation v1: candidate JSON owns declarative policy; the bound module owns schedule, lifecycle, authority, blinding, exact-arithmetic, bootstrap, eligibility, and Evidence calculations. |
| `eval-v1:adr/626/decision/binding/intro-p1` | settled / canonical-contract | The root candidate contains the exact semantic-module binding. |
| `eval-v1:adr/626/decision/binding/code1` | settled / canonical-contract | The binding has exactly `interface_version`, fixed module `path`, and whole-source `source_sha256`. |
| `eval-v1:adr/626/decision/interface/intro-p1` | settled / semantic-module | Interface version `evaluation-semantics-v1` identifies the module's one public operation. |
| `eval-v1:adr/626/decision/interface/code1` | settled / semantic-module | The exact public interface is `evaluate_v1(contract, operation_id, input_value) -> result_value`. |
| `eval-v1:adr/626/decision/operation/p1` | settled / semantic-bundle | The contract supplies every policy value; the bound module uses exact arithmetic with no fallback policy constants; production calls those exact bytes. |
| `eval-v1:adr/626/decision/purity/p1` | settled / semantic-module | The module obeys the closed source-size, stdlib-import, purity, and forbidden-capability boundary. |
| `eval-v1:adr/626/decision/open-set/intro-p1` | settled / checker | The checker opens one closed three-file set from its own repository root with regular-file and no-follow checks. |
| `eval-v1:adr/626/decision/open-set/n1` | settled / checker | The first opened path is the fixed candidate JSON. |
| `eval-v1:adr/626/decision/open-set/n2` | settled / checker | The second opened path is the fixed conformance JSON. |
| `eval-v1:adr/626/decision/open-set/n3` | settled / checker | The third opened path is the fixed bound semantic module. |
| `eval-v1:adr/626/decision/locks/p1` | settled / checker | The checker owns three independent whole-file locks and verifies the candidate's module binding against the fixed source. |
| `eval-v1:adr/626/decision/loading/p1` | settled / checker | The checker owns deterministic source/AST/import/public-surface audit and restricted safe loading, but no Evaluation operation. |
| `eval-v1:adr/626/decision/checker/p1` | settled / checker | The checker independently validates structure, canonical bytes, references, coverage, and digests, safely dispatches vectors, and compares expected results. |
| `eval-v1:adr/626/decision/independence/p1` | settled / checker | The checker does not rederive schedule, bootstrap, lifecycle, authority, blinding, eligibility, or Evidence algorithms. |
| `eval-v1:adr/626/decision/supersession/p1` | settled / semantic-bundle | Only ADR 605's one-data-file clause is superseded; single authority, versioning, and no duplicated policy survive. |

The grammar and table contain exactly 66 source locators: one Outcome sentence, two
Scope sentences, six Versioned-contract bullets, five eligibility gates, fourteen
acceptance bullets, five out-of-scope bullets, eight ADR 605/606 Decision sentences,
nine ADR 620 Decision locators, and sixteen ADR 626 Decision locators. An
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
   (`^[a-f0-9]{64}$`), without an algorithm prefix. A digest-bearing record's
   preimage is its canonical top-level object with exactly its top-level `digest`
   member omitted; no digest includes itself. Nested members named `digest` cannot
   identify an enclosing object. Bundle digests cover a separate digest-member-omitted
   bundle object containing sorted `{path,digest}` entries. Reject a missing
   or non-string required digest, a prohibited nested self-identifier, or a mismatch.
   No digest includes a mutable ref, current time, absolute path, or process environment.
   Separately, the standalone checker's checked-in source owns three reviewed,
   immutable 64-lowercase-hex literal constants: one whole-file SHA-256 lock for
   `contract-v1.candidate.json`, one for `contract-v1.conformance.json`, and one
   for `agentflow/evaluation_semantics_v1.py`. No literal is supplied by a candidate,
   conformance report, environment, configuration, or test fixture. The checker checks
   those exact file bytes before accepting their
   in-file bindings, so a coordinated mutation that recomputes a record, bundle,
   source, or report binding still fails `E_DIGEST` unless the independently reviewed
   checker lock changes.
   The #617 tests mutate one byte in each locked artifact independently, without
   changing the checker locks, and require all three mutations to fail acceptance.
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
lexically; it opens only the two fixed candidate JSON files and the one fixed bound
semantic module, from the repository root, with no-follow semantics and accepts only
regular files.

| Limit | Exact default |
| --- | ---: |
| one JSON artifact | 1 MiB |
| semantic module source | 64 KiB |
| JSON nesting | 32 containers |
| object members / array entries | 256 each |
| definitions | 64 |
| references per schema | 64 |
| path depth | 12 |
| generated cases per generation object | 256 |
| generated case bytes | 64 KiB each |
| generated corpus bytes | 8 MiB (output invariant) |
| checker stdout or stderr | 4 KiB each (output invariant) |

Every limit other than the two marked output invariants is an independently reachable
input bound: the checker fixtures include one exact-limit accepted input and one
limit-plus-one rejected input for each. The generated-corpus and output-stream caps are
postcondition invariants, not aggregate input limits and not manufactured exact/plus-one
inputs. In particular, no 16 MiB aggregate candidate-input bound exists.

### Generated-case byte mapping

Every candidate contains exactly one nonempty bounded generic `generation` object with
this exact shape. It contains no Evaluation rule, label, metric, threshold, or expected
result.

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
the exact specified byte mutation. Each in-memory generated-case replay record is
canonical ASCII JSON containing the generated ID and the payload as lowercase
two-hex-digit-per-byte `input_bytes_hex`; this record serialization, not an intentionally
malformed raw payload, is what “emits canonical bytes” means.

Validate templates in sorted order: exact shape and canonical operand, base-case
resolution, target syntax/range, operation preconditions, generated-ID collision, then
per-case limits and the cumulative output invariant. Respectively fail `E_GENERATOR_TEMPLATE`,
`E_GENERATOR_TARGET`, `E_GENERATOR_PRECONDITION`, `E_GENERATOR_COLLISION`, or
`E_GENERATOR_LIMIT`; a later template is never considered after the first failure.
No system RNG, clock, filesystem enumeration, provider, or network input is permitted.
During validation the checker always reconstructs every generated record from the 1..256
templates in ascending bytewise template-ID order as canonical ASCII JSON lines with
exactly one trailing `LF` per record. The stream digest is SHA-256 over the concatenated
record bytes and is the required top-level 64-lowercase-hex
`generated_stream_sha256` member of every candidate. The checker deterministically
replays that stream, compares the binding, and performs no writes, temporary-file
creation, or persistence. A digest mismatch is `E_DIGEST`; a replay over an output
invariant is `E_GENERATOR_LIMIT`.
Generated cases exercise only generic bytes, parser, reference, and bound mechanics;
they cannot replace the required frozen corpus, independent holdouts, six failure-class
coverage, or planted review-round scenario.

### Bound semantic module

The candidate root contains exactly this additional binding:

```json
{
  "semantic_module": {
    "interface_version": "evaluation-semantics-v1",
    "path": "agentflow/evaluation_semantics_v1.py",
    "source_sha256": "<64 lowercase hexadecimal characters>"
  }
}
```

The path and interface version are exact literals. `source_sha256` follows the existing
digest grammar and equals both the opened module's whole-file SHA-256 and the checker's
independent reviewed lock. The interface version exposes exactly one public operation:

```text
evaluate_v1(contract, operation_id, input_value) -> result_value
```

The candidate JSON remains the declarative authority for schemas, enums, artifact roles
and paths, thresholds, denominators, bounds, truth tables, authority parameters, and
Evidence projections. The module is the bundle's sole procedural authority for schedule,
lifecycle evaluation, authority and blinding transitions, exact arithmetic, bootstrap,
and Evidence constructor projections. It uses only exact `int` and
`fractions.Fraction`; every threshold, draw count, bound, path, role, scope, and policy
value comes from `contract`. There is no hidden fallback threshold or policy constant.

The module source is UTF-8 without BOM or CR bytes, uses LF line endings, and has exactly
one trailing LF. It is pure Python using only the standard library. Its only permitted
static imports are `from fractions import Fraction` and `from hashlib import sha256`.
Source and execution may not use
the filesystem, network, environment, clock, randomness, subprocesses, sockets, dynamic
imports, `eval`, `exec`, or plugin registries. Top level contains only a module docstring,
the approved imports, and function definitions without decorators, annotations, or
default expressions; `evaluate_v1` is the only public callable.

After checking the 64 KiB source limit, whole-file lock, candidate digest binding, and
UTF-8 decoding, the checker parses the source with `ast`. It rejects any other import,
top-level executable statement, forbidden API or name, dunder or reflection access, and
reference to `eval`, `exec`, `compile`, `open`, or `__import__`. It compiles the audited
tree and executes it once in a fresh namespace. The exact builtins are `abs`, `all`,
`any`, `bool`, `bytes`, `dict`, `enumerate`, `int`, `isinstance`, `len`, `list`, `max`,
`min`, `range`, `reversed`, `set`, `sorted`, `str`, `sum`, `tuple`, `zip`, `KeyError`,
`TypeError`, and `ValueError`, plus an import hook restricted to the two approved static
imports. The restricted load is checker mechanics, not an Evaluation operation. Human
review audits the complete locked source for hidden defaults or policy constants that a
syntactic audit cannot classify.

The module reuses the existing error registry. Failure to open its fixed regular path is
`E_IO`; exceeding 64 KiB is `E_LIMIT`; invalid source encoding is `E_UTF8`; a binding or
whole-file mismatch is `E_DIGEST`; and a forbidden AST/import/public surface, execution
exception, invalid result, or vector mismatch is `E_SEMANTIC`. A malformed candidate
binding remains `E_SCHEMA`, or `E_PATH` when its path violates the closed path rule. No
new public code or precedence rule is added.

Production and conformance both call these exact digest-bound module bytes. Neither
translates nor reimplements an algorithm.

### Validation order and checker interface

The zero-argument public checker is exactly:

```text
uv run python scripts/check-evaluation-contract-candidate.py
```

It is a standalone stdlib script: it imports no AgentFlow product or runtime module,
finds the repository root from its own checked-in `scripts/` location rather than the
current directory, and loads only the audited source named below.

```text
docs/evaluation/design/contract-v1.candidate.json
docs/evaluation/design/contract-v1.conformance.json
agentflow/evaluation_semantics_v1.py
```

Other runtime modules and runtime artifacts are #614 work and are deliberately outside
this checker interface. The checker opens the three displayed paths in that order; it
does not follow a candidate-supplied alternate path, import the `agentflow` package, scan
a directory, open a cache, or read configuration or environment state. Repeated runs
from any working directory over identical bytes produce byte-identical output.

It validates the bundle and stops at the first failing artifact. The existing closed
total order for its 28 public codes remains unchanged: 27 validation codes followed by
`E_INTERNAL`:

```text
E_ROOT < E_SOURCE_DRIFT < E_SOURCE_LOCATOR < E_REQUIREMENT_DUPLICATE <
E_REQUIREMENT_MISSING < E_IO < E_LIMIT < E_UTF8 < E_JSON < E_DUPLICATE_KEY <
E_CANONICAL < E_SCHEMA < E_REF < E_REF_CYCLE < E_REF_UNUSED < E_PATH < E_DIGEST <
E_ID < E_CROSS_REFERENCE < E_LINEAGE < E_ORACLE < E_GENERATOR_TEMPLATE <
E_GENERATOR_TARGET < E_GENERATOR_PRECONDITION < E_GENERATOR_COLLISION <
E_GENERATOR_LIMIT < E_SEMANTIC < E_INTERNAL
```

Within one artifact, the first applicable code in that order wins; ties across artifacts
use the displayed path order; within a collection, IDs and paths sort bytewise.
`E_INTERNAL` is reachable only for an unexpected checker fault after its guarded
validation path, is last in the registry, and never exposes a traceback. The checker
independently validates structure and canonical bytes, including schema, reference,
coverage, path, bound, lineage-reference, and digest integrity. It audits the module's
source, AST, imports, and public interface, safely dispatches every declared vector
through the bound `evaluate_v1`, and compares the actual and expected results. It
implements no Evaluation calculation and never uses an expected result as a rule source.

The exact output bytes, streams, paths, and exits are closed. On success stdout is
exactly the following ASCII bytes and stderr is empty (`b""`); exit is `0`:

```json
{"checked":3,"format":"evaluation-contract-candidate-check-v1","status":"ok"}
```

The success byte sequence is
`b'{"checked":3,"format":"evaluation-contract-candidate-check-v1","status":"ok"}\x0a'`.
For a validation failure other than `E_ROOT` or `E_INTERNAL`, stdout is empty (`b""`);
stderr is exactly `canonical_json({"code":CODE,"format":"evaluation-contract-candidate-check-v1","path":PATH,"status":"error"}) + b"\x0a"`;
exit is `1`. `PATH` is exactly
`docs/evaluation/design/contract-v1.candidate.json` for a candidate-artifact error or
`docs/evaluation/design/contract-v1.conformance.json` for a conformance-artifact error;
module byte, digest, source, import, interface, or execution errors name exactly
`agentflow/evaluation_semantics_v1.py`; this includes `E_SOURCE_DRIFT`,
`E_SOURCE_LOCATOR`, `E_REQUIREMENT_DUPLICATE`, and
`E_REQUIREMENT_MISSING` when the malformed binding or inventory is in that artifact.
For `E_ROOT`, stdout is empty and stderr is exactly
`b'{"code":"E_ROOT","format":"evaluation-contract-candidate-check-v1","path":"scripts/check-evaluation-contract-candidate.py","status":"error"}\x0a'`;
exit is `1`. For `E_INTERNAL`, stdout is empty and stderr is exactly
`b'{"code":"E_INTERNAL","format":"evaluation-contract-candidate-check-v1","path":"scripts/check-evaluation-contract-candidate.py","status":"error"}\x0a'`;
exit is `2`. No other bytes, diagnostics, paths, or streams are permitted. An
ordinary artifact-error line therefore has this exact shape, for example:

```json
{"code":"E_DIGEST","format":"evaluation-contract-candidate-check-v1","path":"docs/evaluation/design/contract-v1.candidate.json","status":"error"}
```

It neither repairs nor writes any bundle file and creates no temporary file, cache, or
bytecode artifact.

The required #617 checker-fixture suite assigns exactly one isolated checker-fixture owner
to each registry code, named `error-` plus the lowercase code with `_` changed to `-`.
Each fixture has one intended failing condition and asserts its code, exact stream bytes,
path, and exit. `error-e-internal` uses a test-only controlled internal-fault seam after
the guarded validation path; it asserts the public `E_INTERNAL` bytes and exit without
exposing a traceback. The suite also maintains an explicit reachability matrix and a
two-fault fixture for every jointly reachable unordered pair of error codes; each asserts
the earlier code in the closed total order. The matrix documents why any omitted pair is
mutually unreachable under the checker interface; it may not fabricate impossible faults.
This ownership and pair matrix is mechanical test evidence, not Evaluation fixture semantics.

## Independent case oracle

The checker has two deliberately separate roles. First, it independently validates the
bundle's structure, bytes, references, requirement coverage, and digests and audits the
module's full source, AST, imports, and public interface. Second, it safely dispatches
the candidate's conformance vectors through the canonical bound module and compares
results. The candidate's cases and expected outcomes remain independently reviewed
evidence; they are not a second algorithm and are never passed to the module as policy.

For each semantic case, the checker resolves the operation and input from the validated
case map, calls `evaluate_v1(contract, operation_id, input_value)`, and captures the
result before it reads that case's `expected_result`. The module receives neither the
expected value nor the conformance report. The checker then validates and compares the
actual and expected result with canonical JSON. A module exception, nonconforming result,
or mismatch is `E_SEMANTIC`; the existing code order, output framing, and exit remain
unchanged.

For each semantic case, the checker structurally resolves `case_id` in the validated
case map, requires the exact case-manifest and answer-key references, and validates the
receipt's canonical digest binding before dispatch. The bound module evaluates the
ADR 606 lineage join and returns its result for comparison. A candidate therefore cannot
change a case, its answer key, and its expected result into a self-consistent but unbound
fiction, and the checker still carries no lineage algorithm.

The conformance report maps each applicable authoritative requirement to exactly one
candidate rule ID and at least one declared positive or negative case ID. The required
swapped critical-miss, equality-threshold, bootstrap-replay, partial-token, and
partial-round cases remain candidate data; the checker verifies their structure and
executes them through the bound module. Checker tests protect structural validation,
module audit/loading, vector dispatch/comparison, and rejection behavior, not a duplicate
production semantic registry.
The report may not claim zero `unresolved` until the failure-class decision below is
made; #617's acceptance condition then requires zero unresolved, unmapped applicable
requirements, and duplicate owners.

This closes independent validation at the bundle boundary. The checker does not
rederive schedule, bootstrap, lifecycle, authority, blinding, eligibility, or Evidence
algorithms. Stronger algorithm rederivation would create a second semantic authority and
conflict with [ADR 605](../adr/adr-605-canonical-evaluation-rulebook.md), as narrowed by
[ADR 626](../adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md).

## ADR 626 revision preflight

### Generated facts

| Fact | Command | Output |
| --- | --- | --- |
| Source map | Extract and de-duplicate inventory IDs; count the `adr/626` subset | `66` total, `66` unique, `16` ADR 626 locators. |
| Closed owners | Compare all `settled / checker` IDs with the exact allowed set; assert the named procedural and bundle rows | Checker rows `9`, exact set passes; procedural-module rows `21`; semantic-bundle rows `4`. |
| Fixed mechanics | Extract the checker-open fence, error registry, limit row, lock count, and success JSON | Opened artifacts `3`; locks `3`; public errors `28` (`27` validation plus `E_INTERNAL`); module bound `65536` bytes; success `checked` value `3`. |
| ADR 626 source pin | `git show c13cc6b77bac94ab71b4c689aeef7f2eaa242be3:docs/adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md \| shasum -a 256` | `6bccfb0848fd1ad985c1442e1cb8dbb633ded8bd0f10878e80b0876c9e17a13e`. |
| Preserved unrelated mechanics | SHA-256 of `Mechanical contract` up to `Bound semantic module`, at `c13cc6b` and in the working tree | Both `7a7cc81b3976b3a0f598e8f1faaab8d889b0ddc6d5447dfe5c6c59b25eaedb97`. |

### Scratch text and contradiction audit

`sh /tmp/agentflow-626-doc-audit.sh "$PWD"` compared the ADR and preflight binding,
enumerated and de-duplicated the source map, required the exact closed checker-owner set,
asserted every routed procedural/bundle owner, verified the ADR source pin, compared the
unrelated mechanical section to `c13cc6b`, and searched active text for the superseded
boundary's distinctive phrases. It reported:

```text
source_locators=66 unique=66 adr626=16
owner_vocabulary=closed
checker_owned_rows=9 exact_set=pass
procedural_module_rows=21 audited=pass
semantic_bundle_rows=4 audited=pass
opened_artifacts=3 whole_file_locks=3
public_error_codes=28 validation_codes=27 internal_codes=1
module_source_bound_bytes=65536 success_checked=3
adr626_source_sha256=6bccfb0848fd1ad985c1442e1cb8dbb633ded8bd0f10878e80b0876c9e17a13e
unrelated_mechanical_sha256=7a7cc81b3976b3a0f598e8f1faaab8d889b0ddc6d5447dfe5c6c59b25eaedb97
binding_match=pass stale_active_claims=0 ownership_contradictions=0
historical_rejection_hits=4
```

The four historical hits are confined to ADR 626's Context and rejected Alternative;
they explain the superseded interpreter/VM boundary and are not active requirements.
The scratch script is throwaway and is not committed.

### Revision checks

| Gate | Command | Exit / result |
| --- | --- | --- |
| Diff whitespace | `git diff --check` | Exit `0`; no output. |
| Public links | `uv run python scripts/check-public-doc-links.py` | Exit `0`; `Documentation link check passed for 13 relative link(s).` |
| Focused tests | `uv run pytest -q tests/test_evidence_contract.py tests/test_reviewer.py` | Exit `0`; `80 passed in 0.72s`. |
| Changed paths | `git status --short` | Only `docs/research/evaluation-candidate-preflight.md`. |

## Historical #618 captured verification results

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
| Candidate paths | `git ls-files --error-unmatch docs/evaluation/design/contract-v1.candidate.json docs/evaluation/design/contract-v1.conformance.json agentflow/evaluation_semantics_v1.py scripts/check-evaluation-contract-candidate.py tests/test_evaluation_contract_candidate.py` | Exactly the five #617 deliverable paths print. |
| Candidate checker | `uv run python scripts/check-evaluation-contract-candidate.py` | Exact zero-argument three-file success line, exit `0`; no AgentFlow product/runtime import occurs. |
| Focused candidate tests | `uv run pytest -q tests/test_evaluation_contract_candidate.py` | Candidate data coverage, module audit/loading, conformance execution, byte, bound, lineage, and rejection-case tests pass. |
| Repository acceptance | `uv run pytest -q` | Entire Python suite passes. |

## Product decision closure

At the pinned #583/ADR 605/ADR 606 source revision, #583 required six failure classes
but did not name them, so the preflight correctly left that product choice unresolved.
[ADR 620](../adr/adr-620-evaluation-failure-classes.md) now closes it from the
validated #573 taxonomy: `original_defect`, `plan_gap`, `slice_scope_error`,
`reviewer_false_claim`, `speculative_preference`, and `fix_introduced_defect`.
The #617 candidate conformance report must map the nine pinned ADR 620 locators,
including those exact identifiers and meanings, before claiming zero unresolved
dispositions. ADR 626 adds its 16 pinned boundary locators, extending the complete source
inventory to 66 locators.

All absent field shapes, artifacts, bindings, and computations fail closed. Any later
request to add a metric, threshold, case class, or promotion behavior requires a new
reviewed semantic-bundle version under ADR 605 as narrowed by ADR 626; it is not a
preflight default.
