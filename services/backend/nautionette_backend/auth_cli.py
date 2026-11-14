"""Local operator authentication recovery; never exposed over HTTP.

Run inside the backend container against its real DATA_DIR. Host/container access
is already administrative access to the trusted workspace. Passwords never occur
in command arguments or normal output. This CLI prints setup secrets only on
explicit invocation. The backend may also issue an expiring one-time setup link
in restricted operator logs before initial account claim.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from fastapi import HTTPException


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup-token", help="issue a short-lived first-admin setup token", allow_abbrev=False)
    recovery = commands.add_parser(
        "recover-password", help="reset a local user's password", allow_abbrev=False
    )
    recovery.add_argument("username")
    recovery.add_argument(
        "--password-file", type=Path, help="read a mounted secret file instead of prompting"
    )
    args = parser.parse_args(argv)
    try:
        # Import lazily so --help never initializes a database or reads secrets.
        from .auth import issue_setup_token, recover_password

        if args.command == "setup-token":
            print(issue_setup_token())
        else:
            if args.password_file is not None:
                # Strip only a file's final newline, never meaningful whitespace.
                password = args.password_file.read_text().removesuffix("\n").removesuffix("\r")
            else:
                if not sys.stdin.isatty():
                    parser.error("use an interactive terminal or --password-file")
                password = getpass.getpass("New password: ")
                if password != getpass.getpass("Confirm password: "):
                    parser.error("passwords do not match")
            if not password:
                parser.error("password must not be empty")
            recover_password(args.username, password)
            print("Password recovered; existing sessions revoked.")
    except (OSError, ValueError, RuntimeError, HTTPException):
        # Exception messages can contain secret values, usernames or file paths.
        print(
            "Authentication recovery failed; verify local configuration and command inputs.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
