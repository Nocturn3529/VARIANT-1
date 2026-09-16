"""Deterministic UIA perception with one bounded reread on sparse trees."""

from __future__ import annotations

import asyncio
from typing import Any

from . import elevation as delev
from . import errors as derr
from . import perception_recovery as prec


async def perceive_ui(
    ctx: Any,
    args: dict | None,
    *,
    tool_name: str = "computer",
) -> tuple[str, str, list]:
    """Read UIA controls and optionally perform one deterministic reread.

    This flow never captures pixels and never calls a model. Visual inspection
    and screenshots remain an explicit ``computer.observe`` choice.
    """
    args = dict(args or {})
    max_controls = ctx._parse_max_controls(args)
    cfg = ctx.recovery_config()
    title, controls = await ctx._uia_collect(max_controls)

    if (
        prec.should_skip_reread(args)
        or not cfg.reread_enabled
        or not prec.is_poor_yield(len(controls), cfg)
    ):
        body = await ctx._format_perception_output(
            title, controls, args, tool_name=tool_name,
        )
        await ctx._emit_perception_quality_metrics(
            tool=tool_name,
            window=title,
            step="perceive",
            controls=len(controls),
            include_modal=True,
            include_stack=True,
        )
        return title, ctx._append_focus_warning(body), controls

    # An elevated foreground process is invisible to an un-elevated UIA
    # client. A reread cannot change that, so return the typed cause directly.
    if not controls:
        elev = delev.foreground_elevation_mismatch()
        if elev is not None:
            elev_title = elev.get("title") or title
            err = derr.elevated_target_error(
                window=elev_title,
                pid=elev.get("pid", 0),
                tool=tool_name,
            )
            await ctx._emit_desktop_error(err)
            await ctx._emit_perception_quality_metrics(
                tool=tool_name,
                window=elev_title,
                step="perceive",
                controls=0,
                error=err,
                include_modal=True,
                include_stack=True,
            )
            body = derr.tagged_message(delev.uipi_guidance(elev_title), err)
            return title, ctx._append_focus_warning(body), controls

    low_yield = derr.from_control_count(
        len(controls),
        cfg.min_controls_for_uia,
        tool=tool_name,
        window=title,
    )
    await ctx._emit_desktop_error(low_yield)
    await ctx._emit_perception(
        "perception:low_yield",
        **prec.activity_fields(
            "perception:low_yield",
            controls=len(controls),
            tool=tool_name,
            window=title,
        ),
    )

    await asyncio.sleep(cfg.reread_wait_sec)
    if ctx.session.target_window is not None:
        await ctx._refocus_locked_target()
    title, controls = await ctx._uia_collect(max_controls)

    recovered = not prec.is_poor_yield(len(controls), cfg)
    suffix = prec.reread_note(len(controls), recovered=recovered)
    body = await ctx._format_perception_output(
        title,
        controls,
        args,
        tool_name=tool_name,
        force_full=True,
        suffix=suffix,
    )
    if recovered:
        await ctx._emit_perception(
            "perception:recovered",
            **prec.activity_fields(
                "perception:recovered",
                controls=len(controls),
                tool=tool_name,
                window=title,
                attempt=1,
            ),
        )
    await ctx._emit_perception_quality_metrics(
        tool=tool_name,
        window=title,
        step="perceive",
        controls=len(controls),
        reread=True,
        recovered=recovered,
        error=None if recovered else low_yield,
        include_modal=True,
        include_stack=True,
    )
    return title, ctx._append_focus_warning(body), controls
