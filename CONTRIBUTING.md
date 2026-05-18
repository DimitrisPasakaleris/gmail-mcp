# Contributing

Thanks for taking the time to contribute. This document walks through getting a
dev environment up, running tests, and submitting a change.

## Quick start

```bash
git clone https://github.com/CHANGE_ME/gmail-mcp.git
cd gmail-mcp
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not integration"
```

That's the full dev loop. The package is installed editable, so changes under
`src/gmail_mcp/` are picked up immediately.

## Project layout

```
src/gmail_mcp/        package source
tests/                unit tests (no network)
tests/integration/    integration tests (real Gmail; gated on GMAIL_TEST_REAL=1)
docs/                 contributor-facing documentation
.github/              CI workflow + issue/PR templates
```

## Tests

- **Unit tests** mock the Google client and never touch the network. They are
  the default `pytest` target.
- **Integration tests** are skipped by default. To run them you need a working
  `credentials.json` + completed OAuth flow, then:
  ```bash
  GMAIL_TEST_REAL=1 pytest -m integration
  ```
  Treat any test using `mcp__Gmail_MCP__send_email`-class real actions with
  caution — they touch a real mailbox.

## Lint and type-check

```bash
ruff check .
ruff format --check .
mypy src
```

`pre-commit` runs these on commit; `ruff format` writes changes in-place if
you want to apply them.

## Submitting a change

1. Open an issue first for anything non-trivial — it saves cycles.
2. Branch from `main`, keep the change focused.
3. Add or update tests. Bug fixes should include a test that fails without the fix.
4. Update `CHANGELOG.md` under `## [Unreleased]`.
5. Make sure CI is green locally: `pytest`, `ruff check .`, `mypy src`.
6. Open a pull request; the PR template will walk you through the checklist.

## Release process

Maintainers only:

1. Bump the version in `src/gmail_mcp/__init__.py` **and** `pyproject.toml`.
2. Move the `## [Unreleased]` section in `CHANGELOG.md` under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading.
3. `git tag vX.Y.Z && git push --tags`
4. GitHub Actions builds the wheel and sdist; attach them to the GitHub release.

## Code style notes

- Python 3.11+. We use modern typing (`X | None`, `list[str]`, `dict[str, Any]`).
- Public functions get a docstring explaining params and return shape; the
  existing `tools.py` is the reference.
- Keep error responses going through `ok(...)` and `err(...)` so the contract
  with MCP clients stays consistent.
- Add `logger.debug(...)` around third-party API calls rather than `print(...)`.

## Security

If you find a security issue, please **do not** open a public issue. See
[`SECURITY.md`](SECURITY.md) for the disclosure path.
