"""Observable device status for the explicit ``/system-status`` command."""

from __future__ import annotations

import asyncio
import os


def _mib(value: int) -> int:
    return int(value) // (1024 ** 2)


def _gib(value: int) -> float:
    return int(value) / (1024 ** 3)


def _web_search_status_lines(web_search_config: dict) -> list[str]:
    """Compact provider health for the direct /system-status response."""
    try:
        from web_search import providers as web_search_providers

        status = web_search_providers.public_status(web_search_config)
        provider = str(status.get("provider") or "unknown")
        if provider != "variant1":
            return [f"- Web search: {provider}"]
        variant1 = status.get("variant1") if isinstance(status.get("variant1"), dict) else {}
        if not variant1.get("checked"):
            engines = variant1.get("engines") or []
            suffix = f" ({', '.join(str(x) for x in engines)})" if engines else ""
            return [f"- Web search: VARIANT-1 Search, not checked yet{suffix}"]
        healthy = int(variant1.get("healthy_engines") or 0)
        total = int(variant1.get("engine_count") or 0)
        latency = int(variant1.get("last_latency_ms") or 0)
        state = "degraded" if variant1.get("degraded") else "healthy"
        cache = ", cache hit" if variant1.get("cache_hit") else ""
        lines = [
            f"- Web search: VARIANT-1 Search {state}, {healthy}/{total} engines, "
            f"{latency} ms{cache}"
        ]
        error = " ".join(str(variant1.get("last_error") or "").split())
        if error:
            lines.append(f"- Web search last issue: {error[:300]}")
        return lines
    except Exception:
        return ["- Web search: status unavailable"]


def collect_status(web_search_config: dict) -> str:
    try:
        import psutil
    except Exception as exc:
        raise RuntimeError("System status is unavailable because psutil is not installed.") from exc

    cpu = psutil.cpu_percent(interval=0.3)
    vm = psutil.virtual_memory()
    volume = os.path.abspath(os.sep)
    disk = psutil.disk_usage(volume)
    lines = [
        "System status",
        f"- CPU: {cpu:.0f}%",
        (
            f"- RAM: {vm.percent:.0f}% used "
            f"({_mib(vm.used):,} MiB used, {_mib(vm.available):,} MiB available, "
            f"{_mib(vm.total):,} MiB total)"
        ),
        (
            f"- Disk ({volume}): {disk.percent:.0f}% used "
            f"({_gib(disk.used):,.1f} GiB used, {_gib(disk.free):,.1f} GiB free, "
            f"{_gib(disk.total):,.1f} GiB total)"
        ),
    ]
    try:
        battery = psutil.sensors_battery()
    except Exception:
        battery = None
    if battery is None:
        lines.append("- Battery: not reported by this device")
    else:
        state = "charging" if battery.power_plugged else "on battery"
        lines.append(f"- Battery: {battery.percent:.0f}% ({state})")
    lines.extend(_web_search_status_lines(web_search_config))
    return "\n".join(lines)


async def status_text(web_search_config: dict) -> str:
    """Collect status off the event loop; no model tool or inference involved."""
    return await asyncio.to_thread(collect_status, dict(web_search_config or {}))
