# Changelog

## 0.3.0 — 2026-09-08

### Added

- Bundled recovery skill (`hermes_delegate_routing/skills/delegate-routing/`):
  `register()` now calls `ctx.register_skill("delegate-routing", ...)` so the
  skill is loadable as `skill_view("delegate_routing:delegate-routing")`.
  Plugin skills are read-only and namespaced — they do not enter
  `~/.hermes/skills/` nor `<available_skills>` — so this is a recovery target,
  not a discovery channel. Registration is best-effort and never breaks
  startup (`ctx=None` or missing `register_skill` is a safe no-op).
- Fail-closed routing errors now point at the bundled skill and include a
  minimal `tasks[]` JSON example, so a bad literal is recoverable in one step.
- Seam A schema descriptions are now self-contained mini-manuals: routing
  lives inside `tasks[i]` only (no top-level argument — it is dropped),
  model-only tasks stay on the inherited provider (set both to cross
  providers), literals must match exactly, and unresolvable values fail the
  whole call instead of silently running another model.

### Packaging

- `pyproject.toml` `package-data` now ships the bundled skill
  (`skills/delegate-routing/SKILL.md` + `references/*.md`); verified present
  in both wheel and sdist. No `MANIFEST.in` needed (setuptools
  `package-data` covers both artifacts).

## 0.2.4 — 2026-09-07

### Fixed

- Seam D (async display) follows the formatter move: newer hosts render
  delegation completions from `tools.process_registry_notifications`
  instead of `tools.process_registry`. The patch now tries each known
  location (newest first) and wraps every live one, so heterogeneous
  fan-out again reports `Model: per-task` with the task-to-model mapping
  instead of the stale batch model. Display still degrades loudly and
  independently when no known formatter exists.

## 0.2.3 — 2026-09-07

### Fixed

- Fixed a re-entrant deadlock in `apply_patches()`: importing a host module
  mid-patch could re-enter the plugin loader (some hosts discover plugins at
  `model_tools` import time), which calls `register()` → `apply_patches()`
  on the same thread and deadlocked on the patch lock. Host modules are now
  pre-imported before locking (a nested pass completes first and sets the
  sentinel), and the lock is an `RLock` as a backstop. Regression test in
  `tests/test_reentrancy.py` (fails pre-fix via join timeout, passes post-fix).

## 0.2.2 — 2026-09-07

### Fixed

- Project metadata now points Homepage/Repository/Issues/Changelog at the maintained
  `teich0psia/hermes-delegate-routing` fork instead of the upstream `b3nw` fork.
- Explicit per-task `reasoning_effort` now fails closed if a future Hermes child
  object no longer exposes `reasoning_config`. The plugin logs a warning and raises
  `ValueError` rather than silently attaching a dead attribute and pretending the
  override took effect.

## 0.2.1 — 2026-09-07

### Fixed

- Async delegation completion notices now report the actual per-task child model
  from `results[].model` instead of the stale batch/default `delegation.model`.
  Homogeneous batches show the real model directly; heterogeneous fan-out shows
  `Model: per-task` plus a compact task-to-model mapping.
- The display correction is plugin-only and reuses Hermes' native async formatter;
  if that formatter is unavailable on a future host, routing remains active and
  only the display fix degrades with a warning.
- Added an end-to-end main-profile chat verification where the parent model emitted
  a `delegate_task` call for `deepseek-v4-flash`/`deepseek`/`low`, the child actually
  called DeepSeek, and the async completion re-entered the parent chat showing
  `Model: deepseek-v4-flash`.

## 0.2.0 — 2026-09-07

### Added

- Per-task `reasoning_effort` on `delegate_task(tasks=[...])`, parsed by Hermes'
  own `parse_reasoning_effort()` and applied to the constructed child agent.
- Precedence is now explicit per field: `tasks[i].*` → `delegation.*` → parent
  inheritance. Omitting `reasoning_effort` leaves Hermes' native delegation
  reasoning resolution untouched.
- Request-boundary regression coverage proving distinct per-task reasoning
  efforts reach Hermes' normal provider transport path.

### Changed

- Verified compatibility with the installed Hermes Agent 0.21.0 checkout
  (`63279301bcbdc185c1b07b98a9312eb0c862f26d`).
- Per-task model/provider resolution now preserves newer host
  `request_overrides` and `max_output_tokens` metadata when available, while
  retaining the previous pass-through behavior on older hosts.
- Fixed per-field precedence for partial task overrides: model-only tasks now
  inherit/pin `delegation.provider` before the parent provider, and provider-only
  tasks inherit `delegation.model` before the parent model instead of falling
  through to `/model` auto-detection.
- Host-backed E2E fixtures tolerate the current delegate goal validation and
  optional reasoning response fields without depending on the operator's
  configured delegation provider.

## 0.1.2 — 2026-07-24

### Fixed

- **Seam A now invalidates the host's tool-definitions cache.** Advertising
  `tasks[].model`/`.provider` mutates `entry.dynamic_schema_overrides` directly,
  which does not bump `registry._generation`. `model_tools.get_tool_definitions`
  (the agent loop's `quiet_mode=True` fast path) memoizes on that generation, so
  a long-running gateway that cached the stock `delegate_task` schema *before*
  the plugin patched kept serving the field-less schema for the life of the
  process — the LLM never saw the new fields and per-task routing silently
  no-op'd. `_patch_schema` now bumps `registry._generation` and clears
  `_tool_defs_cache` after wrapping the entry (both best-effort/guarded).
  Regression test in `tests/test_schema_cache_invalidation.py`.

## 0.1.1 — 2026-07-24

Initial public release.

- Per-task `model` / `provider` routing for `delegate_task` via `tasks[i].model`
  and `tasks[i].provider` (top-level args are dropped by the host and are not
  supported — matches upstream's recommended call shape).
- Three-seam, load-time monkeypatch of `tools.delegate_tool`
  (schema advertise → capture per-task creds → apply per child), because
  `delegate_task` bypasses the tool registry (so `register_tool(override=True)`
  can't intercept it). Signature-guarded and idempotent; degrades to a no-op on
  an unsupported host.
- Vendored model/provider resolver reusing the host `/model` switch pipeline;
  reads `parse_model_flags` positionally to tolerate return-arity drift.
- `on_error: fail | fallback` config toggle (default `fail`).
- Tier-1 end-to-end test (`tests/test_e2e_routing.py`) asserting per-task creds
  reach the child's OpenAI client boundary, wired as an opt-in CI `e2e` job.
- CI lint/type-check gates (`ruff`, `mypy`) alongside the host-free unit matrix.
- Verified against upstream hermes-agent **0.19.0** (tag `v2026.7.20`) and
  **0.18.0**. On 0.19.0 the two new `_build_child_agent` params
  (`override_max_tokens`, `override_request_overrides`) pass through the apply
  seam's keyword forwarding unchanged.

Packaging: dropped the unused `plugin.yaml` — the host reads it only for
directory plugins, never for pip/entry-point plugins like this one.
