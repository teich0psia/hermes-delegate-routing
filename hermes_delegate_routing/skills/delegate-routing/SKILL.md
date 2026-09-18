---
name: delegate-routing
description: "Use when routing a Hermes subagent by model/provider."
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

Use for an explicit model, provider, or reasoning level on a Hermes
`delegate_task` child, not for an external agent CLI. Requires the
`hermes-delegate-routing` plugin; loading this skill confirms skill
registration, not that every routing patch is active.

## Fast path

1. **For "same route as this chat", read your own runtime metadata first.**
   The `Model: <id>` and `Provider: <id>` lines appended to your system prompt
   describe **your** active model and provider. Copy the values after the
   labels verbatim into `tasks[i].model` and `tasks[i].provider` — both values,
   not the labels. No tool call or config lookup is needed. Do not substitute
   profile defaults, a quoted example, or another agent's metadata. If either
   line is missing, do not infer it from the other.
2. For a different route or missing metadata, use an already-known exact pair;
   otherwise inspect configured IDs with
   `terminal(command="hermes config get model && hermes config get delegation")`.
   Configuration is a candidate route, not proof of the active chat's route.
   If still unresolved, make one lookup in the requested provider's model list
   (its documented models endpoint or Hermes model picker). Do not change the
   active model just to inspect the picker, or dig through provider source,
   auth dumps, and catalog internals. On a lookup miss, ask for clarification;
   offer at most two candidates only if the lookup actually returned them.
3. Call `delegate_task` with a `tasks` array, even for one child. Replace the
   placeholders below with the resolved pair and give the child self-contained
   background; it does not receive the parent conversation history.

```json
{
  "tasks": [
    {
      "goal": "A specific, self-contained task for the child",
      "context": "Only the background, paths, constraints, and evidence this child needs",
      "model": "<exact model id>",
      "provider": "<exact provider id>"
    }
  ]
}
```

## Pitfalls

- Put routing fields in `tasks[i]` only. The host drops top-level `model`,
  `provider`, and `reasoning_effort` before this plugin runs.
- Set **both** model and provider for an exact route; leaving one out can
  combine the requested value with an unintended default.
- Preserve identifiers literally. Never guess aliases, change case, or invent
  a provider prefix; use the separate `provider` field.
- Omit `reasoning_effort` unless a level was requested. Do not change
  `delegation.*` for a one-off child.

## Verification

Inspect each child's actual `model` and `provider` in structured results when
present; otherwise inspect its session/turn records. A batch-level completion
header alone is not route evidence. If a call fails, preserve the attempted
literals and inspect the error before retrying.

For inheritance, reasoning levels, error policy, and failure diagnostics, load
`skill_view(name="delegate_routing:delegate-routing", file_path="references/routing-details.md")`.
