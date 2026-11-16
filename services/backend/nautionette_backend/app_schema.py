"""Killable schema-validation worker. No backend imports or network/file resolvers."""

from __future__ import annotations

import json
import resource
import sys

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource


def deny_retrieval(uri):
    raise NoSuchResource(ref=uri)


if __name__ == "__main__":
    # Regex and combinatorial schema work cannot be cancelled reliably in a
    # Python thread. The parent also enforces a wall deadline and kills us.
    resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        request = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
        Draft202012Validator.check_schema(request["schema"])
        Draft202012Validator(request["schema"], registry=Registry(retrieve=deny_retrieval)).validate(
            request["data"]
        )
    except Exception:
        sys.exit(1)  # No schema/data/exception text crosses the private boundary.
    sys.stdout.write("ok")
