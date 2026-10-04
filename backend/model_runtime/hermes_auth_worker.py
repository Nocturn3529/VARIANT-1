"""Run inside Hermes's Python environment; emit only account-flow receipts.

OAuth tokens never leave this process. Hermes remains the credential owner.
The parent must acknowledge a finished grant before it can replace the account.
"""
from __future__ import annotations

import contextlib
import json
import sys
from types import SimpleNamespace


def main() -> None:
    output = sys.stdout

    def emit(event: str, **fields) -> None:
        output.write(json.dumps({"event": event, **fields}) + "\n")
        output.flush()

    try:
        with open("nul" if sys.platform == "win32" else "/dev/null", "w") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from hermes_cli.auth import _nous_device_code_login, persist_nous_credentials
                if sys.argv[1] == "logout":
                    from hermes_cli.auth_commands import auth_logout_command
                    auth_logout_command(SimpleNamespace(provider="nous"))
                else:
                    def verification(url: str, code: str) -> None:
                        emit("pending", verification_url=url, user_code=code)

                    grant = _nous_device_code_login(
                        open_browser=False, on_verification=verification)
                    emit("ready")
                    if sys.stdin.readline().strip() != "commit":
                        return
                    persist_nous_credentials(grant)
                emit("complete")
    except BaseException:
        # Hermes/provider exceptions can contain response bodies and credentials.
        # Never forward those across the process boundary.
        emit("error", error="Hermes could not complete the Nous account operation. Check your Hermes installation and retry sign-in.")
        sys.exit(1)


if __name__ == "__main__":
    main()
