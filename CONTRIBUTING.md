# Contributing

Thanks for your interest in `hermes-delegate-routing`.

## Development setup

```bash
git clone https://github.com/b3nw/hermes-delegate-routing
cd hermes-delegate-routing
uv run --extra dev pytest      # unit tests — no hermes-agent host needed (uses fakes)
```

## Checks

CI runs three gates; all must pass before a PR merges:

```bash
uv run --extra dev pytest      # unit tests (host-free)
uv run --extra dev ruff check .  # lint
uv run --extra dev mypy        # type-check
```

## Host-backed tests

`tests/test_integration_smoke.py` and `tests/test_e2e_routing.py` exercise the
plugin against a real hermes-agent. They self-skip when no host is importable, so
they don't affect the default run. To run them, put a hermes-agent checkout/install
on `PYTHONPATH` (see [`docs/CI_E2E_TESTING.md`](docs/CI_E2E_TESTING.md)):

```bash
PYTHONPATH=/path/to/hermes-agent:. \
  /path/to/hermes-agent/.venv/bin/python -m pytest \
  tests/test_integration_smoke.py tests/test_e2e_routing.py
```

## Design context

The plugin couples to host internals by design (there is no public seam for
per-task delegate routing). Before changing the monkeypatch seams, read
[`docs/DESIGN.md`](docs/DESIGN.md) — especially §6 (the three seams) and §10
(coupling & the signature guard). New host versions can drift; the guard turns
drift into a safe no-op, and the host-backed tests are how we confirm a version
still works.

## Pull requests

- Keep changes focused; update `CHANGELOG.md` under an unreleased/next-version
  heading.
- If you verified against a new hermes-agent version, note it in the README
  support table.

## Releasing

This fork distributes GitHub Releases with attached wheel and sdist artifacts.
The PyPI name belongs to the original upstream project; this fork does not publish
there. To cut a release:

1. Bump the version in `pyproject.toml`, `hermes_delegate_routing/__init__.py`,
   `plugin.yaml`, and the bundled skill frontmatter; date the `CHANGELOG.md` entry.
2. Run the relevant tests, build with `uv build`, and validate both artifacts with
   `twine check`. Verify bundled skill files and version metadata in the artifacts.
3. Commit and merge to `main`, then push the specific annotated tag:
   `git tag -a vX.Y.Z -m "Release vX.Y.Z" && git push origin vX.Y.Z`.
4. Create a GitHub Release with `gh release create vX.Y.Z --verify-tag` and attach
   the validated wheel and sdist. Read back the tag, release and uploaded assets.

The `release` workflow builds and checks metadata on version-tag pushes; it does
not publish to PyPI or create the GitHub Release. When Actions has no recorded
run, report local build/test evidence separately rather than claiming remote CI.
