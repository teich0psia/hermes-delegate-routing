# Model parser compatibility repair

## Goal and roles

Restore explicit per-task model/provider delegation on the current installed Hermes without losing per-task Fast or older host compatibility. Main owns plan/design, review and acceptance; DeepSeek V4.1 Flash / opencode-go / high owns implementation through a separate Hermes CLI process.

## Verified starting state

- Plugin source: `/home/nedjem/projects/hermes-delegate-routing`, clean `main` at `eceda529c4ad51a65b042405e91326da4481fc63`, matching GitHub main.
- Plugin version 0.3.2 includes Fast and its tests.
- Installed host: v0.21.5+4636.g99721dc, source `/home/nedjem/.hermes/hermes-agent`.
- Host commit https://github.com/NousResearch/hermes-agent/commit/71fe5fccad0b104f91c529cb2f2254dfad0cfbd4 removes `parse_model_flags`; structured `parse_model_flags_detailed` remains.
- Deployed resolver reproduces the reported import error even in a new Python process.
- Upstream plugin main still calls the legacy parser; no existing repair was found in its public PRs/branches.

## Approved design

1. Keep the adaptation in the plugin resolver, not Hermes core.
2. Prefer `parse_model_flags_detailed`, reading `model_input` and `explicit_provider` attributes. This is the structured function that the removed wrapper itself called, so it preserves the old extraction semantics without introducing new `/model` command validation or persistence behavior.
3. When that callable is absent on an older host, use `parse_model_flags` and read its first two tuple elements as before. Use capability detection, not host version strings. Invocation failures of a present parser must propagate rather than silently selecting another parser.
4. Preserve structured-vs-inline provider conflicts, baseline precedence, route provenance, reasoning, Fast, and request metadata behavior.
5. Update focused unit fixtures and host-backed tests that currently patch the removed name. Cover modern-only and legacy-only hosts, modern preference when both exist, and a clear failure when neither parser exists.
6. Exercise the real installed host parser with the plugin resolver, then existing capture/apply/SDK-boundary and Fast coverage. Additional host seam regressions must be evidenced, reported, and addressed only to the extent necessary to restore this same delegation path.

## Scope and acceptance

- Work on a feature branch based on the clean verified source; one implementer in this tree.
- Make the smallest maintainable change that fully meets the request. Preserve unrelated behavior; avoid speculative abstractions and unrelated cleanup.
- Run relevant existing checks; add focused tests for concrete gaps. Stop once the requested behavior works and relevant checks pass.
- Unit/static checks and current-host integration/SDK-boundary tests must have real recorded results. Separate skipped/unavailable checks from passing checks.
- Main reviews the actual diff and verification evidence, then commits the accepted changes.
- Installation into live managed runtime, service restart, push, public PR and release are not included in this authorization. The live deployment and real-provider child inference remain separate acceptance gates after source validation.

## Status

Source implementation accepted after Main's review and independent verification on `fix/model-parser-compat`. The repaired source commit `ca311e786ef3251953fb00b35f424f08b59c86e7` is installed in the default profile's managed plugin runtime. No service restart or explicit plugin reload was performed; the user owns restart.

### Main verification

- Host-free suite: `130 passed, 3 skipped` (the host-required files are skipped in this run).
- Changed Python files: ruff passed; mypy with `--explicit-package-bases` passed.
- Current installed host source `99721dca`: smoke + SDK-boundary E2E + baseline preflight, `16 passed`. This includes explicit model/provider, reasoning, mixed Fast ON/OFF/omitted with inherited Fast both off and on, Codex request assembly and unsupported proxy rejection.
- Main inspected the complete code/test diff; requested and verified two new lint fixes, required structured-result attribute access, older-host fixture compatibility and current design documentation. Main corrected two documentation statements about the wrapper delegation direction and error behavior.
- Host-backed tests use an isolated scratch HERMES_HOME, with `delegation.oneshot_max_children: 8` only in that scratch home's config, and the actual source modules. SDK/catalog boundaries are recording fakes, not real provider inference.
- Reliable local execution required a minimal subprocess environment and the host interpreter, running the scratch sys.path-pinning runner with `-s`; a process exit of 0 with only the runner's preamble was not counted as a test pass. The observed accepted output explicitly contains `16 passed`.
- Whole-repository ruff still has six pre-existing errors in unchanged `patches.py`/`test_baseline_preflight.py`; they are outside this repair. Bare mypy's existing package-layout problem is avoided with the explicit package-bases option.

### Installed-artifact verification

- User authorized installation only and stated they would perform restart.
- Supported install: `hermes plugins install file:///home/nedjem/projects/hermes-delegate-routing --ref ca311e786ef3251953fb00b35f424f08b59c86e7 --force`, preserving existing plugin selection and omitting `--enable` to avoid explicit live activation. Initial non-interactive attempt was refused without changing installed state; the PTY retry accepted PM dependency preparation and completed successfully.
- Readback: `delegate_routing` remains enabled, source `git pinned@ca311e78`, version 0.3.2 (unreleased source fix; no version bump).
- Managed environment: `/home/nedjem/.hermes/installs/dda9e464ad7149ac/environments/a8343c86c5994b059ddd2199ddf00ffe/venv`.
- Fresh-process resolver import resolves from this environment's `workspace/plugin-sources/delegate_routing-5fbf246fca8c74fc/`, not the development repo. All plugin Python source files match the repaired commit. Real managed-host parser plus installed resolver pass the explicit Sol/openai-codex parsing probe; catalog/credential resolution is mocked, and no provider inference was sent.
- Plugin doctor: runtime discovery, manifest parsing, import and registration passed. Managed dependency check: 140 installed packages compatible.
- Gateway service PID/start time unchanged: 1040335 / 2026-09-30 03:50:59 JST. No stop/restart/reload command was issued. Pre-existing serve/dashboard manual-restart warnings are still present.

### Handoff

Installation is complete; live process adoption and real-provider delegation remain unverified. The user will restart Hermes. After the user confirms restart, verify fresh host/plugin load and replay explicit per-task model/provider delegation with one minimal real-provider child. Remote push/PR and release remain separately authorized actions.
