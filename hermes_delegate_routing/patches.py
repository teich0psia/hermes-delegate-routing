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
import re
import sys
import threading

from ._state import ROUTING, get_creds
from .resolver import resolve_model_provider_override

logger = logging.getLogger(__name__)

# Secret-shaped fragments that must never echo back in tool errors or logs.
# Model/provider literals are user-controlled; the host exception may carry a
# custom provider URL or credential-resolver detail. Keep only a redacted hint.
_REDACT_PATTERNS = (
    re.compile(r"(api[_-]?key\s*[:=]\s*)(['\"]?)[^\s'\",;}]+", re.IGNORECASE),
    re.compile(r"(authorization\s*[:=]\s*)(['\"]?)[^\s'\",;}]+", re.IGNORECASE),
    re.compile(r"([?&](?:key|token|secret|signature|sig|auth)[^=]*=)[^&\s'\",;}]+", re.IGNORECASE),
)


def _redact(text: str) -> str:
    """Replace secret-shaped fragments with [REDACTED]; never raises."""
    try:
        out = str(text)
    except Exception:
        return "[unprintable]"
    for pat in _REDACT_PATTERNS:
        try:
            out = pat.sub(r"\1[REDACTED]", out)
        except Exception:
            continue
    return out[:2000]

# Serializes apply_patches() so a concurrent second caller can't pass the
# _HDR_PATCHED check before the first sets it (which would double-wrap).
# A re-entrant lock (not a plain Lock): importing a host module can
# re-enter the plugin loader on some hosts (the known example is
# model_tools, which discovers plugins at import — deliberately never
# imported from this path since upstream fac3877), which calls register()
# → apply_patches() on this same thread. A plain Lock would deadlock
# there; with an RLock the nested pass proceeds and the sentinel re-check
# below keeps patching single (see _preimport_host()). Cross-thread module
# import-lock cycles are NOT covered by this RLock — those are avoided by
# never starting a model_tools import here at all.
_PATCH_LOCK = threading.RLock()

# True only when the bundled recovery skill registered live in this process.
# The fail-closed error pointer names the skill; when False, the pointer is
# downgraded to the schema guidance so we never advertise a dead target.
SKILL_LIVE = False

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

# Modules that may hold the async completion formatter, newest host first.
# Hermes moved _format_async_delegation from tools.process_registry to
# tools.process_registry_notifications; _patch_async_formatter() tries each.
_ASYNC_FORMATTER_CANDIDATES = (
    "tools.process_registry_notifications",
    "tools.process_registry",
)


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

# Qualified plugin-skill name for recovery pointers (namespace = entry-point
# name ``delegate_routing``; bare name = skill directory name). Keep in sync
# with hermes_delegate_routing/skills/delegate-routing/SKILL.md and register().
_SKILL_REF = "delegate_routing:delegate-routing"
_SKILL_HINT = (
    f" See skill '{_SKILL_REF}' via skill_view (per-task routing lives inside "
    "tasks[i] only; there is no top-level model/provider/reasoning_effort argument)."
)
_SKILL_FALLBACK_HINT = (
    " (bundled recovery skill not registered in this process — per-task routing "
    "lives inside tasks[i] only; there is no top-level model/provider/reasoning_effort "
    "argument)."
)
_MINIMAL_EXAMPLE = (
    '{"tasks": [{"goal": "...", "model": "<exact model ID after /model>", '
    '"provider": "<exact provider id>"}]}'
)


def _skill_pointer() -> str:
    """Recovery pointer: skill URI when live, schema guidance otherwise."""
    if SKILL_LIVE:
        return _SKILL_HINT
    return _SKILL_FALLBACK_HINT

_TASK_MODEL_DESC = (
    "Per-task model for THIS child only — set inside tasks[i]; there is no "
    "top-level model argument (dropped before the tool runs). Accepts a model "
    "ID or alias as used after /model (e.g. 'sonnet', 'gemini-flash-2.0'; do "
    "not include '/model' itself), optionally with an inline '--provider <id>' "
    "(which then counts as an explicit provider). Do NOT use provider:model "
    "syntax; set the separate 'provider' field instead. A model-only task "
    "(no explicit provider anywhere) stays on the inherited delegation/parent "
    "provider — set both model and provider to cross providers. Use the exact "
    "literal; under the default on_error=fail, unresolvable values fail the "
    "whole call instead of silently running another model (on_error=fallback "
    "skips just that override). When omitted, the child inherits the "
    "batch/config model."
)
_TASK_PROVIDER_DESC = (
    "Per-task provider id for THIS child only — set inside tasks[i]; there is no "
    "top-level provider argument. Must be an exact configured provider id; "
    "under the default on_error=fail, unresolvable values fail the whole call "
    "instead of silently running another provider (on_error=fallback skips "
    "just that override). A provider-only task reuses the inherited "
    "delegation/parent model. Prefer this structured field over embedding "
    "'--provider' in 'model' for JSON tool calls."
)
_TASK_REASONING_DESC = (
    "Per-task reasoning effort for THIS child only — set inside tasks[i]. Uses the "
    "same values as Hermes reasoning_effort (for example 'none', 'minimal', 'low', "
    "'medium', 'high', 'xhigh', 'max', or 'ultra', depending on the host version). "
    "Unsupported values fail the whole call under on_error=fail (skipped under "
    "fallback). When omitted, normal delegation.reasoning_effort > parent-agent "
    "inheritance is preserved."
)


def make_schema_override(orig_builder):
    """Wrap the host schema builder to advertise per-task routing fields.

    Deep-copies the original output before injecting because the host builder only
    shallow-copies each property, leaving ``tasks.items`` aliased to the static
    schema — mutating it in place would corrupt the host's schema (DESIGN §6).

    Merge policy for pre-existing fields: when the host (or another plugin)
    already defines ``model`` / ``provider`` / ``reasoning_effort`` on the task
    schema, the existing definition wins for ``type``/``enum`` — we only fill a
    missing ``description`` with ours. This keeps forward-compat with a future
    host that ships native per-task routing instead of clobbering it.
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
        for _name, _desc in (
            ("model", _TASK_MODEL_DESC),
            ("provider", _TASK_PROVIDER_DESC),
            ("reasoning_effort", _TASK_REASONING_DESC),
        ):
            _existing = props.get(_name)
            if _existing is None:
                props[_name] = {"type": "string", "description": _desc}
            elif isinstance(_existing, dict) and not _existing.get("description"):
                _existing["description"] = _desc
        return result

    _wrapped.__dict__["_hdr_delegate_routing_schema"] = True
    return _wrapped


# --- Seam B: capture --------------------------------------------------------

def _resolve_reasoning_override(value):
    """Parse an explicit task reasoning effort through the host chokepoint."""
    try:
        from hermes_constants import parse_reasoning_effort
    except Exception as exc:  # pragma: no cover - host import guard
        raise ValueError(f"Cannot import Hermes reasoning parser: {_redact(exc)}") from exc

    parsed = parse_reasoning_effort(value)
    if parsed is None:
        raise ValueError(f"Unsupported reasoning_effort {_redact(value)!r}")
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
                            i, model, provider, reasoning_effort, _redact(exc),
                        )
                        continue
                    return _tool_error(
                        "delegate_task routing: could not resolve task override "
                        f"for task {i} (model={model!r}, provider={provider!r}, "
                        f"reasoning_effort={reasoning_effort!r}): {_redact(exc)}."
                        f"{_skill_pointer()} Example: {_MINIMAL_EXAMPLE}",
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

    _wrapped.__dict__["_hdr_delegate_routing_capture"] = True
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

    _wrapped.__dict__["_hdr_delegate_routing_apply"] = True
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
    """Import host modules the seams touch, before taking _PATCH_LOCK.

    Importing one of these can re-enter the plugin loader on some hosts,
    which calls register() → apply_patches() on this same thread. Doing it
    up-front means any such re-entry completes its own full pass and sets
    the sentinel, which the caller re-checks under the lock — instead of
    deadlocking (or double-wrapping) mid-patch. All pre-imports are
    best-effort: the seams below keep their own guards and degrade
    gracefully when a module is genuinely absent.

    ``model_tools`` is deliberately NOT pre-imported here. On real hosts it
    runs plugin discovery at import time, and starting that import from
    register()/apply_patches() can cycle on the module import lock across
    threads (main thread importing model_tools while a loader thread
    re-enters registration). An RLock cannot break that cross-thread cycle
    — only never starting the import from this path can (upstream fac3877).
    The same ban applies to _invalidate_tool_defs_cache() below.
    """
    import importlib

    for _mod in (
        "tools.registry",
        "tools.process_registry",
        "tools.process_registry_notifications",
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
    _lock = getattr(registry, "_lock", None)
    _acquire = getattr(_lock, "acquire", None)
    _release = getattr(_lock, "release", None)
    _use_lock = _lock is not None and callable(_acquire) and callable(_release)
    if _use_lock:
        try:
            _acquire()  # type: ignore[misc]
        except Exception:
            _use_lock = False
    try:
        try:
            registry._generation += 1
        except Exception:  # pragma: no cover - defensive; private attr may move
            logger.warning(
                "delegate-routing: could not bump registry._generation; a warm "
                "tool-definitions cache may keep serving the stock schema until "
                "the next registry mutation"
            )
    finally:
        if _use_lock:
            try:
                _release()  # type: ignore[misc]
            except Exception:
                pass
    # Never `from model_tools import ...` here: this runs during plugin
    # registration/discovery, and starting a model_tools import from this
    # path can deadlock on the module import lock against a concurrent
    # model_tools import on another thread (upstream fac3877), which an
    # RLock cannot break. Only reuse an already-loaded module object; a
    # bumped registry._generation already forces a recompute on the first
    # get_tool_definitions() call when model_tools is not loaded yet.
    # A partially-initialized model_tools (mid-import on another thread,
    # visible in sys.modules without the helper yet) is also tolerated via
    # the getattr/callable guard.
    mt = sys.modules.get("model_tools")
    clear_cache = getattr(mt, "_clear_tool_defs_cache", None) if mt is not None else None
    if callable(clear_cache):
        try:
            clear_cache()
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
    """Seam D — correct stale batch-level model metadata in async notices.

    Tries each known formatter location, newest host first. Hermes moved
    the async completion formatter from ``tools.process_registry`` to
    ``tools.process_registry_notifications`` (both expose
    ``_format_async_delegation(evt)`` with a ``Role: `` preamble line the
    display wrapper keys on). Every location holding a callable,
    unmarked formatter is wrapped. Patching several locations is safe
    against the observed host migration: module-attribute rebinding does
    not re-fire through already-bound names, and the migration kept a
    plain alias rather than a dynamic forward, so no event passes two
    wrappers. (A host that dynamically forwarded one location into
    another could duplicate the mapping line; none observed.)
    Returns True when at least one seam is active.
    """
    import importlib

    patched_any = False
    for mod_name in _ASYNC_FORMATTER_CANDIDATES:
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            logger.debug(
                "delegate-routing: async formatter candidate %s unavailable: %s",
                mod_name,
                exc,
            )
            continue
        current = getattr(module, "_format_async_delegation", None)
        if not callable(current):
            continue
        if getattr(current, "_hdr_delegate_routing_display", False):
            patched_any = True
            continue
        wrapped = make_async_formatter_wrapper(current)
        wrapped.__dict__["_hdr_delegate_routing_display"] = True
        vars(module)["_format_async_delegation"] = wrapped
        logger.debug("delegate-routing: patched %s display", mod_name)
        patched_any = True
    if not patched_any:
        logger.warning(
            "delegate-routing: async completion formatter unavailable; "
            "routing remains active but completion notices may show the batch model"
        )
    return patched_any


_RESTORE_STATE: dict = {}


def _snapshot_for_restore(dt, registry_entry) -> None:
    """Record pre-patch originals so restore_patches() can unwind exactly once."""
    if _RESTORE_STATE.get("taken"):
        return
    _RESTORE_STATE.update(
        {
            "taken": True,
            "delegate_task": dt.delegate_task,
            "build_child": dt._build_child_agent,
            "schema_builder": getattr(registry_entry, "dynamic_schema_overrides", None)
            if registry_entry is not None
            else None,
            "entry": registry_entry,
            "formatters": [],
        }
    )
    import importlib

    for _mod_name in _ASYNC_FORMATTER_CANDIDATES:
        try:
            _module = importlib.import_module(_mod_name)
        except Exception:
            continue
        try:
            _current = vars(_module).get("_format_async_delegation")
        except Exception:
            continue
        _RESTORE_STATE["formatters"].append((_module, _current))


def restore_patches() -> None:
    """Best-effort unload: restore pre-patch originals (identity-checked).

    Registered via ``ctx.on_unload`` when the host supports it. Rebinding only
    when the current attribute is still ours keeps a foreign re-patch (or a
    second plugin generation) from being clobbered. Never raises.
    """
    try:
        import tools.delegate_tool as dt
    except Exception:
        return
    if not _RESTORE_STATE.get("taken"):
        return
    try:
        if getattr(dt.delegate_task, "_hdr_delegate_routing_capture", False):
            dt.delegate_task = _RESTORE_STATE["delegate_task"]
        if getattr(dt._build_child_agent, "_hdr_delegate_routing_apply", False):
            dt._build_child_agent = _RESTORE_STATE["build_child"]
        _entry = _RESTORE_STATE.get("entry")
        if _entry is not None:
            _current_builder = getattr(_entry, "dynamic_schema_overrides", None)
            if getattr(_current_builder, "_hdr_delegate_routing_schema", False):
                _entry.dynamic_schema_overrides = _RESTORE_STATE["schema_builder"]
                try:
                    from tools.registry import registry as _registry

                    _invalidate_tool_defs_cache(_registry)
                except Exception:
                    pass
        for _module, _original in _RESTORE_STATE.get("formatters", []):
            try:
                _current = vars(_module).get("_format_async_delegation")
            except Exception:
                continue
            if getattr(_current, "_hdr_delegate_routing_display", False):
                if _original is None:
                    vars(_module).pop("_format_async_delegation", None)
                else:
                    vars(_module)["_format_async_delegation"] = _original
        try:
            if getattr(dt, "_HDR_PATCHED", False):
                dt._HDR_PATCHED = False
        except Exception:
            pass
    except Exception:
        logger.debug("hermes-delegate-routing: restore failed", exc_info=True)
    finally:
        _RESTORE_STATE.clear()


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

        # Snapshot BEFORE mutating so a mid-install failure (or a later unload)
        # can restore the exact originals. _patch_schema needs the live entry.
        try:
            from tools.registry import registry as _live_registry

            _live_entry = _live_registry.get_entry("delegate_task")
        except Exception:
            _live_entry = None
        _snapshot_for_restore(dt, _live_entry)

        # Seam B (capture) and Seam C (apply) via module-attribute rebind. Both
        # call sites resolve these as module globals at call time, so this reaches
        # every path including run_agent._dispatch_delegate_task (DESIGN §6.1).
        try:
            dt.delegate_task = make_delegate_task_wrapper(
                dt.delegate_task, resolve_model_provider_override,
                on_error=on_error, tool_error=tool_error,
            )
            dt._build_child_agent = make_build_child_wrapper(dt._build_child_agent)
        except Exception as exc:
            logger.warning(
                "delegate-routing: seam B/C install failed — restoring originals: %s", exc
            )
            restore_patches()
            return False

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
