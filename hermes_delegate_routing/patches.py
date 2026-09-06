"""The monkeypatch seams.

Four narrow wrappers over Hermes runtime seams (see docs/DESIGN.md §6):

  A. schema  — advertise per-task model/provider/reasoning fields to the LLM
  B. capture — resolve per-task routing state and stash it by task_index
  C. apply   — inject stashed routing into each ``_build_child_agent`` call
  D. display — make async completion metadata report the actual child model(s)

This module defines the wrapper *factories*; the orchestration that imports the
host, validates signatures, and installs them lives in ``apply_patches()``.
Only per-task ``tasks[]`` fields are supported — top-level routing fields never
reach the tool (DESIGN §6.1).
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import threading

from ._state import ROUTING, get_creds
from .resolver import resolve_model_provider_override

logger = logging.getLogger(__name__)

# Serializes apply_patches() so a concurrent second caller can't pass the
# _HDR_PATCHED check before the first sets it (which would double-wrap).
# A re-entrant lock (not a plain Lock): importing a host module below can
# re-enter the plugin loader on some hosts (model_tools discovers plugins at
# import), which calls register() → apply_patches() on this same thread. A
# plain Lock would deadlock there; with an RLock the nested pass proceeds and
# the sentinel re-check below keeps patching single (see _preimport_host()).
_PATCH_LOCK = threading.RLock()

# Host function parameters we depend on. If a future hermes-agent drops/renames
# any of these, apply_patches() refuses to patch rather than half-wrap.
_EXPECTED_DELEGATE_PARAMS = {
    "goal", "context", "tasks", "max_iterations", "role", "background", "parent_agent",
}
_EXPECTED_BUILD_CHILD_PARAMS = {
    "task_index", "goal", "context", "toolsets", "model", "max_iterations",
    "task_count", "parent_agent", "override_provider", "override_base_url",
    "override_api_key", "override_api_mode", "override_acp_command",
    "override_acp_args", "role",
}


def _tool_error(msg: str, tool_error=None) -> str:
    """Return a host-shaped tool error string.

    Uses the host's ``tool_error`` when provided; otherwise a compatible
    ``{"error": ...}`` JSON string (the host's own error shape).
    """
    if tool_error is not None:
        try:
            return tool_error(msg)
        except Exception:  # pragma: no cover - defensive
            pass
    return json.dumps({"error": msg})

# --- Seam A: schema ---------------------------------------------------------

_TASK_MODEL_DESC = (
    "Per-task model override for this subagent. Accepts a model name or alias as "
    "used by /model (e.g. 'sonnet', 'gemini-flash-2.0'), optionally with an "
    "inline '--provider <id>'. Do NOT use provider:model syntax; set the separate "
    "'provider' field instead. When omitted, the child inherits the batch/config "
    "model."
)
_TASK_PROVIDER_DESC = (
    "Per-task provider override. When set, this subagent connects to the specified "
    "provider instead of inheriting from delegation.provider or the parent. The "
    "provider must be configured in Hermes. Prefer this structured field over "
    "embedding '--provider' in 'model' for JSON tool calls."
)
_TASK_REASONING_DESC = (
    "Per-task reasoning effort override. Uses the same values as Hermes "
    "reasoning_effort (for example 'none', 'minimal', 'low', 'medium', 'high', "
    "'xhigh', 'max', or 'ultra', depending on the host version). When omitted, "
    "normal delegation.reasoning_effort > parent-agent inheritance is preserved."
)


def make_schema_override(orig_builder):
    """Wrap the host schema builder to advertise per-task routing fields.

    Deep-copies the original output before injecting because the host builder only
    shallow-copies each property, leaving ``tasks.items`` aliased to the static
    schema — mutating it in place would corrupt the host's schema (DESIGN §6).
    """

    def _wrapped(*args, **kwargs):
        base = orig_builder(*args, **kwargs)
        if not isinstance(base, dict):
            return base
        result = copy.deepcopy(base)
        try:
            props = result["parameters"]["properties"]["tasks"]["items"]["properties"]
        except (KeyError, TypeError):
            logger.warning(
                "delegate-routing: unexpected delegate schema shape; "
                "not advertising per-task routing fields"
            )
            return result
        props.setdefault("model", {"type": "string", "description": _TASK_MODEL_DESC})
        props.setdefault("provider", {"type": "string", "description": _TASK_PROVIDER_DESC})
        props.setdefault(
            "reasoning_effort",
            {"type": "string", "description": _TASK_REASONING_DESC},
        )
        return result

    return _wrapped


# --- Seam B: capture --------------------------------------------------------

def _resolve_reasoning_override(value):
    """Parse an explicit task reasoning effort through the host chokepoint."""
    try:
        from hermes_constants import parse_reasoning_effort
    except Exception as exc:  # pragma: no cover - host import guard
        raise ValueError(f"Cannot import Hermes reasoning parser: {exc}") from exc

    parsed = parse_reasoning_effort(value)
    if parsed is None:
        raise ValueError(f"Unsupported reasoning_effort {value!r}")
    return dict(parsed)


def make_delegate_task_wrapper(orig_delegate_task, resolver, on_error="fail", tool_error=None):
    """Wrap ``delegate_task`` to resolve per-task routing into ROUTING by index.

    ``model`` / ``provider`` are resolved through the host ``/model`` pipeline.
    Explicit ``reasoning_effort`` is parsed through the host reasoning chokepoint.
    The resulting state is keyed by task index for the apply seam to consume.
    The original is then called unchanged; ROUTING is always reset afterwards.

    ``on_error``: 'fail' (default) → return a tool error and do not delegate;
    'fallback' → skip the failed override (child uses batch/config routing) and log.
    """

    def _wrapped(goal=None, context=None, tasks=None, max_iterations=None,
                 role=None, background=None, parent_agent=None, **extra):
        routing = {}
        if isinstance(tasks, list):
            for i, t in enumerate(tasks):
                if not isinstance(t, dict):
                    continue
                model = t.get("model")
                provider = t.get("provider")
                has_model_provider = bool(model or provider)
                reasoning_effort = t.get("reasoning_effort")
                has_reasoning = "reasoning_effort" in t and reasoning_effort is not None
                if not has_model_provider and not has_reasoning:
                    continue
                try:
                    route = {}
                    if has_model_provider:
                        route.update(resolver(
                            model_input=model,
                            provider_input=provider,
                            parent_agent=parent_agent,
                        ))
                    if has_reasoning:
                        route["reasoning_config"] = _resolve_reasoning_override(
                            reasoning_effort
                        )
                    routing[i] = route
                except Exception as exc:
                    if on_error == "fallback":
                        logger.warning(
                            "delegate-routing: task %d override "
                            "(model=%r provider=%r reasoning_effort=%r) failed to resolve; "
                            "using batch/config routing: %s",
                            i, model, provider, reasoning_effort, exc,
                        )
                        continue
                    return _tool_error(
                        "delegate_task routing: could not resolve task override "
                        f"for task {i} (model={model!r}, provider={provider!r}, "
                        f"reasoning_effort={reasoning_effort!r}): {exc}",
                        tool_error,
                    )
        token = ROUTING.set(routing)
        try:
            return orig_delegate_task(
                goal=goal, context=context, tasks=tasks,
                max_iterations=max_iterations, role=role,
                background=background, parent_agent=parent_agent, **extra,
            )
        finally:
            ROUTING.reset(token)

    return _wrapped


# --- Seam C: apply ----------------------------------------------------------

def make_build_child_wrapper(orig_build_child):
    """Wrap ``_build_child_agent`` to inject per-task creds keyed by task_index.

    Signature mirrors the host (DESIGN §6, seam C). ``task_index`` is the only
    correlation key the host passes, so routing is looked up by index. When a
    task has no override, the original batch/config creds pass through unchanged.
    """

    def _wrapped(task_index, goal, context, toolsets, model, max_iterations,
                 task_count, parent_agent, override_provider=None,
                 override_base_url=None, override_api_key=None,
                 override_api_mode=None, override_acp_command=None,
                 override_acp_args=None, role="leaf", **extra):
        creds = get_creds(task_index)
        if creds:
            model = creds.get("model") or model
            override_provider = creds.get("provider") or override_provider
            override_base_url = creds.get("base_url") or override_base_url
            override_api_key = creds.get("api_key") or override_api_key
            override_api_mode = creds.get("api_mode") or override_api_mode
            override_acp_command = creds.get("command") or override_acp_command
            override_acp_args = creds.get("args") or override_acp_args

            # Hermes 0.21 carries provider personality through these newer
            # build-child kwargs. Only override them when the host resolver
            # actually surfaced task-specific values, so older hosts retain
            # their existing delegation behavior unchanged.
            if creds.get("request_overrides") is not None:
                extra["override_request_overrides"] = dict(creds["request_overrides"])
            if creds.get("max_output_tokens") is not None:
                extra["override_max_tokens"] = creds["max_output_tokens"]
        # Forward by keyword (not position) so a future host that inserts/appends
        # a parameter can't silently misalign creds.
        child = orig_build_child(
            task_index=task_index, goal=goal, context=context, toolsets=toolsets,
            model=model, max_iterations=max_iterations, task_count=task_count,
            parent_agent=parent_agent, override_provider=override_provider,
            override_base_url=override_base_url, override_api_key=override_api_key,
            override_api_mode=override_api_mode,
            override_acp_command=override_acp_command,
            override_acp_args=override_acp_args, role=role, **extra,
        )

        # The host resolves delegation.reasoning_effort > parent while building
        # the child. Apply a task-local override afterwards, so omission preserves
        # that native precedence and provider-specific wire translation remains
        # entirely in Hermes transports. Requests read agent.reasoning_config at
        # call time on supported hosts.
        if creds and "reasoning_config" in creds:
            if not hasattr(child, "reasoning_config"):
                msg = (
                    "delegate-routing: constructed child has no reasoning_config attribute; "
                    "refusing to ignore explicit task reasoning_effort override"
                )
                logger.warning(msg)
                raise ValueError(msg)
            child.reasoning_config = dict(creds["reasoning_config"])
        return child

    return _wrapped


# --- Seam D: async completion display --------------------------------------

def make_async_formatter_wrapper(orig_formatter):
    """Report actual child model(s) in async delegation completion metadata.

    Hermes' background batch event carries a batch-level ``model`` captured
    before per-task routing is applied, while each structured result carries the
    actual child model. Reuse the host formatter and only correct that display
    metadata. For heterogeneous batches, mark the header as per-task and append
    a compact task-to-model mapping rather than copying the host formatter.
    """

    def _wrapped(evt):
        if not isinstance(evt, dict):
            return orig_formatter(evt)

        display_evt = evt
        task_models = []
        results = evt.get("results")
        if isinstance(results, list):
            for r in sorted(
                (r for r in results if isinstance(r, dict)),
                key=lambda item: item.get("task_index", 0),
            ):
                model = r.get("model")
                if isinstance(model, str) and model.strip():
                    task_models.append((r.get("task_index", 0), model.strip()))

        if task_models:
            unique_models = list(dict.fromkeys(model for _, model in task_models))
            display_evt = dict(evt)
            display_evt["model"] = unique_models[0] if len(unique_models) == 1 else "per-task"
            rendered = orig_formatter(display_evt)
            if len(unique_models) > 1 and isinstance(rendered, str):
                mapping = ", ".join(
                    f"{index + 1}={model}" for index, model in task_models
                )
                lines = rendered.splitlines()
                insert_at = next(
                    (i + 1 for i, line in enumerate(lines) if line.startswith("Role: ")),
                    None,
                )
                if insert_at is None:
                    lines.append(f"Task models: {mapping}")
                else:
                    lines.insert(insert_at, f"Task models: {mapping}")
                return "\n".join(lines)
            return rendered

        return orig_formatter(display_evt)

    return _wrapped


# --- Orchestration ----------------------------------------------------------

def _params(fn) -> set:
    return set(inspect.signature(fn).parameters)


def _read_on_error() -> str:
    """Read delegate_routing.on_error from host config; default 'fail'."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        val = (cfg.get("delegate_routing") or {}).get("on_error") or "fail"
        return "fallback" if str(val).strip().lower() == "fallback" else "fail"
    except Exception:
        return "fail"


def _preimport_host() -> None:
    """Import every host module the seams touch, before taking _PATCH_LOCK.

    Importing one of these can re-enter the plugin loader on some hosts
    (model_tools discovers plugins at import time), which calls
    register() → apply_patches() on this same thread. Doing it up-front
    means any such re-entry completes its own full pass and sets the
    sentinel, which the caller re-checks under the lock — instead of
    deadlocking (or double-wrapping) mid-patch. All pre-imports are
    best-effort: the seams below keep their own guards and degrade
    gracefully when a module is genuinely absent.
    """
    import importlib

    for _mod in (
        "tools.registry",
        "tools.process_registry",
        "model_tools",
        "hermes_cli.config",
    ):
        try:
            importlib.import_module(_mod)
        except Exception:
            pass


def _invalidate_tool_defs_cache(registry) -> None:
    """Force the host to recompute cached tool definitions after Seam A.

    ``model_tools.get_tool_definitions(quiet_mode=True)`` — the fast path the
    agent loop uses — memoizes into ``_tool_defs_cache`` keyed partly on
    ``registry._generation``. Assigning ``entry.dynamic_schema_overrides``
    directly does NOT bump ``_generation`` (only register/deregister do), so a
    long-running gateway that populated the memo with the stock schema BEFORE we
    patched keeps serving the field-less schema for the life of the process —
    the LLM never sees the per-task routing fields and can't route. Both hooks
    below are private host internals; each is best-effort and guarded so a
    future host that renames them just degrades to no invalidation.
    """
    try:
        registry._generation += 1
    except Exception:  # pragma: no cover - defensive; private attr may move
        logger.warning(
            "delegate-routing: could not bump registry._generation; a warm "
            "tool-definitions cache may keep serving the stock schema until "
            "the next registry mutation"
        )
    try:
        from model_tools import _clear_tool_defs_cache

        _clear_tool_defs_cache()
    except Exception:  # pragma: no cover - defensive; helper may move/rename
        pass


def _patch_schema(dt) -> None:
    """Seam A — wrap the registered ToolEntry's dynamic_schema_overrides.

    The registry stores a direct reference to the builder at registration time,
    so rebinding the module attribute would NOT reach it — we must update the
    ToolEntry itself.
    """
    try:
        from tools.registry import registry

        entry = registry.get_entry("delegate_task")
    except Exception as exc:
        logger.warning(
            "delegate-routing: registry unavailable; per-task routing fields "
            "will not be advertised in the schema: %s", exc,
        )
        return
    if entry is None:
        logger.warning(
            "delegate-routing: delegate_task not in registry; per-task routing "
            "fields not advertised"
        )
        return
    current = getattr(entry, "dynamic_schema_overrides", None) or getattr(
        dt, "_build_dynamic_schema_overrides", None
    )
    if not callable(current):
        logger.warning("delegate-routing: no schema builder to wrap; skipping Seam A")
        return
    entry.dynamic_schema_overrides = make_schema_override(current)
    # A direct attribute mutation doesn't bump registry._generation, so the
    # memoized tool-definitions cache won't refresh on its own — invalidate it.
    _invalidate_tool_defs_cache(registry)


def _patch_async_formatter() -> bool:
    """Seam D — correct stale batch-level model metadata in async notices."""
    try:
        import importlib

        process_registry = importlib.import_module("tools.process_registry")
    except Exception as exc:
        logger.warning(
            "delegate-routing: async completion formatter unavailable; "
            "routing remains active but completion notices may show the batch model: %s",
            exc,
        )
        return False

    current = getattr(process_registry, "_format_async_delegation", None)
    if not callable(current):
        logger.warning(
            "delegate-routing: tools.process_registry._format_async_delegation "
            "unavailable; async completion model display not patched"
        )
        return False
    if getattr(current, "_hdr_delegate_routing_display", False):
        return True

    wrapped = make_async_formatter_wrapper(current)
    wrapped.__dict__["_hdr_delegate_routing_display"] = True
    vars(process_registry)["_format_async_delegation"] = wrapped
    return True


def apply_patches() -> bool:
    """Install the four seams on the host, once. Returns True if routing is active.

    Refuses to patch (returns False, no changes) if the host isn't importable or
    its function signatures don't match expectations — the plugin degrades to a
    no-op and core behavior is untouched. Idempotent via a module sentinel.
    """
    try:
        import tools.delegate_tool as dt
    except Exception as exc:
        logger.warning(
            "delegate-routing: host tools.delegate_tool not importable; "
            "plugin inactive: %s", exc,
        )
        return False

    # Import the remaining host modules before locking (see _preimport_host).
    _preimport_host()

    with _PATCH_LOCK:
        if getattr(dt, "_HDR_PATCHED", False):
            return True

        try:
            dt_params = _params(dt.delegate_task)
            bc_params = _params(dt._build_child_agent)
        except Exception as exc:
            logger.warning(
                "delegate-routing: cannot introspect host functions; not patching: %s", exc,
            )
            return False

        schema_builder = getattr(dt, "_build_dynamic_schema_overrides", None)
        missing_dt = _EXPECTED_DELEGATE_PARAMS - dt_params
        missing_bc = _EXPECTED_BUILD_CHILD_PARAMS - bc_params
        if missing_dt or missing_bc or not callable(schema_builder):
            logger.warning(
                "delegate-routing: host signature mismatch — NOT patching. "
                "delegate_task missing=%s, _build_child_agent missing=%s, "
                "schema_builder=%r. This hermes-agent version may be unsupported; "
                "see docs/DESIGN.md §10.",
                sorted(missing_dt), sorted(missing_bc), schema_builder,
            )
            return False

        on_error = _read_on_error()
        tool_error = getattr(dt, "tool_error", None)

        # Seam B (capture) and Seam C (apply) via module-attribute rebind. Both
        # call sites resolve these as module globals at call time, so this reaches
        # every path including run_agent._dispatch_delegate_task (DESIGN §6.1).
        dt.delegate_task = make_delegate_task_wrapper(
            dt.delegate_task, resolve_model_provider_override,
            on_error=on_error, tool_error=tool_error,
        )
        dt._build_child_agent = make_build_child_wrapper(dt._build_child_agent)

        # Seam A (schema) via the ToolEntry.
        _patch_schema(dt)

        # Seam D is display-only. If a host moves/removes the formatter, routing
        # still remains valid; we log the degraded display behavior explicitly.
        display_patched = _patch_async_formatter()

        dt._HDR_PATCHED = True
        logger.info(
            "delegate-routing: active — patched delegate_task, _build_child_agent, "
            "delegate schema, and async model display=%s for model/provider/reasoning "
            "routing (on_error=%s)",
            display_patched, on_error,
        )
        return True
