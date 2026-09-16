"""Shared recovery contract for replaceable JSON documents."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import uuid


class DocumentLoadError(OSError):
    """Existing data could not be read or preserved; writes must remain blocked."""


def load_document(path, default, *, valid=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return deepcopy(default)
    except (json.JSONDecodeError, UnicodeError) as exc:
        problem = exc
    except OSError as exc:
        raise DocumentLoadError(f"Cannot read durable document {path}: {exc}") from exc
    else:
        matches = isinstance(value, type(default))
        if matches and isinstance(default, dict):
            matches = all(
                key not in value or not isinstance(expected, (dict, list))
                or isinstance(value[key], type(expected))
                for key, expected in default.items()
            )
        if matches and (valid is None or valid(value)):
            return value
        problem = ValueError("invalid document shape")
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    preserved = f"{os.fspath(path)}.invalid-{suffix}-{uuid.uuid4().hex[:8]}"
    try:
        os.replace(path, preserved)
    except OSError as exc:
        raise DocumentLoadError(
            f"Cannot preserve invalid durable document {path}; refusing empty recovery"
        ) from exc
    print(f"[document] preserved invalid data at {preserved}: {problem}", flush=True)
    return deepcopy(default)
