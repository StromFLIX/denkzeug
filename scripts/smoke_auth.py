"""Sign a disposable smoke account in through the real native account API.

Never print credentials or response bodies. Files and their parent directory must
be private to the CI runner. --initialize changes the environment-bootstrap
password through the same endpoint used by normal clients, then signs in again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authenticate(base: str, username: str, directory: Path, *, initialize: bool = False) -> str:
    parsed = urlsplit(base)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise ValueError("Configure a direct HTTPS app origin, or an isolated localhost development origin")
    password = (directory / "account-password").read_text().rstrip("\r\n")
    if not password:
        raise ValueError("The smoke account password file is empty")
    opener = urllib.request.build_opener(NoRedirect())

    def post(path: str, body: dict, token: str = "") -> dict:
        request = urllib.request.Request(  # noqa: S310 - origin validated; redirects disabled
            base.rstrip("/") + "/api/auth/" + path,
            method="POST",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + token} if token else {}),
            },
        )
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def login(value: str) -> dict:
        return post("login", {"username": username, "password": value, "native": True})

    if initialize:
        temporary = (directory / "bootstrap-password").read_text().rstrip("\r\n")
        initial = login(temporary)
        changed = post(
            "change-password",
            {"current_password": temporary, "password": password},
            initial["access_token"],
        )
        if changed.get("changed") is not True or changed.get("signed_out") is not True:
            raise ValueError("Bootstrap password replacement did not complete")
    result = login(password)
    if result["user"].get("must_change_password") or not result.get("access_token"):
        raise ValueError("Smoke account sign-in is incomplete")
    return result["access_token"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--base", default=os.environ.get("BASE", "http://localhost:8080"))
    parser.add_argument("--username", default="smoke-admin")
    parser.add_argument("--directory", type=Path, default=Path(".ci-auth"))
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args(argv)
    try:
        token = authenticate(args.base, args.username, args.directory, initialize=args.initialize)
        # The bootstrap verification session is disposable too. Functional tests
        # create their own refreshing session; no token leaves process memory.
        request = urllib.request.Request(  # noqa: S310 - authenticate validates this canonical origin
            args.base.rstrip("/") + "/api/auth/logout",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15):
            pass
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        print(
            "Smoke account authentication failed; check bootstrap configuration and backend readiness.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
