---
name: delegate-routing
description: "Use when routing subagents or setting per-task Fast."
version: 0.3.2
author: teich0psia, Hermes Agent
license: MIT
platforms: [windows, macos, linux]
metadata:
  hermes:
    tags: [hermes, delegate_task, delegate-routing, model-routing, provider-routing]
    related_skills: [hermes-agent, model-usage-and-routing, opencode]
---

# Hermes per-task delegate routing

## When to use

Use for an explicit model/provider, reasoning level, child-only Fast override,
or a routing failure in Hermes `delegate_task`. Do not use to launch an external
agent CLI or change the parent chat's settings. Requires `hermes-delegate-routing`;
loading this Skill proves registration, not that every runtime patch is active.

## Procedure

### 1. Resolve the requested route

- **For "same route as this chat", read your own runtime metadata first.**
  Copy the values after `Model:` and `Provider:` verbatim into `tasks[i].model`
  and `tasks[i].provider`. Use both values, without their labels. Do not use
  profile defaults, quoted examples, or another agent's metadata. No lookup is
  needed when both values are present; never infer a missing value.
- For another route or missing metadata, use an already-known exact pair;
  otherwise inspect candidates with
  `terminal(command="hermes config get model && hermes config get delegation")`.
  Configuration does not prove the active chat's route. If unresolved, make one
  lookup in the requested provider's documented model list or Hermes picker.
  Do not change the active model to inspect it or search auth/provider internals.
  On a miss, ask; offer at most two candidates actually returned by the lookup.
- For Fast-only or reasoning-only requests, retain normal route inheritance
  unless the task's instructions also require an explicit model/provider.

Proceed only when the requested route is resolved or inheritance is intentional.

### 2. Select optional controls

Omit `reasoning_effort` unless requested by the user or task instructions.
Fast is independent of reasoning effort and model choice:

| Requested behavior | Field inside the affected `tasks[i]` |
|---|---|
| Keep existing behavior; no Fast override requested | omit `fast` |
| Turn Fast ON for this child | `"fast": true` |
| Turn Fast OFF for this child, including inherited Fast | `"fast": false` |

Preserve existing behavior when unspecified: **omission is not OFF**.
Use actual booleans, not strings, numbers, or `null`.

Before either explicit Fast value, check the currently offered `delegate_task`
schema for `tasks[i].fast`. If absent, stop the Fast request: do not send an
unadvertised field or silently omit it. Updated Skill text and a fresh
interpreter's schema do not prove the resident gateway is updated. Request
approval for any required restart. An advertised field still does not prove the
resolved model/provider/endpoint supports Fast; unsupported ON fails by default.

### 3. Build and submit the existing call shape

Use `tasks=[...]` even for one child. Put all routing/control fields inside each
task, never at the top level. Replace these placeholders with resolved values;
supply a self-contained goal, relevant evidence/paths, constraints, and acceptance
checks in `context` because the child does not receive the parent conversation.

```json
{
  "tasks": [
    {
      "goal": "A specific, self-contained task for the child",
      "context": "Relevant background, paths, constraints, and acceptance checks",
      "model": "<exact model id>",
      "provider": "<exact provider id>"
    }
  ]
}
```

Add optional controls only to the affected task; leave other children unchanged.

## Pitfalls

- Set both model and provider for an exact route; a partial pair can inherit an
  unintended default. Preserve IDs literally; do not invent `provider:model`.
- Do not replace Fast with a cheaper model or lower reasoning effort.
- Do not change global `/fast`, `delegation.*`, or error policy for a one-off child.
- Do not retry unchanged failures or silently choose a different route. Preserve
  the attempted literals, inspect the error, and resolve its cause first.

## Verification

Report each child's outcome and the evidence for its effective route. Use its
structured `model`/`provider` fields or session/turn records, not a batch header.
For explicit Fast, verify its application log or child request settings; an input
boolean or completed task is not proof. If unavailable, report Fast as unverified.
`on_error: fallback` can leave Fast unapplied; sending Fast does not prove a speedup.

For inheritance, supported reasoning levels, Fast evidence, or error recovery,
load [routing details](references/routing-details.md) with
`skill_view(name="delegate_routing:delegate-routing", file_path="references/routing-details.md")`.
