# Contributing to AgentFlow

AgentFlow is an early macOS beta maintained in public. Focused bug fixes,
documentation improvements, and small compatibility changes are welcome.
Large features should start with an issue so their scope and fit can be settled
before implementation.

## Development setup

```bash
git clone https://github.com/ConnorGriffin/agentflow.git
cd agentflow
uv sync --group dev
uv run pytest -q
```

Console changes also require:

```bash
cd agentflow/webui
npm ci
npm test
npm run build
```

The built console is tracked. Commit changes under `agentflow/webui/dist/` with
the source change that produced them.

## Pull requests

- Keep each pull request narrowly scoped and explain the user-visible effect.
- Add or update tests for behavior changes.
- Run the Python and console checks that apply to the change.
- Never include credentials, private repository data, transcripts, or local
  environment paths.
- User-interface changes must begin with the repository's mockup workflow and
  follow `PRODUCT.md` and `DESIGN.md`.
- Security reports belong in the private channel described in `SECURITY.md`,
  not a public issue.

## Developer Certificate of Origin

AgentFlow uses the [Developer Certificate of Origin 1.1](DCO), not a CLA.
Sign off every commit:

```bash
git commit -s
```

The sign-off certifies that you have the right to submit the contribution under
the project's Apache-2.0 license. Pull requests containing unsigned commits will
not be merged.

## Review and acceptance

Passing CI is required but does not guarantee acceptance. Maintainers may ask
for changes or decline work that conflicts with the product direction, safety
model, compatibility policy, or maintenance capacity. Contributions are
licensed under Apache-2.0 on the same terms as the rest of the project.
