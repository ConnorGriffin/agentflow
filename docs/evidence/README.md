# Evidence contracts

Evidence has three independent version namespaces. Python keeps the existing `Observation`
and adds `EvidenceEnvelopeV2`; JSON producer contracts use contract v1 or contract v2; durable
storage uses SQLite schema v3. A number in one namespace does not imply the same number in another.

`contract-v1.json` and `contract-v2.json` are the normative wire contracts. Fixture filenames route
to exactly one manifest and parser by their terminal `-v1.json` or `-v2.json` suffix.
