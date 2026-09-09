---
name: delegate-routing
description: "Use when routing a Hermes subagent by model/provider."
version: 0.3.1
author: Hermes Agent
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [hermes, delegate_task, delegate-routing, model-routing, provider-routing]
    related_skills: [hermes-agent, model-usage-and-routing, opencode]
---

# Hermes per-task delegate routing

Use this skill whenever a delegated child must run on an explicit model,
provider, or reasoning level. The user-facing surface is Hermes' internal
`delegate_task`. Provided by the `hermes-delegate-routing` plugin — if this
skill loads, the plugin is installed and active in this session.

## Fast path

For one child, still use a one-entry `tasks` array and put the routing fields
inside that task:

```json
{
  "tasks": [
    {
      "goal": "A specific, self-contained task for the child",
      "context": "Only the background, paths, constraints, and evidence this child needs",
      "model": "<literal model id or configured alias>",
      "provider": "<literal configured provider id>",
      "reasoning_effort": "<supported level>"
    }
  ]
}
```

Do **not** use top-level `model`, `provider`, or `reasoning_effort`; the host
drops top-level routing arguments before the tool runs, while this plugin
reads routing fields from `tasks[i]` only.

## Rules that prevent the recurring mistakes

- Specify **both** `model` and `provider` when crossing away from the current
  delegation route. A model-only task stays on the inherited
  `delegation.provider`; a provider-only task reuses the inherited
  `delegation.model`. Specifying only one field can route to an unintended pair.
- Preserve model identifiers literally. Do not turn a display name into
  `provider/model` syntax or another guessed alias; use the separate
  `provider` field instead. Unresolvable values fail the whole call
  (fail-closed) — they never silently fall back to another model.

## Resolving an ambiguous model name

A user-given name ("Deepseek v4 flash") is not yet a literal. Resolve it in
this order — stop at the first hit, never read provider internals:

1. Current session runtime metadata (the active model/provider pair) —
   zero extra calls when the user means "that same route".
2. `hermes config get model` / `hermes config get delegation` — one call,
   exact configured IDs.
3. The model picker (`hermes model`) or provider's live `/v1/models` list —
   one lookup, exact ID match only.

Do not read provider plugin sources, `auth` dumps, or catalog JSON beyond
step 3. One lookup miss means the name is unknown: offer at most two
candidate literals as a two-choice question instead of demanding a raw
literal, and never fire `delegate_task` on a guess. `reasoning_effort` needs
no resolution — omission inherits, so leave it out unless the user asked for
a level.
- `reasoning_effort` is optional and uses the Hermes-supported level
  vocabulary: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or
  `ultra` (depending on the host version).
- The field precedence is task value → matching `delegation.*` setting →
  parent inheritance. Omitted fields retain normal Hermes behavior.
- Give every child a self-contained `goal` and `context`; it does not receive
  the parent conversation history.

## Verification and recovery

1. Prefer the direct routed `delegate_task` call. Do not run a configuration
   investigation on every delegation when the plugin is already known active.
2. If the call fails, preserve the literal values and inspect the exact error.
   Then check `hermes config get plugins` and `hermes config get delegation`.
3. If logs say the plugin is `INACTIVE`, or the host was updated without a
   gateway restart, restart the relevant Hermes gateway before retrying. A
   stale gateway can run an older plugin even when the installed package is
   newer.
4. To verify the route, prefer the child result's actual model/provider fields
   or the child turn record carrying `platform=subagent` and `model=<id>`.
   Never infer the model from a stale batch-level completion header alone.
5. Do not mutate `delegation.*` for a one-off task. If a persistent default is
   genuinely wanted, use `hermes config set` and verify with
   `hermes config get`; never hand-edit YAML.

For the precedence table, error policy, and diagnostics, load
`references/routing-details.md`.
