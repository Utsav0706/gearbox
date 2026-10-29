"""Smoke test for the Docker Compose stack, run by CI: python3 scripts/compose_smoke.py URL

Proves the pieces are wired together (API -> Postgres via the migration, API -> Redis)
without spending money: CI has no model key and no Ollama, so a real request must fail
cleanly with a 503 after trying every tier, and that failure must reach the request log.
Standard library only, so it runs on a bare CI runner.
"""

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name: str, ok: bool, detail: object) -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        sys.exit(1)


def main(base: str) -> None:
    status, health = call("GET", f"{base}/health")
    check("health is ok", status == 200 and health["status"] == "ok", health)
    check("database reachable", health["database"] is True, health)
    check(
        "cache is Redis and reachable", health["cache"] == {"backend": "redis", "ok": True}, health
    )

    chat = f"{base}/v1/chat/completions"
    status, body = call(
        "POST", chat, {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    )
    check("unknown model is a 400", status == 400 and "model must be" in str(body), body)

    status, body = call("POST", chat, {"messages": [{"role": "user", "content": "hi"}]})
    check("no tier available is a clean 503", status == 503, body)
    check("503 says every tier was tried", "no allowed tier answered" in str(body), body)

    status, stats = call("GET", f"{base}/stats")
    check("failure reached the request log", stats["requests"] == 1 and stats["errors"] == 1, stats)
    check("fallback was recorded", stats["fallbacks"] == 1, stats)
    print("compose smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1].rstrip("/"))
