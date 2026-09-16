from __future__ import annotations

from observability.trace_exporters import OpenTelemetryTraceExporter, flatten_attributes


def test_flatten_attributes_preserves_scalar_identity_and_bounds_objects():
    attrs = flatten_attributes({
        "run_id": "run_1",
        "sequence": 3,
        "attributes": {
            "tool": "read_file",
            "nested": {"a": 1},
            "text": "private result",
            "args_preview": "secret=1",
        },
    })
    assert attrs["variant1.run_id"] == "run_1"
    assert attrs["variant1.sequence"] == 3
    assert attrs["variant1.attributes.tool"] == "read_file"
    assert attrs["variant1.attributes.nested"] == "<dict items=1>"
    assert attrs["variant1.attributes.text.redacted"] is True
    assert attrs["variant1.attributes.text.chars"] == len("private result")
    assert attrs["variant1.attributes.args_preview.redacted"] is True
    assert "private result" not in str(attrs)
    assert "secret=1" not in str(attrs)


def test_opentelemetry_exporter_pairs_matching_start_and_end():
    exporter = OpenTelemetryTraceExporter(tracer_name="variant1.tests")
    base = {
        "schema": "variant1.trace.v1",
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
        "parent_span_id": "3" * 16,
        "event": "tool:start",
        "timestamp": 1.0,
        "status": "running",
        "attributes": {"call_id": "call_1"},
    }
    exporter({**base, "phase": "start"})
    assert len(exporter._open) == 1
    exporter({
        **base,
        "event": "tool:result",
        "phase": "end",
        "timestamp": 2.0,
        "status": "ok",
    })
    assert len(exporter._open) == 0
    exporter.close()
