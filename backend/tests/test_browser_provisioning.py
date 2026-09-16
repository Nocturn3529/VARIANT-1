from pathlib import Path

import pytest

from browser_fabric.provisioning import (
    BrowserProvisionError,
    chromium_installed,
    ensure_chromium_runtime,
)


def _complete(root: Path) -> None:
    for name in ("chromium-1234", "chromium_headless_shell-1234"):
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "INSTALLATION_COMPLETE").write_bytes(b"")


def test_chromium_runtime_requires_full_and_headless_install_markers(tmp_path):
    assert chromium_installed(str(tmp_path)) is False
    first = tmp_path / "chromium-1234"
    first.mkdir()
    (first / "INSTALLATION_COMPLETE").write_bytes(b"")
    assert chromium_installed(str(tmp_path)) is False
    _complete(tmp_path)
    assert chromium_installed(str(tmp_path)) is True


@pytest.mark.asyncio
async def test_first_use_installs_the_pinned_playwright_chromium(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            _complete(tmp_path)
            return b"installed", None

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    installed = await ensure_chromium_runtime(
        str(tmp_path), timeout_s=2, process_factory=create,
    )

    assert installed == tmp_path.resolve()
    args, kwargs = calls[0]
    assert args[-2:] == ("install", "chromium")
    assert kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_first_use_failure_is_clear_and_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    class Process:
        returncode = 7

        async def communicate(self):
            return b"network unavailable", None

    async def create(*_args, **_kwargs):
        return Process()

    with pytest.raises(BrowserProvisionError, match="network unavailable"):
        await ensure_chromium_runtime(
            str(tmp_path), timeout_s=2, process_factory=create,
        )
    assert chromium_installed(str(tmp_path)) is False
