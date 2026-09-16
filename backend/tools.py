"""VARIANT-1 tool registry and built-in web/browser handlers.

Core types:
  Tool          — name, description, params, async handler
  ToolRegistry  — built-ins and mounted object handlers; specs + run-by-name
  ToolsConfig   — enable/disable and provider settings (config/tools.json)

Batch execution is ``tool_runner`` / ``action_executor``; orchestration is the
native agent engine. Web/browser handlers live in ``tools_web`` and are
re-exported here for registry composition.
"""

import json
import copy
import math
import os
import re
import tempfile
import threading
import time
import types

from core_invariants import canonical_digest
from tool_core import ToolError, ToolExecutionResult

# Browser-like defaults. The old "Mozilla/5.0 (VARIANT-1)" UA is often blocked by
# DDG/Cloudflare; a mainstream Chrome UA + Accept headers fixes most static
# search/fetch failures without needing a real browser session.
# Largest result we feed back to the model per agentic step (borrowed from
# Odysseus's MAX_OUTPUT_CHARS; keeps the context budget sane).
MAX_RESULT_CHARS = 10_000

# External tool names are canonicalized to a provider-compatible identifier.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VISIBILITY_VALUES = frozenset({"provider", "broker_only", "both"})
_EFFECT_CLASS_VALUES = frozenset({
    "pure", "read", "write", "external_side_effect", "interactive",
})
_IDEMPOTENCY_VALUES = frozenset({"none", "caller_key", "naturally_idempotent"})


def _replace_with_retry(source: str, destination: str) -> None:
    """Settle brief Windows sharing/AV races without weakening atomic replace."""

    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= 5:
                raise
            time.sleep(0.02 * (2 ** attempt))


def _stable_revision(prefix: str, value: object) -> str:
    return f"{prefix}.{canonical_digest(value)[:16]}"


def _handler_revision(name: str, handler) -> str:
    code = getattr(handler, "__code__", None)
    if type(code) is not types.CodeType:
        code = None
    module = getattr(handler, "__module__", "")
    if not isinstance(module, str):
        module = type(handler).__module__
    qualname = getattr(handler, "__qualname__", "")
    if not isinstance(qualname, str) or not qualname:
        qualname = type(handler).__qualname__
    basis = {
        "capability": name,
        "module": module,
        "qualname": qualname,
        "bytecode": code.co_code.hex() if code else "",
        "constants": repr(code.co_consts)[:20_000] if code else "",
    }
    return _stable_revision("variant1.handler", basis)


def _default_effect_class(name: str, category: str, annotations: dict) -> str:
    lower = str(name or "").strip().lower()
    if bool((annotations or {}).get("readOnlyHint")):
        return "read"
    if lower in {"read_file", "glob", "grep", "web_search", "browser_read",
                 "browser_screenshot"}:
        return "read"
    if lower in {"apply_patch"}:
        return "write"
    if lower == "ask_user":
        return "interactive"
    # Unknown effects are deliberately classified conservatively.
    return "external_side_effect"

def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v in (0, 1):
        return bool(v)
    text = str(v).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    raise ValueError("not a boolean")


def _schema_type(spec: dict) -> str:
    typ = str((spec or {}).get("type") or "string").strip().lower()
    return {
        "int": "integer", "float": "number", "double": "number",
        "bool": "boolean", "list": "array", "dict": "object",
    }.get(typ, typ)


def _bound(value, spec: dict, key: str, path: str, relation: str) -> None:
    if key not in spec:
        return
    try:
        boundary = spec[key]
        violated = {
            "min": value < boundary,
            "max": value > boundary,
            "exclusive_min": value <= boundary,
            "exclusive_max": value >= boundary,
        }[relation]
    except (TypeError, ValueError):
        raise ToolError(f"{path}: invalid {key} constraint")
    if violated:
        raise ToolError(f"{path}: violates {key}={boundary}")


def _normalize_schema_value(value, spec: dict, path: str, *, coerce: bool):
    """Validate VARIANT-1's provider-neutral JSON-schema subset recursively."""
    spec = spec if isinstance(spec, dict) else {}
    typ = _schema_type(spec)

    try:
        if typ == "any":
            pass
        elif typ == "string":
            if not isinstance(value, str):
                if coerce and isinstance(value, (list, tuple)):
                    separator = str(spec.get("coerce_list_separator") or "")
                    value = separator.join(str(item) for item in value)
                elif coerce and not isinstance(value, (dict, set)):
                    value = str(value)
                else:
                    raise TypeError
            if "minLength" in spec and len(value) < int(spec["minLength"]):
                raise ToolError(f"{path}: must contain at least {spec['minLength']} characters")
            if "maxLength" in spec and len(value) > int(spec["maxLength"]):
                raise ToolError(f"{path}: must contain at most {spec['maxLength']} characters")
            if spec.get("pattern"):
                try:
                    matched = re.search(str(spec["pattern"]), value)
                except re.error as exc:
                    raise ToolError(f"{path}: invalid schema pattern: {exc}") from exc
                if not matched:
                    raise ToolError(f"{path}: does not match the required pattern")
        elif typ == "integer":
            if isinstance(value, bool):
                raise TypeError
            if coerce:
                value = int(value)
            elif not isinstance(value, int):
                raise TypeError
        elif typ == "number":
            if isinstance(value, bool):
                raise TypeError
            if coerce:
                value = float(value)
            elif not isinstance(value, (int, float)):
                raise TypeError
            if not math.isfinite(value):
                raise ToolError(f"{path}: must be a finite number")
        elif typ == "boolean":
            if coerce:
                value = _to_bool(value)
            elif not isinstance(value, bool):
                raise TypeError
        elif typ == "array":
            if not isinstance(value, list):
                if (
                    coerce
                    and spec.get("coerce_singleton_object") is True
                    and isinstance(value, dict)
                ):
                    value = [value]
                else:
                    raise TypeError
            if "minItems" in spec and len(value) < int(spec["minItems"]):
                raise ToolError(f"{path}: needs at least {spec['minItems']} item(s)")
            if "maxItems" in spec and len(value) > int(spec["maxItems"]):
                raise ToolError(f"{path}: allows at most {spec['maxItems']} item(s)")
            item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else None
            if item_spec is not None:
                value = [
                    _normalize_schema_value(item, item_spec, f"{path}[{index}]", coerce=False)
                    for index, item in enumerate(value)
                ]
        elif typ == "object":
            if not isinstance(value, dict):
                raise TypeError
            if "minProperties" in spec and len(value) < int(spec["minProperties"]):
                raise ToolError(f"{path}: needs at least {spec['minProperties']} properties")
            if "maxProperties" in spec and len(value) > int(spec["maxProperties"]):
                raise ToolError(f"{path}: allows at most {spec['maxProperties']} properties")
            properties = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
            required = {
                str(name) for name, child in properties.items()
                if isinstance(child, dict) and child.get("required") is True
            }
            if isinstance(spec.get("required"), list):
                required.update(str(name) for name in spec["required"])
            missing = sorted(
                name for name in required
                if name not in value
            )
            if missing:
                raise ToolError(f"{path}: missing required property/properties: {', '.join(missing)}")
            unknown = sorted(str(name) for name in value if name not in properties)
            if unknown and spec.get("additionalProperties") is False:
                raise ToolError(f"{path}: unknown property/properties: {', '.join(unknown)}")
            normalized = dict(value)
            for name, child in properties.items():
                if name in normalized:
                    if normalized[name] is None:
                        raise ToolError(f"{path}.{name}: null is not allowed")
                    normalized[name] = _normalize_schema_value(
                        normalized[name], child, f"{path}.{name}", coerce=False)
            value = normalized
        else:
            raise ToolError(f"{path}: unsupported schema type {typ!r}")
    except (TypeError, ValueError):
        article = "an" if typ in {"integer", "array", "object"} else "a"
        raise ToolError(f"{path}: must be {article} {typ}")

    enum = spec.get("enum")
    if isinstance(enum, (list, tuple)) and enum and value not in enum:
        choices = ", ".join(repr(item) for item in enum)
        raise ToolError(f"{path}: must be one of {choices}")
    if typ in {"integer", "number"}:
        _bound(value, spec, "minimum", path, "min")
        _bound(value, spec, "maximum", path, "max")
        _bound(value, spec, "exclusiveMinimum", path, "exclusive_min")
        _bound(value, spec, "exclusiveMaximum", path, "exclusive_max")
    return value


def validate_arguments(
    name: str,
    args: dict,
    params: dict | None,
) -> dict:
    """Validate one built-in argument object against a VARIANT-1 param schema.

    Mounted Python objects use this same validator after selecting a method so
    method-specific bounds and enums are enforced instead of only the wider
    dispatcher transport schema.
    """

    if not isinstance(args, dict):
        raise ToolError(f"{name}: arguments must be an object")
    declared_params = params or {}
    declared = set(declared_params)
    unknown = sorted(str(key) for key in args if key not in declared)
    if unknown:
        raise ToolError(
            f"{name}: unknown argument(s): {', '.join(unknown)}"
        )
    out = dict(args)
    for pname, spec in declared_params.items():
        typ = _schema_type(spec or {})
        required = bool((spec or {}).get("required"))
        if pname not in out:
            if required:
                raise ToolError(
                    f"{name} needs '{pname}'" + (f" ({typ})" if typ else "")
                )
            continue
        val = out[pname]
        if val is None:
            if (
                not required
                and "default" in (spec or {})
                and (spec or {}).get("default") is None
            ):
                continue
            raise ToolError(f"{name}.{pname}: null is not allowed")
        out[pname] = _normalize_schema_value(
            val, spec or {}, f"{name}.{pname}", coerce=True
        )
    return out


class Tool:
    def __init__(self, name, description, handler, category="general",
                 params=None, when="", avoid="", prefer_over=None, hidden=False,
                 annotations=None, *, capability_id="", schema_revision="",
                 handler_revision="", visibility="both", effect_class="",
                 parallel_safe=None, idempotency="", touches_desktop=None,
                 may_return_secrets=None, default_deadline_ms=0,
                 result_projection="typed-content-v1", object_methods=None,
                 control_admission=None):
        self.name = name
        self.description = description
        self.handler = handler          # async (args: dict) -> str
        # Host-only validation for lifecycle controls that must reach an
        # in-flight operation. Never inferred from model-supplied metadata.
        self.control_admission = control_admission
        self.category = category
        self.params = params or {}      # {param: {type, required, desc}}
        # Protocol / host annotations (e.g. MCP readOnlyHint). Not prompt text.
        self.annotations = dict(annotations or {}) if annotations else {}
        # Discovery metadata (Phase A): when/avoid guide tool choice; prefer_over
        # drops sibling tools from model-facing lists when both are enabled;
        # hidden tools stay callable (aliases/compat) but are never disclosed.
        # Initial visibility is owned centrally by tool_discovery; individual
        # tools cannot opt themselves into the fixed provider front door.
        self.when = str(when or "").strip()
        self.avoid = str(avoid or "").strip()
        self.prefer_over = tuple(prefer_over or ())
        self.hidden = bool(hidden)
        self.capability_id = str(capability_id or name).strip()
        self.schema_revision = str(schema_revision or _stable_revision(
            "variant1.schema",
            {"capability": self.capability_id, "params": self.params},
        ))
        self.handler_revision = str(
            handler_revision or _handler_revision(self.capability_id, handler)
        )
        self.visibility = str(visibility or "both").strip().lower()
        if self.visibility not in _VISIBILITY_VALUES:
            raise ValueError(f"invalid tool visibility: {self.visibility!r}")
        self.effect_class = str(
            effect_class
            or _default_effect_class(self.name, self.category, self.annotations)
        ).strip().lower()
        if self.effect_class not in _EFFECT_CLASS_VALUES:
            raise ValueError(f"invalid tool effect class: {self.effect_class!r}")
        self.touches_desktop = (
            None if touches_desktop is None else bool(touches_desktop)
        )
        if parallel_safe is None:
            parallel_safe = self.effect_class in {"pure", "read"}
        self.parallel_safe = bool(parallel_safe) and self.effect_class in {"pure", "read"}
        default_idempotency = (
            "naturally_idempotent"
            if self.effect_class in {"pure", "read"}
            else "none"
        )
        self.idempotency = str(idempotency or default_idempotency).strip().lower()
        if self.idempotency not in _IDEMPOTENCY_VALUES:
            raise ValueError(f"invalid tool idempotency: {self.idempotency!r}")
        self.may_return_secrets = bool(
            self.effect_class == "read"
            if may_return_secrets is None
            else may_return_secrets
        )
        try:
            self.default_deadline_ms = max(0, int(default_deadline_ms or 0))
        except (TypeError, ValueError):
            raise ValueError("default_deadline_ms must be a non-negative integer")
        self.result_projection = str(result_projection or "typed-content-v1")
        # A unified mounted-object seed may advertise locally projected method
        # signatures while retaining one broker handler and capability ID.
        # This metadata is consumed only by the IPython catalog; it is never a
        # provider-visible tool list.
        self.object_methods = tuple(
            copy.deepcopy(row)
            for row in (object_methods or ())
            if isinstance(row, dict)
        )

    def spec(self) -> dict:
        out = {"name": self.name, "description": self.description,
               "category": self.category, "params": self.params}
        if self.when:
            out["when"] = self.when
        if self.avoid:
            out["avoid"] = self.avoid
        if self.prefer_over:
            out["prefer_over"] = list(self.prefer_over)
        if self.hidden:
            out["hidden"] = True
        if self.annotations:
            out["annotations"] = dict(self.annotations)
        return out

    def validate_args(self, args: dict) -> dict:
        """Phase C: check required params and coerce declared types before the
        handler runs, so a malformed call fails with a clear message instead of a
        confusing handler traceback. Only applies to built-ins (our param schema);
        MCP tools (category 'mcp:*') use the server's own schema and are skipped."""
        if str(self.category).startswith("mcp:"):
            if not isinstance(args, dict):
                raise ToolError(f"{self.name}: arguments must be an object")
            return dict(args)
        return validate_arguments(self.name, args, self.params)

    async def run(self, args: dict) -> str | ToolExecutionResult:
        return await self.handler(self.validate_args(args))

    def broker_metadata(self) -> dict:
        """Versioned host metadata; intentionally absent from provider specs."""
        return {
            "capability_id": self.capability_id,
            "schema_revision": self.schema_revision,
            "handler_revision": self.handler_revision,
            "visibility": self.visibility,
            "effect_class": self.effect_class,
            "parallel_safe": self.parallel_safe,
            "idempotency": self.idempotency,
            "touches_desktop": self.touches_desktop,
            "may_return_secrets": self.may_return_secrets,
            "default_deadline_ms": self.default_deadline_ms,
            "result_projection": self.result_projection,
        }

    def broker_metadata_for_args(self, args: dict | None) -> dict:
        """Return operation-specific broker policy for a mounted object call.

        One Python object intentionally owns one capability identity.  Its
        methods can still retain their real read/write classification and
        scheduling semantics after the dispatcher operation is known.
        """

        if not self.object_methods or not isinstance(args, dict):
            return {}
        operation = str(args.get("operation") or "")
        method = next(
            (row for row in self.object_methods
             if str(row.get("name") or "") == operation),
            None,
        )
        if method is None:
            return {}
        effect = str(method.get("effect_class") or self.effect_class).strip().lower()
        if effect not in _EFFECT_CLASS_VALUES:
            raise ValueError(f"invalid object method effect class: {effect!r}")
        parallel = bool(
            method.get("parallel_safe", effect in {"pure", "read"})
        ) and effect in {"pure", "read"}
        idempotency = str(
            method.get("idempotency")
            or ("naturally_idempotent" if effect in {"pure", "read"}
                else self.idempotency)
        ).strip().lower()
        if idempotency not in _IDEMPOTENCY_VALUES:
            raise ValueError(
                f"invalid object method idempotency: {idempotency!r}"
            )
        return {
            "effect_class": effect,
            "parallel_safe": parallel,
            "idempotency": idempotency,
            "touches_desktop": method.get(
                "touches_desktop", self.touches_desktop
            ),
            "may_return_secrets": method.get(
                "may_return_secrets", self.may_return_secrets
            ),
            "default_deadline_ms": method.get(
                "default_deadline_ms", self.default_deadline_ms
            ),
        }


class ToolRegistry:
    def __init__(self):
        self._tools = {}            # name -> Tool
        self._capabilities = {}     # capability_id -> Tool
        self.mcp_servers = {}       # name -> info (populated by the MCP client later)

    def register(self, tool: Tool):
        if not _TOOL_NAME_RE.fullmatch(str(tool.name or "")):
            raise ValueError(
                f"invalid tool name {tool.name!r}; use 1-64 letters, numbers, '_' or '-'"
            )
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        if tool.capability_id in self._capabilities:
            raise ValueError(f"duplicate capability id: {tool.capability_id}")
        self._tools[tool.name] = tool
        self._capabilities[tool.capability_id] = tool

    def remove(self, name: str):
        tool = self._tools.pop(name, None)
        if tool is not None:
            self._capabilities.pop(tool.capability_id, None)

    def get(self, name: str):
        return self._tools.get(name)

    def get_capability(self, capability_id: str):
        return self._capabilities.get(str(capability_id or ""))

    def all(self):
        return list(self._tools.values())

    def specs(self, enabled: set = None, include_hidden: bool = False) -> list:
        out = []
        for t in self._tools.values():
            if enabled is not None and t.name not in enabled:
                continue
            if t.hidden and not include_hidden:
                continue
            out.append(t.spec())
        return disclose_specs(out) if not include_hidden else out


def disclose_specs(specs: list) -> list:
    """Model-facing filter: drop hidden tools and losers of prefer_over.

    When both a preferred tool and a sibling it replaces are present, only the
    preferred tool is shown so the model is not asked to choose between
    complete overlaps.
    """
    visible = [s for s in (specs or []) if isinstance(s, dict) and s.get("name")
               and not s.get("hidden")]
    names = {s["name"] for s in visible}
    losers: set = set()
    for s in visible:
        for other in s.get("prefer_over") or ():
            if other in names:
                losers.add(other)
    if not losers:
        return visible
    return [s for s in visible if s["name"] not in losers]


# --------------------------------------------------------------------------
# Persisted config
# --------------------------------------------------------------------------
class ToolsConfig:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.data = {}
        self._persisted_data = copy.deepcopy(self.data)
        self.load()

    @staticmethod
    def _normalized(value) -> dict:
        raw = dict(value) if isinstance(value, dict) else {}
        # Per-handler enable flags predated ASTB and are deliberately discarded.
        # The immutable catalog/mount grant is the model admission authority.
        raw.pop("enabled", None)
        for section in ("desktop", "web_search"):
            if not isinstance(raw.get(section), dict):
                raw[section] = {}
        raw["desktop"].pop("enabled", None)
        web = raw["web_search"]
        selected = str(web.get("provider") or "variant1").strip().lower()
        web["provider"] = {"brave": "brave-free", "ddg": "ddgs"}.get(
            selected, selected)
        for block in web.values():
            if isinstance(block, dict):
                for secret_key in ("api_key", "token", "secret"):
                    block.pop(secret_key, None)
        return raw

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.data = self._normalized(d)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[tools] config load failed ({e}); defaults", flush=True)
        self.data = self._normalized(self.data)
        self._persisted_data = copy.deepcopy(self.data)

    def _persist(self, value: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        persisted = copy.deepcopy(self._normalized(value))
        fd, temp_path = tempfile.mkstemp(
            prefix=".variant1-tools-", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(persisted, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp_path, self.path)
            temp_path = ""
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass

    def save(self):
        with self._lock:
            try:
                self._persist(self.data)
            except Exception:
                self.data = copy.deepcopy(self._persisted_data)
                raise
            self.data = self._normalized(self.data)
            self._persisted_data = copy.deepcopy(self.data)

    def _commit(self, mutate) -> None:
        with self._lock:
            staged = copy.deepcopy(self.data)
            mutate(staged)
            staged = self._normalized(staged)
            self._persist(staged)
            self.data = staged
            self._persisted_data = copy.deepcopy(staged)

    # -- web_search: default VARIANT-1 Search; optional SearXNG / API backends ----
    @property
    def web_search(self) -> dict:
        cfg = self.data.setdefault("web_search", {})
        if not isinstance(cfg, dict):
            cfg = {}
            self.data["web_search"] = cfg
        cfg.setdefault("provider", "variant1")
        miy = cfg.setdefault("variant1", {})
        if not isinstance(miy, dict):
            miy = {}
            cfg["variant1"] = miy
        miy.setdefault("engines", ["ddg", "bing"])
        searx = cfg.setdefault("searxng", {})
        if not isinstance(searx, dict):
            searx = {}
            cfg["searxng"] = searx
        # Managed Docker sidecar defaults (see web_search.searxng.SearxngServer).
        # Autostart off by default — free path is in-process VARIANT-1 Search.
        searx.setdefault("base_url", "http://127.0.0.1:8888")
        searx.setdefault("autostart", False)
        searx.setdefault("managed", True)
        searx.setdefault("host", "127.0.0.1")
        searx.setdefault("port", 8888)
        searx.setdefault("docker_image", "docker.io/searxng/searxng:latest")
        searx.setdefault("container_name", "variant1-searxng")
        return cfg

    def set_web_search_config(self, updates: dict):
        if not isinstance(updates, dict):
            return
        def mutate(staged):
            cfg = staged.setdefault("web_search", {})
            if not isinstance(cfg, dict):
                cfg = {}
                staged["web_search"] = cfg
            if "provider" in updates:
                cfg["provider"] = str(
                    updates.get("provider") or "variant1"
                ).strip().lower()
            for key, value in updates.items():
                if key == "provider" or not isinstance(value, dict):
                    continue
                if isinstance(value, dict):
                    block = cfg.setdefault(key, {})
                    if not isinstance(block, dict):
                        block = {}
                        cfg[key] = block
                    for k, v in value.items():
                        sk = str(k)
                        if sk in {"api_key", "token", "secret"}:
                            continue
                        if sk in ("autostart", "managed") and not isinstance(v, bool):
                            block[sk] = str(v).strip().lower() in (
                                "1", "true", "yes", "on"
                            )
                        elif sk == "port":
                            try:
                                block[sk] = int(v)
                            except (TypeError, ValueError):
                                block[sk] = v
                        else:
                            block[sk] = v
        self._commit(mutate)

# --------------------------------------------------------------------------
# Web and browser handlers
# --------------------------------------------------------------------------
# Web/browser handlers live in tools_web (re-exported below).
from tools_web import (  # noqa: E402 — after core types
    DEFAULT_WEB_HEADERS,
    DEFAULT_WEB_UA,
    MAX_FETCH_BYTES,
    MAX_REDIRECTS,
    WEB_HTTP_ATTEMPTS,
    _untrusted_web_content,
    browser_click,
    browser_fill,
    browser_navigate,
    browser_read,
    browser_screenshot,
    ddg_static_search,
    html_to_text,
    normalize_result_url,
    web_search,
)

BROWSER_OBJECT_METHODS = (
    {
        "name": "navigate",
        "description": (
            "Navigate the current browser page and return its typed observation and "
            "durable session/page handles. Omit kind to follow this chat's browser "
            "selection, which defaults to the built-in browser. An explicit user "
            "browser/tab request takes precedence. kind='embedded' selects VARIANT-1's in-app browser and "
            "kind='managed' selects an isolated managed browser. "
            "Read result.surface/browser_kind to identify the actual surface; use result.page "
            "and result.session to continue in that browser."
        ),
        "effect_class": "external_side_effect",
        "parallel_safe": False,
        "params": {
            "url": {"type": "string", "required": True, "desc": "http(s) URL"},
            "max_chars": {"type": "integer", "required": False},
            "session_id": {"type": "string", "required": False,
                           "desc": "Select an exact existing session. Conflicting kind/profile/headless options are rejected."},
            "target_id": {"type": "string", "required": False,
                          "desc": "Select an existing page by its returned target ID."},
            "kind": {
                "type": "string",
                "required": False,
                "enum": ["embedded", "managed"],
                "desc": "Omit to follow this chat's browser selection (built-in by default).",
            },
            "profile_id": {"type": "string", "required": False},
            "profile_name": {"type": "string", "required": False},
            "persistent_profile": {"type": "boolean", "required": False},
            "headless": {"type": "boolean", "required": False, "desc": "Managed-browser creation constraint. The built-in browser is always visible."},
            "include_html": {"type": "boolean", "required": False},
            "include_screenshot": {"type": "boolean", "required": False},
            "max_elements": {"type": "integer", "required": False},
            "metadata": {"type": "object", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "read",
        "description": (
            "Read the current page and return text plus stable element references."
        ),
        "effect_class": "read",
        "parallel_safe": False,
        "params": {
            "max_chars": {"type": "integer", "required": False},
            "max_elements": {"type": "integer", "required": False},
            "include_html": {"type": "boolean", "required": False},
            "include_screenshot": {"type": "boolean", "required": False},
            "session_id": {"type": "string", "required": False},
            "target_id": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "screenshot",
        "description": "Capture the current browser page as a model-visible image. Use result.image.save(path) for the original image, or result.image.read_bytes(); image.size is a byte count.",
        "effect_class": "read",
        "parallel_safe": False,
        "params": {
            "session_id": {"type": "string", "required": False},
            "target_id": {"type": "string", "required": False},
        },
    },
    {
        "name": "click",
        "description": "Click one element reference returned by browser.read.",
        "effect_class": "external_side_effect",
        "parallel_safe": False,
        "params": {
            "target": {"type": "any", "required": True},
            "position": {"type": "object", "required": False},
            "force": {"type": "boolean", "required": False},
            "timeout_ms": {"type": "integer", "required": False},
            "timeout": {"type": "integer", "required": False},
            "include_screenshot": {"type": "boolean", "required": False},
            "session_id": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "fill",
        "description": "Fill one text-field reference returned by browser.read.",
        "effect_class": "external_side_effect",
        "parallel_safe": False,
        "params": {
            "target": {"type": "any", "required": True},
            "text": {"type": "string", "required": True},
            "include_screenshot": {"type": "boolean", "required": False},
            "session_id": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
)

def register_builtins(registry: ToolRegistry, *, web_search_handler):
    if not callable(web_search_handler):
        raise TypeError("web_search_handler is required")
    registry.register(Tool(
        "web_search",
        "Search the web with query, or read one known page with url. A complete "
        "http(s) URL in query remains supported for compatibility. "
        "Searches return five results; URL reads return up to 8,000 readable characters. "
        "Use browser.navigate instead for JS-rendered, authenticated, or interactive pages.",
        web_search_handler, category="web",
        params={
            "query": {
                "type": "string", "required": False,
                "desc": "search terms; may also be one complete http(s) URL",
            },
            "url": {
                "type": "string", "required": False,
                "desc": "one complete http(s) URL to read; do not combine with query",
            },
        },
        when="finding web sources or reading one known webpage",
        avoid="interactive or authenticated browser work (use browser)"))
    from object_api import dispatch_object, register_object_tool

    async def browser(args):
        return await dispatch_object(
            BROWSER_OBJECT_METHODS,
            args,
            api_name="browser",
            handlers={
                "navigate": browser_navigate,
                "read": browser_read,
                "screenshot": browser_screenshot,
                "click": browser_click,
                "fill": browser_fill,
            },
        )

    register_object_tool(
        registry,
        name="browser",
        description=(
            "Preferred capability for web tasks, using the built-in browser by default. "
            "Honor the user's browser choice and use another browser when the task or recovery requires it. "
            "Navigate, read, screenshot, click, and fill. "
            "Returned handles keep compact page, history, trace, and element continuation."
        ),
        methods=BROWSER_OBJECT_METHODS,
        handler=browser,
        category="browser",
        schema_revision="variant1.browser-seed.v2",
        handler_revision="variant1.browser-seed-handler.v2",
        effect_class="external_side_effect",
        may_return_secrets=True,
    )


# --------------------------------------------------------------------------
# Optional human-readable host-capability inventory.
# --------------------------------------------------------------------------
# Above this many disclosed tools, the optional human-readable inventory clips
# descriptions to their first sentence.
TOOLS_COMPACT_MIN = 18
_TOOLS_FULL_DESC = frozenset({
    "run_command", "ask_user", "glob", "grep",
    "web_search", "apply_patch", "read_file",
})


def _compact_desc(desc: str) -> str:
    d = " ".join(str(desc or "").split())
    first = re.split(r"(?<=[.!?])\s+", d, maxsplit=1)[0]
    if len(first) > 180:
        first = first[:177].rsplit(" ", 1)[0] + "…"
    return first


def _tool_line(spec: dict, *, compact: bool) -> str:
    """One model-facing tool line, including when/avoid disambiguation."""
    ps = ", ".join(
        (k + ("" if v.get("required") else "?"))
        for k, v in (spec.get("params") or {}).items())
    desc = spec.get("description") or ""
    keep_full = (spec.get("name") in _TOOLS_FULL_DESC
                 or bool(spec.get("when") or spec.get("avoid") or spec.get("prefer_over")))
    if compact and not keep_full:
        desc = _compact_desc(desc)
    bits = [f"- {spec['name']}({ps}): {desc}"]
    when = (spec.get("when") or "").strip()
    avoid = (spec.get("avoid") or "").strip()
    if when:
        bits.append(f"  use when: {when}")
    if avoid:
        bits.append(f"  do not use when: {avoid}")
    return "\n".join(bits)


def tools_prompt(specs: list, *, native: bool = True) -> str:
    """Render a human-readable inventory without defining another protocol.

    Runtime prompts no longer include this list because the same information is
    already present in provider-native schemas. The helper remains useful in
    settings, diagnostics, and exported datasets.
    """
    if not specs:
        return ""
    specs = disclose_specs(specs)
    compact = len(specs) > TOOLS_COMPACT_MIN
    lines = ["You can use tools to help the user. Available tools:"]
    for s in specs:
        lines.append(_tool_line(s, compact=compact))
    return "\n".join(lines)
