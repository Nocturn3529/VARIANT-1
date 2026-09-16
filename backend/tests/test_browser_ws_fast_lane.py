"""The visible-browser response bypasses ordinary WebSocket dispatch."""

from pathlib import Path


def test_embedded_browser_result_has_receive_loop_fast_lane():
    source = (Path(__file__).parents[1] / "server_http.py").read_text(
        encoding="utf-8"
    )
    fast_lane = source.index('if mtype == "browser:host:result"')
    ordinary_dispatch = source.index(
        "handled = await ws_dispatch.dispatch", fast_lane
    )

    assert fast_lane < ordinary_dispatch
    assert "interactive.resolve_host_result(" in source[fast_lane:ordinary_dispatch]
    assert 'mtype.startswith("browser-fabric:")' not in source
