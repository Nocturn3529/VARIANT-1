"""Empty terminal profiles follow the host, not a Windows shell."""

from execution_hosts.profiles import ExecutionProfileRegistry


def test_empty_profile_is_powershell_on_windows_and_sh_elsewhere(monkeypatch):
    registry = ExecutionProfileRegistry()

    monkeypatch.setattr("execution_hosts.profiles.os.name", "posix")
    monkeypatch.setattr(
        "execution_hosts.profiles.shutil.which",
        lambda name: "/bin/sh" if name == "sh" else None,
    )
    posix = registry.resolve("")
    assert posix.name == "sh"
    assert posix.argv[0] == "/bin/sh"

    monkeypatch.setattr("execution_hosts.profiles.os.name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "execution_hosts.profiles.os.path.isfile",
        lambda path: path == powershell,
    )
    def reject_path_lookup(name):
        raise AssertionError(f"Windows system shell must not use PATH lookup: {name}")

    monkeypatch.setattr("execution_hosts.profiles.shutil.which", reject_path_lookup)
    windows = registry.resolve("")
    assert windows.name == "powershell"
    assert windows.argv[0] == powershell
