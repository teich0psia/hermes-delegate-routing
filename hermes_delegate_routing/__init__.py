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
_SKILL_DESCRIPTION = "Use when routing a Hermes subagent by model/provider."


def _skill_md_path() -> Path:
    return Path(__file__).parent / "skills" / _SKILL_DIRNAME / _SKILL_FILENAME


def _register_bundled_skill(ctx) -> bool:
    """Register the bundled recovery skill. Returns True when live.

    Plugin skills are read-only and namespaced
    (``delegate_routing:delegate-routing``); they are explicit loads only, not
    part of the system prompt's ``<available_skills>`` index (though the host
    currently surfaces plugin-skill metadata via ``skills_list``). Never
    raises: ``ctx=None``, a missing ``register_skill`` attribute, a missing
    SKILL.md, or a registration error all degrade to ``False``.
    """
    try:
        if ctx is None or not hasattr(ctx, "register_skill"):
            return False
        skill_md = _skill_md_path()
        if not skill_md.exists():
            logger.debug("hermes-delegate-routing: bundled skill missing at %s", skill_md)
            return False
        try:
            frontmatter = {"version": __version__}
            ctx.register_skill(_SKILL_DIRNAME, skill_md, _SKILL_DESCRIPTION, frontmatter)
        except TypeError:
            # Older hosts accept only (name, path).
            ctx.register_skill(_SKILL_DIRNAME, skill_md)
        return True
    except Exception:  # pragma: no cover - registration must never break startup
        logger.debug("hermes-delegate-routing: bundled skill registration failed", exc_info=True)
        return False


def register(ctx=None) -> None:
    """Plugin entry point — called once at startup by the Hermes plugin loader.

    Installs the four monkeypatch seams (schema, capture, apply, async display)
    plus the bundled recovery skill. Never raises: every step is guarded, and a
    missing host or mismatched signatures degrade to a no-op with a warning
    (see ``patches.apply_patches``).
    """
    try:
        skill_live = _register_bundled_skill(ctx)
        if hasattr(ctx, "on_unload") and callable(getattr(ctx, "on_unload", None)):
            try:
                from .patches import restore_patches

                ctx.on_unload(restore_patches)
            except Exception:
                logger.debug(
                    "hermes-delegate-routing: on_unload hook registration failed",
                    exc_info=True,
                )
        from . import patches as _patches
        from .patches import apply_patches

        _patches.SKILL_LIVE = bool(skill_live)
        active = apply_patches()
    except Exception:  # pragma: no cover - defensive; must never break startup
        logger.exception("hermes-delegate-routing: unexpected error during patch")
        return
    if active:
        if not skill_live:
            logger.warning(
                "hermes-delegate-routing v%s active but bundled skill not "
                "registered — routing-error skill pointers may be stale",
                __version__,
            )
        else:
            logger.info("hermes-delegate-routing v%s active", __version__)
    else:
        logger.warning(
            "hermes-delegate-routing v%s loaded but INACTIVE (host unavailable "
            "or unsupported signature)", __version__,
        )
