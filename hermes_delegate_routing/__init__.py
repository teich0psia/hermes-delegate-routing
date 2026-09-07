"""hermes-delegate-routing — explicit per-task delegate routing.

A fork-free Hermes Agent plugin. It lets a batch delegation route each subagent
to a different model/provider/reasoning effort via per-task fields:

    delegate_task(tasks=[
        {
            "goal": "cheap summarize", "model": "gemini-flash-2.0",
            "provider": "openrouter", "reasoning_effort": "low",
        },
        {
            "goal": "careful review", "model": "sonnet",
            "provider": "anthropic", "reasoning_effort": "high",
        },
    ])

Design and rationale live in docs/DESIGN.md. In short: `delegate_task` is
special-cased in the host runtime to bypass the tool registry, so the sanctioned
`register_tool(override=True)` path cannot intercept it. This plugin instead
applies four narrow, idempotent runtime monkeypatches at load time (schema
advertise → capture per-task routing → apply per child → correct async completion
model display). See docs/DESIGN.md §6 and §6.1.

Only per-task `tasks[i].model`/`.provider`/`.reasoning_effort` is supported
(top-level routing fields are dropped by the host before they reach the tool).
This matches upstream's own recommended `tasks=[...]` call shape.
"""

from __future__ import annotations

import logging
from pathlib import Path

__version__ = "0.3.0"

logger = logging.getLogger(__name__)

_SKILL_DIRNAME = "delegate-routing"
_SKILL_FILENAME = "SKILL.md"


def _register_bundled_skill(ctx) -> None:
    """Register the bundled recovery skill (best-effort, never fatal).

    Plugin skills are read-only and namespaced (``delegate_routing:delegate-routing``);
    they do not enter ``~/.hermes/skills/`` nor ``<available_skills>``. The skill
    exists so the routing-failure error pointer always has a live target.
    """
    if ctx is None or not hasattr(ctx, "register_skill"):
        return
    skill_md = Path(__file__).parent / "skills" / _SKILL_DIRNAME / _SKILL_FILENAME
    if not skill_md.exists():
        logger.debug("hermes-delegate-routing: bundled skill missing at %s", skill_md)
        return
    try:
        ctx.register_skill(_SKILL_DIRNAME, skill_md)
    except Exception:  # pragma: no cover - registration must never break startup
        logger.debug("hermes-delegate-routing: bundled skill registration failed", exc_info=True)


def register(ctx=None) -> None:
    """Plugin entry point — called once at startup by the Hermes plugin loader.

    Installs the four monkeypatch seams (schema, capture, apply, async display)
    plus the bundled recovery skill. Safe to call without a live ``ctx``.
    Never raises: if the host is missing or its signatures don't match, the
    plugin degrades to a no-op and logs a warning (see ``patches.apply_patches``).
    """
    from .patches import apply_patches

    _register_bundled_skill(ctx)
    try:
        active = apply_patches()
    except Exception:  # pragma: no cover - defensive; must never break startup
        logger.exception("hermes-delegate-routing: unexpected error during patch")
        return
    if active:
        logger.info("hermes-delegate-routing v%s active", __version__)
    else:
        logger.warning(
            "hermes-delegate-routing v%s loaded but INACTIVE (host unavailable "
            "or unsupported signature)", __version__,
        )
