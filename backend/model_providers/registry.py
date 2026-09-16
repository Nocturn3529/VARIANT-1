"""Lazy provider discovery for bundled and user-installed profiles."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Iterable

from .base import ProviderProfile
from .builtin import builtin_profiles


class ProviderRegistry:
    """Name/alias registry with last-writer-wins plugin overrides."""

    def __init__(self, *, plugin_dirs: Iterable[str | os.PathLike] = ()):
        self._profiles: dict[str, ProviderProfile] = {}
        self._aliases: dict[str, str] = {}
        self._origins: dict[str, dict] = {}
        self._discovering_origin: dict | None = None
        self.errors: list[dict] = []
        for profile in builtin_profiles():
            self.register(profile, origin={"kind": "builtin"})
        for directory in plugin_dirs:
            self.discover(Path(directory))

    @staticmethod
    def normalize(name: str) -> str:
        return str(name or "").strip().lower().replace("_", "-")

    def register(self, profile: ProviderProfile, *, origin: dict | None = None) -> None:
        canonical = self.normalize(profile.name)
        if not canonical:
            raise ValueError("provider profile needs a name")
        # Overrides replace the old alias contract too; stale aliases must
        # not keep routing to a profile that no longer advertises them.
        self._aliases = {
            alias: target for alias, target in self._aliases.items()
            if target != canonical
        }
        self._profiles[canonical] = profile
        self._origins[canonical] = dict(origin or self._discovering_origin or {"kind": "builtin"})
        for alias in profile.aliases:
            self._aliases[self.normalize(alias)] = canonical

    def canonical_name(self, name: str) -> str:
        value = self.normalize(name)
        return self._aliases.get(value, value)

    def get(self, name: str) -> ProviderProfile | None:
        return self._profiles.get(self.canonical_name(name))

    def unregister(self, name: str) -> bool:
        canonical = self.canonical_name(name)
        removed = self._profiles.pop(canonical, None)
        if removed is None:
            return False
        self._origins.pop(canonical, None)
        self._aliases = {
            alias: target for alias, target in self._aliases.items()
            if target != canonical
        }
        return True

    def list(self) -> list[ProviderProfile]:
        return [self._profiles[name] for name in sorted(self._profiles)]

    def origin(self, name: str) -> dict:
        return dict(self._origins.get(self.canonical_name(name)) or {"kind": "unknown"})

    def plugin_catalog(self) -> list[dict]:
        return [
            {"provider": name, **dict(origin)}
            for name, origin in sorted(self._origins.items())
            if origin.get("kind") == "plugin"
        ]

    def discover(self, root: Path) -> None:
        """Load ``provider.json`` and optional ``provider.py`` plugin folders.

        A Python plugin must expose ``register(registry)``. It runs with VARIANT-1's
        user privileges, so only locally trusted plugins belong here.
        """
        if not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            before_profiles = dict(self._profiles)
            before_aliases = dict(self._aliases)
            before_origins = dict(self._origins)
            try:
                self._load_plugin(child)
            except Exception as exc:
                # One failing folder cannot leave its JSON half registered or
                # its aliases shadowing a working built-in provider.
                self._profiles = before_profiles
                self._aliases = before_aliases
                self._origins = before_origins
                self.errors.append({"plugin": child.name, "error": str(exc)})

    def _load_plugin(self, child: Path) -> None:
        data_file = child / "provider.json"
        origin = {"kind": "plugin", "plugin": child.name, "path": str(child)}
        previous_origin = self._discovering_origin
        self._discovering_origin = origin
        try:
            self._load_plugin_contents(child, data_file, origin)
        finally:
            self._discovering_origin = previous_origin

    def _load_plugin_contents(self, child: Path, data_file: Path, origin: dict) -> None:
        if data_file.is_file():
            raw = json.loads(data_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("provider.json must contain an object")
            if raw.get("version"):
                origin["version"] = str(raw["version"])[:80]
            name = self.normalize(raw.get("name") or child.name)
            current = self.get(name)
            if current:
                self.register(current.with_overrides(raw))
            else:
                self.register(ProviderProfile(name=name, display_name=raw.get("display_name") or name).with_overrides(raw))

        code_file = child / "provider.py"
        if code_file.is_file():
            module_name = f"_variant1_provider_{self.normalize(child.name).replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, code_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {code_file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise ValueError("provider.py must define register(registry)")
                register(self)
            except Exception:
                sys.modules.pop(module_name, None)
                raise


def default_registry(app_root: str, config_dir: str | None = None) -> ProviderRegistry:
    dirs = [Path(app_root) / "config" / "plugins" / "model-providers"]
    if config_dir:
        candidate = Path(config_dir) / "plugins" / "model-providers"
        if candidate not in dirs:
            dirs.append(candidate)
    return ProviderRegistry(plugin_dirs=dirs)
