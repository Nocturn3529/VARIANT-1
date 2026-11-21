"""Reasoning contracts advertised by the signed-in Nous catalog, keyed by exact ID."""
from __future__ import annotations

from collections.abc import Mapping

DEFAULT_MODEL = "meituan/longcat-2.5:free"


def reasoning_rules(rows: list[dict]) -> dict[str, dict]:
    rules = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        model = row["id"].strip()
        if not model:
            continue
        info = row.get("reasoning")
        info = info if isinstance(info, Mapping) else {}
        params = row.get("supported_parameters")
        params = params if isinstance(params, list) else []
        levels = info.get("supported_efforts")
        mandatory = info.get("mandatory")
        efforts = tuple(dict.fromkeys(
            value for value in (levels if isinstance(levels, list) else ())
            if isinstance(value, str) and value and len(value) <= 24
            and not (mandatory is True and value in {"off", "none"})
        ))
        field = ("reasoning.effort" if "reasoning" in params else
                 "reasoning_effort" if "reasoning_effort" in params else "")
        efforts = efforts if field else ()
        rule = {"efforts": efforts, "effort_field": field if efforts else "", "default_effort": ""}
        if efforts and field == "reasoning.effort":
            rule["enabled_field"] = "reasoning.enabled"
        default = info.get("default_effort")
        if mandatory is False and info.get("default_enabled") is False:
            # A preferred effort when enabled must not turn reasoning on.
            rule["default_effort"] = next((v for v in ("none", "off") if v in efforts), "")
        elif default in efforts:
            rule["default_effort"] = default
        if isinstance(mandatory, bool):
            rule["mandatory"] = mandatory
        if isinstance(info.get("default_enabled"), bool):
            rule["default_enabled"] = info["default_enabled"]
        if not efforts and mandatory is False and "reasoning" in params:
            rule["toggle"] = True
            rule["minimum_fields"] = {"reasoning.enabled": False}
        rules[model] = rule
    return rules
