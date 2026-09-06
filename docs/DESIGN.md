# Design — `hermes-delegate-routing`

A fork-free Hermes Agent plugin that adds explicit per-task `model` / `provider` /
`reasoning_effort` routing to `delegate_task`. This document explains what it does, why it's built as
a load-time monkeypatch, and where it couples to host internals.

## 1. Summary

`delegate_task` normally runs every subagent in a batch on shared delegation
routing. This plugin lets each task pick its own model/provider/reasoning effort:

```jsonc
delegate_task(tasks=[
  {"goal": "Summarize these logs",      "model": "gemini-flash-2.0", "provider": "openrouter", "reasoning_effort": "low"},
  {"goal": "Review this diff for bugs", "model": "sonnet",           "provider": "anthropic",  "reasoning_effort": "high"},
  {"goal": "Research the CVE",          "model": "deepseek-pro",     "provider": "deepseek",   "reasoning_effort": "medium"}
])
```

It installs against **unmodified upstream hermes-agent** — no fork, no source
patch, no rebase burden.

## 2. Background

Per-task delegate routing was proposed upstream several times
([#36790](https://github.com/NousResearch/hermes-agent/pull/36790),
[#12794](https://github.com/NousResearch/hermes-agent/pull/12794),
[#3172](https://github.com/NousResearch/hermes-agent/pull/3172)) and not accepted,
despite recurring user requests
([#34764](https://github.com/NousResearch/hermes-agent/issues/34764),
[#15789](https://github.com/NousResearch/hermes-agent/issues/15789),
[#3719](https://github.com/NousResearch/hermes-agent/issues/3719),
[#18591](https://github.com/NousResearch/hermes-agent/issues/18591)). A plugin
delivers the capability without maintaining a fork of a fast-moving codebase.

### Non-goals

- **Top-level `delegate_task(model=…, provider=…, reasoning_effort=…)`** (outside `tasks[]`). The host
  drops top-level args before the tool runs (§5), and the `tasks=[{…}]` shape is
  the recommended one anyway. Not designed out — just not wired.
- **Heuristic/auto routing** (classify a task → pick a model). Different products
  exist for that (`hermes-model-router`, `cobalt-agent`). This plugin does
  **explicit** routing only — the caller names the model/provider/reasoning effort.

## 3. Prior art

| Project | Mechanism | Scope | Fork-free? |
|---|---|---|---|
| upstream PRs #36790 / #12794 / #3172 | core patch | delegate per-task | ❌ (a fork) |
| `cobalt-agent` | `pre_tool_call` hook **+ patches `delegate_tool.py`** | auto-routing by `task_type` | ❌ patches source |
| `hermes-model-router` | skill + config classifier | task-type → model | ✅ (heuristic, not explicit) |
| `hermes-arc` | `pre_llm_call` + `patch_run_agent.py` | turn-level | ⚠️ compat patch |
| `hermes-agent-kit` | gateway hooks | per-topic (Telegram) | ✅ (different scope) |
| `agentmint-hermes-runner` | monkeypatch of `delegate_task` | route to named subagents | ✅ no source patch |

No existing project ships a clean, fork-free plugin for *explicit per-task
`model`/`provider`* override. `agentmint-hermes-runner` demonstrates that
monkeypatching `delegate_task` from a pip plugin works in production; this plugin
applies the same technique to a different capability.

Related: open PR
[#23898](https://github.com/NousResearch/hermes-agent/pull/23898) proposes a native
`runtime_override` primitive via the `pre_llm_call` hook. If it lands it could let
the apply seam (§6) drop its monkeypatch; it does not remove the need for the
capture seam.

## 4. Plugin-system fit

- **Discovery:** pip entry point group `hermes_agent.plugins`
  (`hermes_cli/plugins.py`); no `plugin.yaml` needed for entry-point plugins.
  Enable via `plugins.enabled` in `config.yaml`; `register(ctx)` runs at load.
- **Registry override API** (`register(..., override=True)`) can replace a built-in
  tool — but it does **not** intercept `delegate_task` at runtime (§6.1).
- **Lifecycle hooks** (`pre_tool_call` is veto-only, `transform_tool_result`,
  `subagent_start/stop`, middleware) cannot cleanly inject per-task creds into
  `delegate_task` (§7).
- **Load-time monkeypatch** is an established pattern in the ecosystem
  (`agentmint-hermes-runner`, `hermes-arc`) and is the mechanism used here.

## 5. Host runtime constraints (why the design is shaped this way)

Facts about the host that dictate the approach (originally verified against
0.18.0; re-verified against the installed 0.21.0 checkout):

1. **`delegate_task` has no `**kwargs`** (`tools/delegate_tool.py`: `goal, context,
   tasks, max_iterations, role, background, parent_agent`). An unknown kwarg raises.
2. **Top-level args are whitelisted out.** Both call sites — the registry handler
   lambda and `run_agent._dispatch_delegate_task` (used from
   `agent/agent_runtime_helpers.py` and `agent/tool_executor.py`) — forward only
   only its supported control/spawn arguments. So top-level routing fields never
   reach the tool.
3. **Per-task fields survive.** `_strip_model_hidden_task_fields` strips only
   `{"acp_command","acp_args"}`, so `model`/`provider`/`reasoning_effort` inside
   `tasks[i]` reach the build loop — which currently ignores these per-task routing
   fields and uses the batch/config routing for construction.
4. **`_build_child_agent`** is a module-level function called with `task_index=i`
   and `override_provider/base_url/api_key/api_mode/...`. It receives only
   `task_index`, not the task dict — so per-task creds must be correlated by index.
5. **The resolver reuses public host functions** — `hermes_cli.model_switch`
   (`parse_model_flags`, `switch_model`), `hermes_cli.config.load_config`,
   `hermes_cli.runtime_provider.resolve_runtime_provider` — no new primitives.

**Implication:** the only routing channel that reaches the tool is `tasks[i]`; the
only place to apply model/provider routing without reimplementing the build loop is
`_build_child_agent`, keyed by `task_index`. Explicit reasoning can use the same
index correlation and then replace `child.reasoning_config` after native child
construction.

## 6. Design — four seams

The plugin installs four narrow runtime patches once, idempotently, in
`register(ctx)`. Seams A–C handle routing; seam D is display-only. Because the host
resolves `delegate_task`, `_build_child_agent`, and the async formatter as module
globals at call time, rebinding those attributes reaches the active call paths.

- **A — schema.** Wrap the tool's `dynamic_schema_overrides` builder to advertise
  `tasks[].model`, `tasks[].provider`, and `tasks[].reasoning_effort` to the model.
  (Updates the registered `ToolEntry`, since the registry holds a direct reference
  to the builder.)
- **B — capture.** Wrap `delegate_task`: for each task with a `model`/`provider`,
  resolve a full route bundle via the host `/model` switch pipeline; for an explicit
  `reasoning_effort`, call Hermes' own `parse_reasoning_effort()`. Stash the combined
  routing state in a `ContextVar` keyed by task index; then call the original. The
  build loop runs synchronously within this call, so the `ContextVar` is in scope
  when seam C fires
  — including for `background=True` delegations (only child *execution* is
  deferred, not construction).
- **C — apply.** Wrap `_build_child_agent`: look up the stashed route by
  `task_index` and override `model`/`override_*` before calling the original. After
  Hermes constructs the child normally, apply an explicit task `reasoning_effort`
  to `child.reasoning_config`. Tasks with no override pass through unchanged. This
  keeps provider-specific reasoning translation in Hermes' normal transports.
- **D — async display.** Wrap `tools.process_registry._format_async_delegation`.
  Hermes' async batch event keeps the batch/default model captured before seam C,
  but each completed result contains the actual child `model`. The wrapper shallow-
  copies only the display event, replaces the header model from `results[].model`,
  and delegates formatting back to Hermes. Mixed-model fan-out uses
  `Model: per-task` plus a compact task-to-model mapping. If this formatter is not
  present on a host version, routing still activates and only the display fix is
  skipped with a warning.

**Precedence (per field):** `tasks[i].model/provider/reasoning_effort` → matching
`delegation.*` config → parent agent. A model-only task pins the inherited
provider, and a provider-only task reuses the inherited model; omitted fields are
not handed back to `/model` auto-detection. In particular, omitting task
`reasoning_effort` leaves Hermes' native `delegation.reasoning_effort > parent`
resolution untouched.

### 6.1 Why not the registry override API

The sanctioned way to replace a built-in tool is `register(..., override=True)`,
which swaps the `ToolEntry.handler`. But `delegate_task` is **special-cased in the
agent runtime** (`agent/agent_runtime_helpers.py`, `agent/tool_executor.py`) to run
via `run_agent._dispatch_delegate_task`, which imports and calls
`tools.delegate_tool.delegate_task` **directly** — bypassing the registry. So an
`override=True` handler would never run for this tool. The routing runtime seams
(B/C) can therefore only be installed by module-attribute monkeypatch (which the
direct import picks up) — the schema seam (A) still flows through the registry.
Seam D is similarly a module-attribute wrapper on the async formatter. None of
these require an `allow_tool_override` grant.

A future upstream change that routed `delegate_task` through the registry, or that
landed the `runtime_override` primitive (PR #23898), would let this plugin drop the
monkeypatch.

## 7. Alternatives considered

| Approach | Why not |
|---|---|
| Registry `override=True` only | Misses `_dispatch_delegate_task` (direct import); still couldn't vary per child without patching `_build_child_agent`. |
| `pre_tool_call` hook mutates args | Veto-only by contract; and top-level fields are whitelisted out downstream. |
| Middleware rewrite into `tasks[i]` | Fields survive but the loop ignores them — still needs seam C. |
| Reimplement `delegate_task` (vendor the loop) | ~300 lines tracking many internals; higher churn than four narrow seams. |
| Fork + core patch | The thing we're avoiding. |

## 8. Packaging

- Standalone pip package; module `hermes_delegate_routing`, entry point
  `hermes_agent.plugins → delegate_routing`.
- Enable with `plugins.enabled: [delegate_routing]`. No `allow_tool_override`
  needed (this plugin does not use the registry override API).
- Install into the same environment as hermes-agent (`pip install …`; `pip install
  -e .` for development).

## 9. Failure modes

- **Unresolvable model/provider/reasoning effort:** default **fail-hard** — the `delegate_task` call
  returns a tool error naming the bad value. Config toggle
  `delegate_routing.on_error: fail | fallback`; `fallback` skips the override (the
  task uses normal batch/config routing) and logs a warning.
- **Host missing / signature mismatch:** `apply_patches()` validates the target
  signatures and **refuses to patch** on any mismatch, logging a loud warning. The
  plugin degrades to a no-op; core behavior is untouched (never half-patched).
- **Child reasoning attribute drift:** if an explicit task reasoning override was
  resolved but the constructed child no longer exposes `reasoning_config`, seam C
  logs a warning and raises `ValueError`. This fails closed instead of silently
  creating an unused ad-hoc attribute and pretending the override succeeded.
- **Double load:** idempotent via a module sentinel guarded by a lock.
- **Concurrency:** the `ContextVar` isolates overlapping `delegate_task` calls; the
  synchronous build loop keeps index→creds stable within a call.

## 10. Coupling & risks

The core cost of this approach is dependence on host internals
(`delegate_task`, `_build_child_agent`, `_build_dynamic_schema_overrides`,
`tools.process_registry._format_async_delegation`, `parse_model_flags`,
`parse_reasoning_effort`, `_strip_model_hidden_task_fields`). Mitigations:

- **Signature guard** at patch time turns host drift into a safe no-op with a loud
  log, not a crash.
- **Arity-transparent wrappers** (`**kwargs`, keyword forwarding) tolerate the host
  *adding* a parameter; the resolver reads `parse_model_flags` positionally to
  tolerate its return tuple growing.
- **Pinned, tested host versions** (see `CHANGELOG.md` / README support table).
- **Future work:** if the host ships native per-task routing, add an explicit
  capability check and no-op rather than layering these routing seams on top.

### Evidence index (hermes-agent 0.21.0 checkout `63279301`; original seams also verified on 0.18/0.19)

- `tools/delegate_tool.py` — `delegate_task` signature (no `**kwargs`); child build
  loop (batch-wide creds); `_build_child_agent`; `_strip_model_hidden_task_fields`
  = `{"acp_command","acp_args"}`; native `delegation.reasoning_effort > parent`
  resolution; registry registration; per-subagent result `model`.
- `run_agent.py` — `_dispatch_delegate_task` (whitelists args; direct import,
  bypasses registry).
- `tools/process_registry.py` — `_format_async_delegation` renders the async
  completion header from batch-level `evt.model`, while each batch result carries
  the actual child `model` used after per-task routing.
- `agent/agent_runtime_helpers.py`, `agent/tool_executor.py` — `delegate_task`
  special-case dispatch.
- `tools/registry.py` — `register(override=True)` + plugin override policy;
  `dynamic_schema_overrides` consumed in `get_definitions`.
- `hermes_cli/plugins.py` — entry-point discovery (`hermes_agent.plugins`).
- `hermes_cli/model_switch.py` — `switch_model` (pure resolver; `is_global` gates
  persistence, which this plugin never requests), `parse_model_flags`.
