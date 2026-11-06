# Contributing

Thanks for helping. Bug reports, ideas and pull requests are all welcome. For anything larger than a small fix, open
an issue first.

## Development

The tests need no network, no Garmin account and no Hermes checkout:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python -m pytest -q
hermes plugins validate .
```

CI runs the tests on Python 3.11 and 3.13 and `hermes plugins validate --install-deps` against the latest stable
Hermes Agent release. Run `--install-deps` locally only in a throwaway Hermes install: it installs into Hermes'
own environment.

## Conventions

- Use only Hermes' public plugin extension points and the Desktop plugin SDK. Never patch Hermes core or Desktop.
- The plugin stays read-only against Garmin and talks to no host other than Garmin's Personal API.
- The API key and token never reach the model, the chat, logs or disk. Tests and fixtures use synthetic values only:
  never commit a real key, real health data or real activity files.
- Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for every user-visible change.

## Releases

Maintainers release by:
1. Moving the `Unreleased` entries under a new version heading.
2. Bumping the version in `plugin.yaml`, `pyproject.toml`, `dashboard/manifest.json` and `__version__` in
   `hermes_garmin/__init__.py`.
3. Pushing a tag `vX.Y.Z`, then opening a SHA-bump PR for the catalog entry.

By contributing you agree that your work is licensed under the [MIT License](LICENSE).
