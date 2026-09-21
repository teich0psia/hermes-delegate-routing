# hermes-delegate-routing

**A fork-free [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds explicit per-task `model` / `provider` / `reasoning_effort` routing and optional `fast` control to `delegate_task`.**

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

## Optional per-task Fast mode

Add `fast` inside a task without changing the existing call shape:

```python
delegate_task(tasks=[
    {"goal": "Check the result", "model": "gpt-5.4", "provider": "openai-codex", "fast": True},
    {"goal": "Run at normal speed", "model": "gpt-5.4", "provider": "openai-codex", "fast": False},
    {"goal": "Use the usual delegation settings"},
])
```

| `tasks[i].fast` | Behavior |
|---|---|
| omitted | Existing delegation behavior, unchanged; **not** an implicit `false` |
| `true` | Enable Fast for this child only, if its resolved route supports it |
| `false` | Disable Fast for this child, including inherited Fast request flags (and a bounded `auto`/`cold` window, if a host ever gives children one) |

Only JSON booleans are accepted: `null`, strings, and numbers are invalid.
Fast never changes the parent, siblings, model, provider, reasoning effort, or
persistent configuration. It is the host's priority/Fast request option, not a
switch to a smaller model or lower reasoning effort. Provider billing rules still apply.

Hermes' own route-aware Fast resolver decides support and request parameters
(e.g. `service_tier: priority` for eligible OpenAI/Codex routes, `speed: fast` for
eligible Anthropic routes). The plugin does not send those flags to unsupported
proxies. Explicit OFF removes recognized Fast flags from copied request settings,
including `extra_body`, while preserving unrelated settings and non-Fast tiers.
Custom gateway-specific tier names are outside this option's scope.

Under the default `delegate_routing.on_error: fail`, an unsupported Fast request
or missing host capability fails the delegation before child execution; already
constructed children are closed and detached from the parent, so a rejected
batch leaves no child behind. With `on_error: fallback`, a Fast capability
failure warns and preserves that child's pre-existing Fast settings, keeping its
resolved model/provider/reasoning. Invalid input types are capture-time errors
and follow the existing policy of skipping the entire task override in fallback
mode. No Fast option is guaranteed applied in fallback mode.

Fast ON requires a host with `hermes_cli.models.resolve_fast_mode_overrides`;
explicit ON/OFF also requires child `request_overrides` and `service_tier`
attributes. Older hosts can still use existing calls with `fast` omitted.
No additional setting or top-level `fast` argument is introduced.

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
   `tasks[].reasoning_effort` / optional boolean `tasks[].fast` to the model
   (via the registered `ToolEntry`);
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
