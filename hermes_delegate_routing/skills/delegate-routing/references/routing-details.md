# Delegate-routing reference

Read only the section needed for the current request or failure.

- [Field inheritance and reasoning](#field-inheritance-and-reasoning)
- [Fast application and evidence](#fast-application-and-evidence)
- [Error policy and recovery](#error-policy-and-recovery)
- [Skill source and runtime readiness](#skill-source-and-runtime-readiness)

## Field inheritance and reasoning

Model/provider/reasoning fields resolve independently: task value → matching
`delegation.*` setting → parent inheritance. Omission is not a request to copy
this chat's exact route; configured delegation defaults may differ.

| Task fields | Effective behavior |
|---|---|
| neither model nor provider | normal delegation baseline / parent inheritance |
| model only | requested model on the inherited provider (no silent hop) |
| provider only | inherited model on the requested provider, if that route resolves |
| both | exact requested model/provider pair |
| reasoning only | normal model/provider, task-local reasoning override |
| Fast only | normal model/provider/reasoning, task-local Fast override |

The resolver also accepts inline `--provider <id>` in the model string; this
counts as an explicit provider. Prefer the structured field for JSON calls.

Use `reasoning_effort` values supported by the installed host: `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`, or `ultra`, depending on host version.

## Fast application and evidence

For a Fast-only request with intentional route inheritance, keep the usual call
shape. This example shows independent controls, not a live task to execute:

```json
{
  "tasks": [
    {"goal": "Task A: use Fast", "fast": true},
    {"goal": "Task B: disable Fast", "fast": false},
    {"goal": "Task C: preserve existing behavior"}
  ]
}
```

Supply real goals/context before use. Add a resolved model/provider pair to each
task only when an exact route is also required by the request or task instructions.

ON uses Hermes' `resolve_fast_mode_overrides` on the child's final route. OFF
removes recognized inherited Fast flags and clears the child's bounded
`auto`/`cold` Fast mode (defensive — current hosts do not hand children one);
it preserves non-Fast settings. Both explicit values require the child's
request/Fast attributes. Omission requires none of these capabilities. A
rejected batch (default `on_error: fail`) closes and detaches every child built
so far, so the parent keeps no closed child.

Match application evidence to the affected child/task index. The plugin logs
`delegate-routing: task <index> fast=True applied` (or `fast=False`); otherwise
inspect that child's request settings without exposing credentials. This proves
local application, not provider-side priority or measured latency. If neither
is available, distinguish requested Fast from verified Fast in the result.

## Error policy and recovery

`delegate_routing.on_error` defaults to `fail`. Distinguish two failure stages:

| Failure stage | Default `fail` | Explicit `fallback` |
|---|---|---|
| Capture: bad route literal, unavailable provider, conflicting inline provider, unsupported reasoning, invalid Fast type | Fail the call before children start | Skip the failed task override; normal delegation may use a different route |
| Apply: Fast capability unavailable on the constructed child's route/host | Reject the batch before execution; close and detach the constructed children | Skip only Fast; keep resolved route, reasoning, and pre-existing Fast settings |

Do not enable fallback to conceal an error; use it only when the user explicitly
accepts best-effort behavior. A fallback warning is not proof that Fast is OFF.

1. Preserve the attempted IDs/options and the returned error; check the reported
   stage before retrying. Correct only a cause supported by evidence.
2. For a routed-call failure or stale-plugin warning, inspect configuration with
   `terminal(command="hermes config get plugins && hermes config get delegation")`.
3. For `INACTIVE`, inspect the host/signature warning. A restart alone cannot
   fix an unsupported signature. Request approval before any restart.
4. If unresolved, report the blocker and ask rather than looping through aliases,
   providers, global settings, or identical calls.

A warning about the optional async completion formatter alone does not prove
routing failed. Verify each child's actual route rather than the batch header.

## Skill source and runtime readiness

Use `skill_view(name="delegate_routing:delegate-routing")` for the canonical
packaged Skill; do not create a second copy under the user skills directory.
An editable installation reads the repository's bundled files directly; a copy
installation requires updating the installed package.

Keep these checks separate: Skill content, the tool schema offered to the current
agent, and actual child request settings. Readable new text proves neither a new
runtime schema nor successful Fast application. A fresh interpreter does not
prove which code a resident gateway loaded.

When version skew is suspected, inspect provenance with the target Hermes
interpreter **outside the source checkout**: local egg-info can shadow installed
metadata. Use the module's `__file__` and distribution `direct_url.json` together.
For a package-version check, select that interpreter in this command:

```text
terminal(command="python -c \"import hermes_delegate_routing; print(hermes_delegate_routing.__version__)\"")
```
