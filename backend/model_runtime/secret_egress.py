"""Fail-closed model-envelope secret sanitation for cloud requests."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import re
from typing import Any, Callable, Iterable


class SecretEgressBlocked(RuntimeError):
    """A cloud envelope contains an unresolved likely secret."""
    terminal_reason = "host_preflight_error"
    cause_class = "harness"

    def __init__(self, findings: list[dict[str, str]], *, local_ref: str = "", cloud_ref: str = ""):
        self.findings = tuple(dict(row) for row in findings)
        self.local_ref = str(local_ref or "")
        self.cloud_ref = str(cloud_ref or "")
        codes = sorted({str(row.get("code") or "suspected_secret") for row in findings})
        super().__init__(
            "cloud request blocked by the secret-egress firewall "
            f"({len(findings)} finding(s): {', '.join(codes)}). "
            "Use a local model or remove the suspected secret."
        )


@dataclass(frozen=True)
class EgressProjection:
    payload: dict[str, Any]
    local_ref: str = ""
    cloud_ref: str = ""
    known_replacements: int = 0
    redacted_history_fields: int = 0


_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.I | re.S,
)
_AUTH_HEADER = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*"
    r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"
)
_TOKEN_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{20,}|"
    r"AIza[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9])"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token)\s*[:=]\s*[\"']?([A-Za-z0-9._~+/=-]{8,})"
)
_SIGNED_URL = re.compile(
    r"https?://[^\s\"']+[?&](?:X-Amz-Signature|Signature|sig|token|access_token)="
    r"[A-Za-z0-9%._~+/-]{12,}[^\s\"']*",
    re.I,
)
_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|authorization)$",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"^(?:\{\{VARIANT1_[A-Z0-9_:.-]+\}\}|<[^>]*(?:secret|token|password|key)[^>]*>|"
    r"(?:your|example|dummy|redacted|placeholder)[-_ ].*)$",
    re.I,
)


def _pointer(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _walk_strings(value: Any, parts: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, (*parts, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, (*parts, str(index)))
    elif isinstance(value, str):
        yield parts, value


def _is_provider_reasoning_ciphertext(
    payload: dict[str, Any], parts: tuple[str, ...]
) -> bool:
    """Identify only the opaque continuation blob emitted by Responses APIs.

    The blob is provider-generated ciphertext that must be replayed byte-for-byte
    to the same provider. Pattern scanning or exact-value substitution can both
    corrupt it, and random ciphertext can coincidentally resemble a token prefix.
    Ordinary fields named ``encrypted_content`` remain subject to the firewall.
    """

    if (
        len(parts) != 3
        or parts[0] != "input"
        or parts[2] != "encrypted_content"
    ):
        return False
    try:
        index = int(parts[1])
        item = payload["input"][index]
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return isinstance(item, dict) and str(item.get("type") or "") == "reasoning"


def _replace_at(value: Any, parts: tuple[str, ...], replacement: str) -> None:
    cursor = value
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    if not parts:
        raise ValueError("provider payload root must be an object")
    if isinstance(cursor, list):
        cursor[int(parts[-1])] = replacement
    else:
        cursor[parts[-1]] = replacement


def _fingerprint(value: str) -> str:
    """Stable, non-reversible evidence identifier for a blocked value."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:18]


class SecretEgressFirewall:
    """Sanitize exact managed values and block unresolved high-confidence hits."""

    def __init__(
        self,
        *,
        artifact_store: Any = None,
        known_secret_resolver: Callable[[], Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.known_secret_resolver = known_secret_resolver

    def _known(self) -> list[tuple[str, str]]:
        if not callable(self.known_secret_resolver):
            return []
        try:
            rows = self.known_secret_resolver()
            # None and an empty iterable are legitimate "no managed secrets"
            # results. Resolver/iteration failures are different: continuing
            # would silently disable exact-secret sanitation for this request.
            if rows is None:
                return []
            unique: dict[str, str] = {}
            for label, secret in rows:
                clean = str(secret or "")
                if len(clean) >= 4:
                    unique.setdefault(clean, str(label or "managed"))
        except Exception as exc:
            raise SecretEgressBlocked([{
                "code": "known_secret_resolution_failed",
                "path": "/",
                "fingerprint": _fingerprint(type(exc).__name__),
            }]) from exc
        return sorted(
            ((label, secret) for secret, label in unique.items()),
            key=lambda row: len(row[1]),
            reverse=True,
        )

    @staticmethod
    def _history_replacement(payload: dict[str, Any], parts: tuple[str, ...]) -> str | None:
        """Only completed kernel-call history can be withheld for recovery.

        User instructions, current/unanswered calls, images, schemas, signed
        reasoning and unrelated payload fields retain the existing block.
        """
        note = ('Local historical cell content was withheld because it contains credential material. '
                'Inspect retained Python state and remaining work without printing credentials; do not replay omitted code.')
        try:
            if parts[0] == 'messages':
                rows = payload['messages']; index = int(parts[1]); row = rows[index]
                if row.get('role') == 'assistant' and len(parts) == 6 and parts[2] == 'tool_calls' and parts[4:] == ('function', 'arguments'):
                    call = row['tool_calls'][int(parts[3])]
                    if call.get('function', {}).get('name') != 'ipython' or not call.get('id'):
                        return None
                    if any(r.get('role') == 'tool' and r.get('tool_call_id') == call['id'] for r in rows[index+1:]):
                        return json.dumps({'code': '# '+note})
                if row.get('role') == 'tool' and parts[2:] == ('content',) and row.get('tool_call_id'):
                    if any(c.get('id') == row['tool_call_id'] and c.get('function', {}).get('name') == 'ipython'
                           for prior in rows[:index] for c in prior.get('tool_calls', ())):
                        return note
            if parts[0] == 'input' and len(parts) == 3:
                rows = payload['input']; index = int(parts[1]); row = rows[index]
                call_id = row.get('call_id')
                if not call_id: return None
                if row.get('type') == 'function_call' and row.get('name') == 'ipython' and parts[2] == 'arguments':
                    if any(r.get('type') == 'function_call_output' and r.get('call_id') == call_id for r in rows[index+1:]):
                        return json.dumps({'code': '# '+note})
                if row.get('type') == 'function_call_output' and parts[2] == 'output':
                    if any(r.get('type') == 'function_call' and r.get('name') == 'ipython' and r.get('call_id') == call_id for r in rows[:index]):
                        return note
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            pass
        return None

    @staticmethod
    def _findings(payload: dict[str, Any]) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        patterns = (
            ("private_key", _PRIVATE_KEY),
            ("authorization_header", _AUTH_HEADER),
            ("token_prefix", _TOKEN_PREFIX),
            ("jwt", _JWT),
            ("secret_assignment", _SECRET_ASSIGNMENT),
            ("signed_url", _SIGNED_URL),
        )
        for parts, text in _walk_strings(payload):
            if _is_provider_reasoning_ciphertext(payload, parts):
                continue
            if "{{VARIANT1_SECRET:" in text:
                scrubbed = re.sub(r"\{\{VARIANT1_SECRET:[^}]+\}\}", "", text)
            else:
                scrubbed = text
            inspected = [((), scrubbed)]
            # Provider tool arguments are JSON encoded inside a string. Scan
            # decoded values too; escaped quotes must not hide a literal.
            try:
                encoded = json.loads(scrubbed)
                if isinstance(encoded, (dict, list)):
                    inspected = list(_walk_strings(encoded))
            except (ValueError, TypeError, RecursionError):
                pass
            for nested_parts, candidate in inspected:
                code_text = bool(nested_parts and nested_parts[-1] == 'code' and parts[-1] == 'arguments')
                for code, pattern in patterns:
                    for match in pattern.finditer(candidate):
                        if code == 'secret_assignment' and code_text:
                            prefix = candidate[match.start():match.start(1)].rstrip()
                            if not prefix.endswith(('"', "'")) and not match.group(1).isdigit():
                                # password=local_config.get(...) is a local
                                # expression, not a secret literal assignment.
                                continue
                        findings.append({"code": code, "path": _pointer(parts),
                                         "fingerprint": _fingerprint(match.group(0))})
                if nested_parts and _SECRET_KEY.fullmatch(nested_parts[-1]):
                    clean = candidate.strip().strip("\"'")
                    if len(clean) >= 8 and not _PLACEHOLDER.fullmatch(clean):
                        findings.append({'code':'secret_named_field','path':_pointer(parts),'fingerprint':_fingerprint(clean)})
            if parts and _SECRET_KEY.fullmatch(parts[-1]):
                clean = scrubbed.strip().strip("\"'")
                if len(clean) >= 8 and not _PLACEHOLDER.fullmatch(clean):
                    findings.append({
                        "code": "secret_named_field",
                        "path": _pointer(parts),
                        "fingerprint": _fingerprint(clean),
                    })
        # Stable de-duplication without retaining the matched value.
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, str]] = []
        for row in findings:
            key = (row["code"], row["path"])
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out

    @staticmethod
    def _redact_findings(payload: dict[str, Any], findings: list[dict[str, str]]) -> dict[str, Any]:
        out = copy.deepcopy(payload)
        paths = {row["path"] for row in findings}
        for parts, _text in list(_walk_strings(out)):
            if _pointer(parts) in paths:
                _replace_at(out, parts, "{{VARIANT1_SUSPECTED_SECRET:BLOCKED}}")
        return out

    def project(
        self,
        payload: dict[str, Any],
        *,
        provider: str,
        model: str,
        scope: str = "",
    ) -> EgressProjection:
        if not isinstance(payload, dict):
            raise TypeError("cloud provider payload must be an object")
        local_projection = copy.deepcopy(payload)
        replacement_count = 0
        for label, secret in self._known():
            placeholder = "{{VARIANT1_SECRET:" + re.sub(
                r"[^A-Za-z0-9_.-]", "_", label
            )[:80] + "}}"
            for parts, text in list(_walk_strings(local_projection)):
                if _is_provider_reasoning_ciphertext(local_projection, parts):
                    continue
                if secret in text:
                    _replace_at(
                        local_projection,
                        parts,
                        text.replace(secret, placeholder),
                    )
                    replacement_count += text.count(secret)

        findings = self._findings(local_projection)
        cloud_projection = copy.deepcopy(local_projection)
        redacted_history_fields = 0
        finding_paths = {row['path'] for row in findings}
        for parts, _ in list(_walk_strings(cloud_projection)):
            if _pointer(parts) not in finding_paths: continue
            replacement = self._history_replacement(cloud_projection, parts)
            if replacement is not None:
                _replace_at(cloud_projection, parts, replacement)
                redacted_history_fields += 1
        findings = self._findings(cloud_projection)
        cloud_projection = self._redact_findings(cloud_projection, findings)
        local_ref = ""
        cloud_ref = ""
        if self.artifact_store is not None:
            local_artifact = self.artifact_store.put_json(
                {
                    "schema": "variant1.model-egress.local.v1",
                    "provider": str(provider or ""),
                    "model": str(model or ""),
                    "payload": local_projection,
                },
                kind="model_egress_local_projection",
                scope=scope,
            )
            cloud_artifact = self.artifact_store.put_json(
                {
                    "schema": "variant1.model-egress.cloud.v1",
                    "provider": str(provider or ""),
                    "model": str(model or ""),
                    "payload": cloud_projection,
                },
                kind="model_egress_cloud_projection",
                scope=scope,
            )
            local_ref = local_artifact.ref
            cloud_ref = cloud_artifact.ref
        if findings:
            raise SecretEgressBlocked(
                findings,
                local_ref=local_ref,
                cloud_ref=cloud_ref,
            )
        # JSON roundtrip protects provider adapters from custom mapping values
        # and proves the result is a normal wire object.
        safe = json.loads(json.dumps(cloud_projection, ensure_ascii=False))
        return EgressProjection(
            payload=safe,
            local_ref=local_ref,
            cloud_ref=cloud_ref,
            known_replacements=replacement_count,
            redacted_history_fields=redacted_history_fields,
        )
