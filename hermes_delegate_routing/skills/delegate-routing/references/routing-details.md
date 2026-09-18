# Delegate-routing reference

## Field inheritance

Each field resolves independently: task value → matching `delegation.*`
setting → parent inheritance. For example, with this hypothetical baseline
(placeholders, not current profile values):

```yaml
delegation:
  model: "<baseline model id>"
  provider: "<baseline provider id>"
```

| Task fields | Effective behavior |
|---|---|
| neither model nor provider | normal delegation baseline / parent inheritance |
| model only | requested model on the inherited provider (no silent hop) |
| provider only | inherited model on the requested provider, if that route resolves |
| both | exact requested model/provider pair |
| reasoning only | normal model/provider, task-local reasoning override |

The resolver also accepts inline `--provider <id>` in the model string; this
counts as an explicit provider. Prefer the structured field for JSON calls.

## Reasoning levels

`reasoning_effort` uses the host's supported vocabulary: `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`, or `ultra`, depending on host version.

## Error policy

`delegate_routing.on_error` defaults to `fail`. A bad literal, unavailable
provider, conflicting inline provider, or unsupported reasoning value fails
the whole delegation call before children start. Under `fallback`, a failed
task override is skipped and normal delegation may run on a different route.
Do not switch to `fallback` merely to make an error disappear; use it only
when the user explicitly accepts best-effort routing.

## Failure diagnostics

Only investigate configuration after a routed call fails or the runtime
reports a stale plugin:

```text
terminal(command="hermes config get plugins && hermes config get delegation")
```

If package/process version skew is suspected, use the relevant Hermes
interpreter, outside a source checkout:

```text
terminal(command="python -c \"import hermes_delegate_routing; print(hermes_delegate_routing.__version__)\"")
```

That fresh interpreter does not prove which version a resident gateway loaded.
If logs report `INACTIVE`, inspect the host/signature warning. After a host or
plugin update, a stale gateway may need a restart before retrying; a restart
alone cannot fix an unsupported host signature.

The plugin corrects async completion display when the host exposes the
supported formatter. A warning about that private formatter does not by
itself prove a routing failure.
