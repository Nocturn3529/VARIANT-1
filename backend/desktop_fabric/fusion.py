"""Deterministic UIA/OCR/VLM fusion with UIA-authoritative semantics."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Iterable, Mapping

from .models import DesktopElement, stable_digest


def _bounds(value: Any) -> tuple[int, int, int, int] | None:
    if not value or not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = tuple(int(v) for v in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def _words(value: str) -> set[str]:
    return set(re.findall(r"[\w]+", str(value or "").casefold()))


def _iou(a: tuple[int, int, int, int] | None, b: tuple[int, int, int, int] | None) -> float:
    if not a or not b:
        return 0.0
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if not inter:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / float(area_a + area_b - inter)


def _contains(container: tuple[int, int, int, int] | None,
              item: tuple[int, int, int, int] | None) -> bool:
    if not container or not item:
        return False
    cx = (item[0] + item[2]) / 2.0
    cy = (item[1] + item[3]) / 2.0
    return container[0] <= cx <= container[2] and container[1] <= cy <= container[3]


def _element_from_raw(
    raw: Mapping[str, Any], *, observation_id: str, window_id: str,
    window_generation: int, element_generation: int, default_provenance: str,
    occurrence: int,
) -> DesktopElement:
    role = str(raw.get("role") or ("Detected" if default_provenance == "vlm" else "Text"))
    name = str(raw.get("name") or raw.get("label") or "")
    text = str(raw.get("text") or "")
    value = str(raw.get("value") or "")
    automation_id = str(raw.get("automation_id") or raw.get("aid") or "")
    runtime_id = tuple(int(v) for v in raw.get("runtime_id") or ())
    semantic_path = str(raw.get("semantic_path") or raw.get("key") or "")
    backend_key = str(raw.get("backend_key") or raw.get("id") or raw.get("key") or "")
    bounds = _bounds(raw.get("bounds"))
    identity = (
        ("runtime", list(runtime_id)) if runtime_id else
        ("automation", automation_id, semantic_path) if automation_id else
        ("path", semantic_path) if semantic_path else
        ("semantic", role, name, bounds, occurrence, default_provenance)
    )
    element_ref = "el_" + stable_digest(window_id, window_generation, identity)[:28]
    provenance_raw = raw.get("provenance") or (default_provenance,)
    if isinstance(provenance_raw, str):
        provenance_raw = (provenance_raw,)
    provenance = tuple(dict.fromkeys(str(v) for v in provenance_raw if v))
    state_value = raw.get("states")
    if not isinstance(state_value, Mapping):
        state_value = {
            "state": str(raw.get("state") or ""),
            "offscreen": bool(raw.get("offscreen", False)),
        }
    patterns_raw = raw.get("patterns") or ()
    if isinstance(patterns_raw, str):
        patterns_raw = (patterns_raw,)
    fingerprint = stable_digest(
        identity, role, name, text, value, state_value, bounds, patterns_raw,
    )
    return DesktopElement(
        element_ref=element_ref, observation_id=observation_id,
        window_id=window_id, window_generation=window_generation,
        element_generation=element_generation, role=role, name=name,
        text=text, value=value, states=dict(state_value),
        patterns=tuple(sorted(set(str(v) for v in patterns_raw if v))),
        bounds=bounds, runtime_id=runtime_id, automation_id=automation_id,
        semantic_path=semantic_path,
        confidence=max(0.0, min(1.0, float(raw.get("confidence", 1.0)))),
        actionable=bool(raw.get("actionable", default_provenance == "vlm")),
        provenance=provenance or (default_provenance,),
        fingerprint=fingerprint, backend_key=backend_key,
    )


def normalize_elements(
    records: Iterable[Mapping[str, Any]], *, observation_id: str, window_id: str,
    window_generation: int, element_generation: int,
    default_provenance: str,
) -> list[DesktopElement]:
    occurrences: dict[tuple[str, str, str], int] = {}
    result: list[DesktopElement] = []
    for raw in records:
        key = (str(raw.get("role") or ""), str(raw.get("name") or raw.get("label") or ""),
               str(raw.get("key") or ""))
        occurrences[key] = occurrences.get(key, 0) + 1
        result.append(_element_from_raw(
            raw, observation_id=observation_id, window_id=window_id,
            window_generation=window_generation,
            element_generation=element_generation,
            default_provenance=default_provenance, occurrence=occurrences[key],
        ))
    return result


def fuse_elements(
    *, uia: Iterable[Mapping[str, Any]], visual: Iterable[Mapping[str, Any]] = (),
    ocr: Iterable[Mapping[str, Any]] = (), observation_id: str, window_id: str,
    window_generation: int, element_generation: int,
) -> tuple[DesktopElement, ...]:
    """Fuse sources while never promoting visual guesses to UIA semantics."""
    semantic = normalize_elements(
        uia, observation_id=observation_id, window_id=window_id,
        window_generation=window_generation, element_generation=element_generation,
        default_provenance="uia",
    )
    supplementary: list[DesktopElement] = []
    supplementary.extend(normalize_elements(
        ocr, observation_id=observation_id, window_id=window_id,
        window_generation=window_generation, element_generation=element_generation,
        default_provenance="ocr",
    ))
    supplementary.extend(normalize_elements(
        visual, observation_id=observation_id, window_id=window_id,
        window_generation=window_generation, element_generation=element_generation,
        default_provenance="vlm",
    ))

    unmatched: list[DesktopElement] = []
    for extra in supplementary:
        extra_words = _words(" ".join((extra.name, extra.text, extra.value)))
        candidates: list[tuple[float, int]] = []
        for index, base in enumerate(semantic):
            overlap = _iou(base.bounds, extra.bounds)
            containment = _contains(base.bounds, extra.bounds) or _contains(extra.bounds, base.bounds)
            base_words = _words(" ".join((base.name, base.text, base.value)))
            lexical = (len(extra_words & base_words) / max(1, len(extra_words | base_words)))
            score = overlap * 0.7 + lexical * 0.3
            if overlap >= 0.2 or (containment and lexical >= 0.25) or lexical >= 0.8:
                candidates.append((score, index))
        if not candidates:
            unmatched.append(extra)
            continue
        candidates.sort(reverse=True)
        best_score, best_index = candidates[0]
        if len(candidates) > 1 and abs(best_score - candidates[1][0]) < 0.05:
            # Ambiguous fusion is safer as an explicit visual node than as
            # incorrect semantic evidence on one of two UIA nodes.
            unmatched.append(extra)
            continue
        base = semantic[best_index]
        provenance = tuple(dict.fromkeys(base.provenance + extra.provenance))
        states = dict(base.states)
        states["fused_confidence"] = round(extra.confidence, 3)
        semantic[best_index] = replace(
            base, provenance=provenance, states=states,
            confidence=max(base.confidence, extra.confidence),
            fingerprint=stable_digest(base.fingerprint, extra.fingerprint, provenance),
        )
    return tuple(semantic + unmatched)


def observation_fingerprint(elements: Iterable[DesktopElement], *, window_generation: int) -> str:
    items = list(elements)
    semantic = [item for item in items if "uia" in item.provenance]
    selected = semantic or items
    return stable_digest(
        window_generation,
        [(item.element_ref, item.role, item.name, item.text, item.value,
          {key: value for key, value in item.states.items()
           if key != "fused_confidence"}, item.bounds,
          "uia" if "uia" in item.provenance else item.provenance)
         for item in selected],
    )


__all__ = ["fuse_elements", "normalize_elements", "observation_fingerprint"]
