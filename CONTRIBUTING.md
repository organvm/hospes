# Contributing to HOSPES

Thank you for helping improve HOSPES. Contributions are welcome as issues, design discussions,
documentation, tests, and pull requests.

## Before you start

- Use an issue for substantial behavior or schema changes so maintainers can confirm scope.
- Never submit credentials, real contact details, correspondence, unpublished media, contracts,
  private relationship notes, or production data.
- Use synthetic identities and `example.test`, `example.com`, or `example.org` addresses in tests.
- Keep external communication draft-only. No contribution may add an autonomous send path.

## Development

HOSPES requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test,api]'
pytest
```

Run the repository's full completion predicate before opening a pull request:

```bash
./done.sh
```

## Pull requests

- Work in a focused branch and keep `main` releasable.
- Add or update tests for behavior changes.
- Update schemas and packaged-resource mirrors together.
- Explain the authority boundary, failure behavior, and migration impact for consequential changes.
- Confirm the privacy scan passes and list any intentionally changed public fixtures.

By submitting a contribution, you agree that it is licensed under Apache License 2.0.
