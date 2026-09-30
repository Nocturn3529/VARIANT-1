"""
VARIANT-1 observability doctor — a one-shot setup-health linter.

`run_checks(snap)` takes a plain snapshot dict (built by server.py from the live
config/state) and returns a list of findings. Pure and side-effect-free so it's
easy to unit-test. Each finding: {level, title, detail, fix}.

Levels, worst-first: "critical" > "warn" > "info" > "ok".
"""

LEVELS = ("critical", "warn", "info", "ok")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


def _worst(findings) -> str:
    best = "ok"
    for f in findings:
        if _RANK.get(f["level"], 99) < _RANK[best]:
            best = f["level"]
    return best


def run_checks(snap: dict) -> dict:
    """Return {summary, findings}. `snap` keys (all optional, defaulted):
      mode, provider, has_key, key_tokens(dict provider->token),
      mcp_servers(dict name->{status}), engine_ready, model_name, is_windows,
      and vision(dict).
    """
    f = []

    def add(level, title, detail, fix=""):
        f.append({"level": level, "title": title, "detail": detail, "fix": fix})

    mode = snap.get("mode", "local")
    provider = snap.get("provider", "")
    is_win = snap.get("is_windows", True)

    # --- API key hygiene ---------------------------------------------------
    key_tokens = snap.get("key_tokens", {}) or {}
    credential_fix = "Re-enter the credential in Settings → API keys to encrypt it for this platform."
    for prov, tok in key_tokens.items():
        if not tok:
            continue
        if tok.startswith("dpapi:"):
            if not is_win:
                add("critical", f"{prov} key uses Windows-only encryption",
                    "DPAPI credentials require the Windows account that saved them.", credential_fix)
        elif tok.startswith("fernet:"):
            if is_win:
                add("critical", f"{prov} key uses Unix/macOS encryption",
                    "Fernet credentials are supported on Unix/macOS; this Windows store uses DPAPI.", credential_fix)
        elif tok.startswith("plain:"):
            add("critical" if is_win else "warn", f"{prov} key stored as plaintext (dev)",
                "Legacy plaintext credentials are disabled on Windows." if is_win
                else "This is a non-encrypted legacy development credential.", credential_fix)
        else:
            add("critical", f"{prov} key is not encrypted",
                "The stored credential has no recognized encryption format; it may be plaintext.", credential_fix)

    # --- cloud / engine readiness -----------------------------------------
    if mode == "cloud" and not snap.get("has_key"):
        add("warn", f"Cloud mode selected but no key for {provider or 'the provider'}",
            "Chat will fail until a key is saved for the active provider.",
            "Add or re-enter the credential in Settings → API keys, then test the connection.")
    if mode == "local" and not snap.get("engine_ready"):
        add("warn", "Local engine isn't ready",
            f"No model is loaded ({snap.get('model_name') or 'none selected'}).",
            "Choose or download a model in Settings → Local models.")

    # --- MCP ---------------------------------------------------------------
    mcp = snap.get("mcp_servers", {}) or {}
    mcp_errored = [n for n, s in mcp.items() if (s or {}).get("status") == "error"]
    for n in mcp_errored:
        add("warn", f"MCP server '{n}' is in an error state",
            "Its tools won't be available.",
            "Reconnect or correct the server configuration in Settings → Plugins.")

    # --- vision route ------------------------------------------------------
    vision = snap.get("vision", {}) or {}
    if vision.get("route") == "cloud":
        add("info", "Cloud vision is active",
            "Requested screenshots are sent to the selected model provider.",
            "Select a local multimodal model when local image processing is required.")

    if not f:
        add("ok", "Looks healthy", "No risky or broken settings found.")

    # sort worst-first for display
    f.sort(key=lambda x: _RANK.get(x["level"], 99))
    return {"summary": _worst(f), "findings": f}
