"""Tests for Seam D: async completion model display correction."""

from __future__ import annotations

from hermes_delegate_routing.patches import make_async_formatter_wrapper


def _formatter(evt):
    return f"Role: {evt.get('role', 'leaf')}   Model: {evt.get('model', '?')}\nRESULT"


def test_single_actual_model_replaces_stale_batch_model_without_mutating_event():
    wrapped = make_async_formatter_wrapper(_formatter)
    evt = {
        "role": "leaf",
        "model": "qwen3.8-max-preview",
        "results": [
            {"task_index": 0, "model": "deepseek-v4-flash"},
        ],
    }

    rendered = wrapped(evt)

    assert "Model: deepseek-v4-flash" in rendered
    assert "qwen3.8-max-preview" not in rendered
    assert evt["model"] == "qwen3.8-max-preview"


def test_homogeneous_batch_reports_actual_model_once():
    wrapped = make_async_formatter_wrapper(_formatter)
    evt = {
        "role": "leaf",
        "model": "batch-default",
        "results": [
            {"task_index": 0, "model": "deepseek-v4-flash"},
            {"task_index": 1, "model": "deepseek-v4-flash"},
        ],
    }

    rendered = wrapped(evt)

    assert "Model: deepseek-v4-flash" in rendered
    assert "Task models:" not in rendered


def test_mixed_batch_reports_per_task_mapping_in_task_order():
    wrapped = make_async_formatter_wrapper(_formatter)
    evt = {
        "role": "leaf",
        "model": "batch-default",
        "results": [
            {"task_index": 1, "model": "gpt-5.6-codex"},
            {"task_index": 0, "model": "deepseek-v4-flash"},
        ],
    }

    rendered = wrapped(evt)

    assert "Model: per-task" in rendered
    assert "Task models: 1=deepseek-v4-flash, 2=gpt-5.6-codex" in rendered
    assert rendered.index("Model: per-task") < rendered.index("Task models:")


def test_missing_result_models_preserves_host_output():
    wrapped = make_async_formatter_wrapper(_formatter)
    evt = {
        "role": "leaf",
        "model": "batch-default",
        "results": [{"task_index": 0, "status": "failed"}],
    }

    assert wrapped(evt) == _formatter(evt)


def test_non_dict_event_is_passthrough():
    seen = []

    def formatter(evt):
        seen.append(evt)
        return "host"

    wrapped = make_async_formatter_wrapper(formatter)
    assert wrapped("raw") == "host"
    assert seen == ["raw"]
