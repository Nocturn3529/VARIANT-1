"""Unit tests for perception quality metrics helpers."""

from __future__ import annotations

import desktop.errors as derr
import desktop.perception_quality as pq
from desktop import vision_capture as vc


def test_merge_observability_config_defaults():
    cfg = pq.merge_observability_config(None)
    assert cfg.enabled is True
    assert cfg.verbosity == pq.VERBOSITY_NORMAL


def test_merge_observability_config_bool_and_dict():
    off = pq.merge_observability_config({"quality_metrics": False})
    assert off.enabled is False

    compact = pq.merge_observability_config({
        "quality_metrics": {"enabled": True, "verbosity": "compact"},
    })
    assert compact.enabled is True
    assert compact.verbosity == "compact"


def test_should_emit_respects_off():
    assert pq.should_emit(pq.QualityMetricsConfig(enabled=False)) is False
    assert pq.should_emit(pq.QualityMetricsConfig(enabled=True, verbosity="off")) is False
    assert pq.should_emit(pq.QualityMetricsConfig(enabled=True, verbosity="normal")) is True


def test_format_metrics_line_normal():
    ctx = pq.PerceptionQualityInput(
        tool="read_ui",
        step="perceive",
        controls=42,
        yield_status="ok",
        reread=True,
        perception_mode="incremental",
        incremental_savings_pct=65,
        incremental_changes=3,
        capture_mode="window",
        capture_size="1280×720",
        stack_depth=2,
    )
    line = pq.format_metrics_line(ctx, verbosity="normal")
    assert line.startswith("Perception quality:")
    assert "read_ui" in line
    assert "42 ctrl" in line
    assert "yield:ok" in line
    assert "reread" in line
    assert "incremental" in line
    assert "Δ:65%" in line
    assert "cap:window" in line
    assert "stack:2" in line


def test_format_metrics_line_compact():
    ctx = pq.PerceptionQualityInput(
        tool="computer",
        controls=0,
        reread=True,
        error_type="COORDINATE_MISMATCH",
        error_severity="recoverable",
    )
    line = pq.format_metrics_line(ctx, verbosity="compact")
    assert "computer" in line
    assert "reread" in line
    assert "err:COORDINATE_MISMATCH" in line
    assert "yield:" not in line


def test_build_payload_includes_structured_fields():
    ctx = pq.PerceptionQualityInput(tool="computer", controls=10, reread=True)
    payload = pq.build_payload(ctx, verbosity="normal")
    assert payload["tool"] == "computer"
    assert payload["controls"] == 10
    assert payload["reread"] is True
    assert "text" in payload
    assert payload["verbosity"] == "normal"


def test_enrich_helpers():
    ctx = pq.PerceptionQualityInput()
    err = derr.capture_failed_error("capture missing")
    pq.enrich_from_error(ctx, err)
    assert ctx.error_type == "CAPTURE_FAILED"

    meta = vc.CaptureMeta(mode="window", width=800, height=600)
    pq.enrich_from_capture_meta(ctx, meta)
    assert ctx.capture_mode == "window"
    assert "800" in ctx.capture_size


def test_infer_yield_status():
    assert pq.infer_yield_status(10, min_controls=3) == "ok"
    assert pq.infer_yield_status(1, min_controls=3) == "low"
    assert pq.infer_yield_status(20, recovered=True) == "recovered"


def test_perception_event_text():
    fields = {"text": "Perception quality: read_ui · 5 ctrl"}
    assert pq.perception_event_text(fields) == fields["text"]
