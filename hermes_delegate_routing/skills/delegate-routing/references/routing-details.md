# Delegate-routing reference

## Canonical one-child call

```text
delegate_task(tasks=[{
  "goal": "...",
  "context": "...",
  "model": "<exact /model name>",
  "provider": "<exact configured provider id>",
  "reasoning_effort": "minimal"
}])
```

Use the literal model/provider values from the current session's runtime
metadata — never guess, normalize case, or rewrite a display name into
`provider/model` syntax.

## Why `tasks[]` is mandatory

The plugin wraps four Hermes seams:

1. the tool schema advertises `tasks[].model`, `tasks[].provider`, and
   `tasks[].reasoning_effort`;
2. the delegate call resolves those fields through Hermes' model-switch and
   reasoning parsers;
3. the child builder applies the resolved route by `task_index`;
4. async completion display reports the actual child model when the host exposes
   the formatter.

The host special-cases `delegate_task` and drops unsupported top-level routing
arguments before execution. Therefore this is wrong even for one task:

```text
delegate_task(
  goal="...",
  model="...",
  provider="..."
)
```

This is the correct form (see the canonical call above).

## Precedence examples

Assume the current profile baseline is:

```yaml
delegation:
  model: gpt-5.6-luna
  provider: openai-codex
```

| Task fields | Effective behavior |
|---|---|
| neither model nor provider | normal delegation baseline / parent inheritance |
| model only | requested model on the inherited provider (no silent hop) |
| provider only | inherited model on the requested provider, if that route resolves |
| both | exact requested model/provider pair |
| reasoning only | normal model/provider, task-local reasoning override |

Thus a cross-provider request must normally set both fields. Do not assume a
model string alone selects the provider currently used by the parent chat.

## Error policy

`delegate_routing.on_error` defaults to `fail`. A bad literal, unavailable
provider, conflicting inline provider, or unsupported reasoning value fails the
whole delegation call before children start. This is preferable to silently
running the wrong model. If the user explicitly requests best-effort behavior,
set the profile option to `fallback` only after explaining that the requested
route may be skipped.

## Actual-model verification

For synchronous results, inspect each structured result's `model` and
`provider` fields when present. For background results, inspect the completion
payload's per-result data or the child session/turn record. A batch header may
represent the profile-level model captured before per-task routing. The plugin
patches the supported async formatter, but the host can move that private
formatter; a display warning is not by itself a routing failure.

## Minimal diagnostics

Use these only after an actual routed call fails or the runtime reports a stale
plugin:

```bash
hermes config get plugins
hermes config get delegation
python -c "import hermes_delegate_routing; print(hermes_delegate_routing.__version__)"
```

If the log warns that a previous Hermes update was not followed by a gateway
restart, restart before comparing installed-package and live-process behavior.
