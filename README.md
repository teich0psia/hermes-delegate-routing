# hermes-delegate-routing

**A fork-free [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds explicit per-task `model` / `provider` / `reasoning_effort` routing to `delegate_task`.**

Route each subagent in a batch delegation to a different model/provider/reasoning effort:

```jsonc
delegate_task(tasks=[
  {"goal": "Summarize these logs",      "model": "gemini-flash-2.0", "provider": "openrouter", "reasoning_effort": "low"},
  {"goal": "Review this diff for bugs", "model": "sonnet",           "provider": "anthropic",  "reasoning_effort": "high"},
  {"goal": "Research the CVE",          "model": "deepseek-pro",     "provider": "deepseek",   "reasoning_effort": "medium"}
])
```

Per-task delegate routing was proposed upstream and not accepted; this plugin
delivers it as a standalone package, so there is no fork to maintain. Full design
and rationale: [`docs/DESIGN.md`](docs/DESIGN.md).

## Install

Install into the **same environment** as your hermes-agent, then enable it.

```bash
pip install "git+https://github.com/teich0psia/hermes-delegate-routing.git@main"
# or pin a release (see the Releases page for tags):
# pip install "git+https://github.com/teich0psia/hermes-delegate-routing.git@vX.Y.Z"
# or, for development from a checkout:
pip install -e /path/to/hermes-delegate-routing
```

The pip entry point (`hermes_agent.plugins`) makes hermes-agent auto-discover the
plugin; you still have to enable it in `config.yaml`:

```yaml
plugins:
  enabled: [delegate_routing]

# optional:
delegate_routing:
  on_error: fail   # "fail" (default) → a bad task routing override fails the call;
                   # "fallback"       → skip the override, use normal routing, log a warning
```

No `allow_tool_override` grant is needed — the plugin does not use the registry
override API (see "How it works").

## Usage

Put routing fields inside `tasks[]` — **even for a single task**:

```python
delegate_task(tasks=[{"goal": "…", "model": "sonnet", "provider": "anthropic", "reasoning_effort": "high"}])
```

- `model` — a model name/alias as used by `/model` (e.g. `sonnet`,
  `gemini-flash-2.0`), optionally with inline `--provider <id>`.
- `provider` — a configured provider id. Prefer this structured field over
  embedding `--provider` in `model`.
- `reasoning_effort` — the same effort vocabulary understood by the installed
  Hermes version (for current Hermes: `none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`, `ultra`). Hermes still owns provider-specific clamping and wire
  formatting.
- Precedence per field: `tasks[i].model/provider/reasoning_effort` → matching
  `delegation.*` config → parent agent inheritance.
- A model-only task keeps the inherited delegation/parent provider pinned; a
  provider-only task keeps the inherited delegation/parent model. Omitted fields
  do not silently fall through to `/model` auto-detection.
- Omitting a field preserves normal Hermes delegation behavior for that field.

**Top-level `delegate_task(model=…, provider=…, reasoning_effort=…)` is intentionally not supported** —
the host drops top-level args before the tool runs, so only `tasks[]` fields take
effect. This matches the recommended call shape (see [`docs/DESIGN.md`](docs/DESIGN.md)).

## Recovery skill

The package bundles a read-only skill, registered at load as
`skill_view("delegate_routing:delegate-routing")`. Plugin skills are explicit
loads only — not part of the system prompt's `<available_skills>` index
(though the host currently surfaces plugin-skill metadata via `skills_list`)
— so this skill is a **recovery target, not the discovery channel**: routing-failure
errors point at it with a minimal `tasks[]` example, and the per-task schema
descriptions are self-contained enough to call correctly without it. Unload
restores the pre-patch originals via `ctx.on_unload(restore_patches)`.

## How it works

`delegate_task` is special-cased in the host runtime
(`agent/agent_runtime_helpers.py`) to bypass the tool registry, so the sanctioned
`register_tool(override=True)` mechanism can't intercept it. Instead the plugin
installs four narrow, idempotent runtime monkeypatches at load:

1. **schema** — advertise `tasks[].model` / `tasks[].provider` /
   `tasks[].reasoning_effort` to the model (via the registered `ToolEntry`);
2. **capture** — wrap `delegate_task` to resolve per-task model/provider state via
   the host `/model` switch pipeline and reasoning via Hermes' `parse_reasoning_effort()`,
   then stash it by task index;
3. **apply** — wrap `_build_child_agent` to inject task routing per child. Hermes
   builds the child normally first, then an explicit task reasoning effort replaces
   the child's `reasoning_config`, so provider-specific request translation remains
   entirely in Hermes;
4. **async display** — wrap Hermes' async completion formatter so the parent sees
   the actual `results[].model` used by each child instead of the stale batch-level
   `delegation.model`. Mixed-model fan-out is labeled `Model: per-task` with a compact
   task-to-model mapping.

If the host isn't importable or its function signatures don't match,
`apply_patches()` **refuses to patch** and the plugin degrades to a no-op — core
behavior is never left half-patched. See [`docs/DESIGN.md`](docs/DESIGN.md).

## Supported versions

Verified against upstream [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent):

| hermes-agent | Status |
|---|---|
| `0.21.0` (checkout `63279301`, tested 2026-09-07) | ✅ verified — current schema/signatures, request-boundary routing, main-profile natural-language delegation, and async actual-model display E2E |
| `0.21.0` (checkout `693641aa`, tested 2026-09-07) | ✅ verified — routing seams unchanged; async display follows the formatter move to `process_registry_notifications` (live `Model: per-task` mapping confirmed) |
| `0.19.0` (tag [`v2026.7.20`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.20)) | ✅ verified — routing/signature/schema/reasoning compatibility; async display fix degrades safely if host formatter differs |
| `0.18.0` (tag `v2026.7.1`) | ✅ verified — routing/signature/schema/reasoning compatibility; host supports efforts through `xhigh` |

Because the plugin depends on host internals, new hermes-agent releases can
drift. The signature guard turns drift into a **safe no-op with a loud log**, not
a crash. File an issue if you hit an INACTIVE warning on a newer version.

## Development

```bash
uv run --extra dev pytest            # unit tests (no host needed; uses fakes)
uv run --extra dev ruff check .      # lint
uv run --extra dev mypy              # type-check

# host-backed tests (integration smoke + Tier-1 e2e) against a real host —
# they self-skip when no host is importable:
PYTHONPATH=/path/to/hermes-agent:. \
  /path/to/hermes-agent/.venv/bin/python -m pytest tests/test_integration_smoke.py tests/test_e2e_routing.py
```

The host-backed tests also run in CI as an opt-in `e2e` job — see
[`docs/CI_E2E_TESTING.md`](docs/CI_E2E_TESTING.md).

## License

MIT
