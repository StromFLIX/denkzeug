"""Read-only staging latency smoke. Never print chat/session records or credentials."""

from time import monotonic

from scripts.smoke_api import API


def check(api, *, budget=3.0):
    timings = []
    for resource in ("chats", "sessions"):
        for limit in (1, 20, 50):
            start = monotonic()
            page = api.call(f"/api/chat-inspection/{resource}?limit={limit}", timeout=budget)
            elapsed = monotonic() - start
            if (
                not isinstance(page, dict)
                or not isinstance(page.get(resource), list)
                or len(page[resource]) > limit
                or "next_offset" not in page
            ):
                raise RuntimeError("Chat diagnostic smoke: invalid bounded page")
            if elapsed >= budget:
                raise RuntimeError("Chat diagnostic smoke: local metadata read exceeded latency budget")
            timings.append(
                {
                    "resource": resource,
                    "limit": limit,
                    "rows": len(page[resource]),
                    "seconds": round(elapsed, 3),
                }
            )
    return timings


def main():
    with API() as api:
        timings = check(api)
    # Only performance metadata; no IDs, titles, transcripts, or tool payloads.
    print({"chat_diagnostics": "ok", "timings": timings})


if __name__ == "__main__":
    main()
