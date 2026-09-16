"""Assemble the small amount of context outside provider tool schemas."""

from __future__ import annotations

def tools_prompt_block(
    enabled_now: set,
    tspec: list,
    *,
    skills_catalog: str = "",
    apps_catalog: str = "",
) -> str:
    """Assemble skill/app catalogs that are absent from the one ipython schema.

    Names, descriptions, parameters, and use/avoid metadata already travel in
    the persistent-Python environment prompt and mounted proxies. This helper
    only injects compact skill catalogs for workers that still build a prose
    tools block.
    """
    del enabled_now, tspec
    sections: list[str] = []
    if skills_catalog:
        sections.append(skills_catalog)
    if apps_catalog:
        sections.append(apps_catalog)
    return "\n\n".join(section for section in sections if section)


def tool_lines(
    specs: list,
) -> str:
    """Render a concise description of a newly disclosed provider schema."""
    out = []
    for s in specs:
        ps = ", ".join(
            (k + ("" if v.get("required") else "?"))
            for k, v in (s.get("params") or {}).items()
        )
        out.append(f"- {s['name']}({ps}): {s['description']}")
    return "\n".join(out)
