"""Authenticated deployment checks. No automatic mutation retries or response logging."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from scripts.account_session import AccountSession


class APIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class API:
    def __init__(self):
        self.url = os.environ["APP_URL"].rstrip("/")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APP_URL must be direct HTTPS")
        self.session = AccountSession.from_env(self.url)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def call(self, path, method="GET", body=None, expected=200):
        request = urllib.request.Request(  # noqa: S310 - validated HTTPS; redirects disabled
            self.url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.session.open(request, timeout=250) as response:
                if response.status != expected:
                    raise RuntimeError(f"Deployment smoke: unexpected HTTP {response.status}")
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == expected:
                return None
            raise APIError(
                f"Deployment smoke: {method} {path} returned HTTP {error.code}", error.code
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError(
                "Deployment smoke: transport failed; inspect state, never replay a mutation"
            ) from None
