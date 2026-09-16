"""Explicit shell profile resolution for local, WSL, and SSH terminals."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
from typing import Mapping, Sequence

from .models import ExecutionValidationError


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    name: str
    host_kind: str
    argv: tuple[str, ...]
    environment: dict[str, str]


class ExecutionProfileRegistry:
    """Host-local resolver; it performs no implicit workspace rebinding."""

    def __init__(self, profiles: Mapping[str, Sequence[str]] | None = None) -> None:
        self._custom = {
            str(name).strip().lower(): tuple(str(item) for item in argv)
            for name, argv in dict(profiles or {}).items()
        }

    @staticmethod
    def names() -> tuple[str, ...]:
        return (
            "powershell", "pwsh", "cmd", "git-bash", "wsl:<distribution>",
            "ssh:<host>", "sh", "bash",
        )

    def resolve(
        self,
        profile: str,
        *,
        argv: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> ResolvedProfile:
        name = str(profile or "").strip().lower()
        if argv is not None:
            command = tuple(str(item) for item in argv)
            if not command or any(not item or "\x00" in item for item in command):
                raise ExecutionValidationError("custom terminal argv is invalid")
            return ResolvedProfile(
                name=name or "custom", host_kind="local", argv=command,
                environment={str(key): str(value)
                             for key, value in dict(environment or {}).items()},
            )
        if not name:
            name = "powershell" if os.name == "nt" else "sh"
        custom = self._custom.get(name)
        if custom:
            return ResolvedProfile(
                name=name, host_kind="local", argv=custom,
                environment={str(key): str(value)
                             for key, value in dict(environment or {}).items()},
            )
        if name == "powershell":
            executable = shutil.which("powershell.exe" if os.name == "nt" else "pwsh")
            if not executable:
                raise ExecutionValidationError("PowerShell is not installed")
            command = (executable, "-NoLogo")
            host_kind = "local"
        elif name == "pwsh":
            executable = shutil.which("pwsh.exe" if os.name == "nt" else "pwsh")
            if not executable:
                raise ExecutionValidationError("PowerShell 7 is not installed")
            command = (executable, "-NoLogo")
            host_kind = "local"
        elif name == "cmd":
            executable = shutil.which("cmd.exe")
            if not executable:
                raise ExecutionValidationError("cmd.exe is unavailable")
            command = (executable,)
            host_kind = "local"
        elif name == "git-bash":
            candidates = [
                shutil.which("bash.exe"),
                os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "bash.exe"),
                os.path.join(os.environ.get("ProgramFiles", ""), "Git", "usr", "bin", "bash.exe"),
            ]
            executable = next((item for item in candidates if item and os.path.isfile(item)), None)
            if not executable:
                raise ExecutionValidationError("Git Bash is not installed")
            command = (executable, "--login", "-i")
            host_kind = "local"
        elif name.startswith("wsl:") or name == "wsl":
            executable = shutil.which("wsl.exe")
            if not executable:
                raise ExecutionValidationError("WSL is unavailable")
            distribution = name.partition(":")[2].strip()
            command = (executable, "-d", distribution) if distribution else (executable,)
            host_kind = "wsl"
        elif name.startswith("ssh:"):
            executable = shutil.which("ssh.exe") or shutil.which("ssh")
            target = name.partition(":")[2].strip()
            if not executable:
                raise ExecutionValidationError("SSH client is unavailable")
            if not target or target.startswith("-") or "\x00" in target:
                raise ExecutionValidationError("SSH profile needs a valid host target")
            command = (executable, target)
            host_kind = "ssh"
        elif name in {"sh", "bash"}:
            executable = shutil.which(name)
            if not executable:
                raise ExecutionValidationError(f"{name} is unavailable")
            command = (executable, "-i")
            host_kind = "local"
        else:
            raise ExecutionValidationError(f"unknown execution profile: {profile}")
        return ResolvedProfile(
            name=name, host_kind=host_kind, argv=tuple(command),
            environment={str(key): str(value)
                         for key, value in dict(environment or {}).items()},
        )


__all__ = ["ExecutionProfileRegistry", "ResolvedProfile"]
