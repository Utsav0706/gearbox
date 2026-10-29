"""Exact-match answer cache: the same request twice costs nothing the second time.

The key is a hash of the request as it affects the answer: the messages (with runs of
whitespace collapsed, so "What is  2+2?\n" and "What is 2+2?" match), the requested
model ("auto" or a pinned tier) and the token limit. Case is kept: in code, case
changes meaning. A semantic cache (similar, not identical, prompts) is out of scope.

Only complete answers (finish_reason "stop") are cached, so a cut-off answer is never
served again. Two backends behind one interface:
- MemoryCache: a bounded in-process LRU, for local runs and tests (lost on restart).
- RedisCache: shared and persistent, for Docker Compose and deployment (REDIS_URL).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Protocol

from gearbox.service.schemas import ChatCompletionRequest


def cache_key(req: ChatCompletionRequest) -> str:
    payload = {
        "model": req.model,
        "max_tokens": req.token_limit,
        "messages": [[m["role"], " ".join(m["content"].split())] for m in req.plain_messages()],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"gearbox:answer:{digest}"


class Cache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...


class MemoryCache:
    def __init__(self, ttl_s: int, max_entries: int) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._data: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if time.monotonic() > expires:
            del self._data[key]
            return None
        self._data.move_to_end(key)  # recently used: evicted last
        return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


class RedisCache:
    def __init__(self, client: Any, ttl_s: int) -> None:
        self._redis = client
        self._ttl = ttl_s

    def get(self, key: str) -> dict[str, Any] | None:
        raw = self._redis.get(key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._redis.set(key, json.dumps(value), ex=self._ttl)


class NoCache:
    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def set(self, key: str, value: dict[str, Any]) -> None:
        pass


def make_cache(enabled: bool, ttl_s: int, max_entries: int, redis_url: str | None) -> Cache:
    if not enabled:
        return NoCache()
    if redis_url:
        import redis

        return RedisCache(redis.Redis.from_url(redis_url), ttl_s)
    return MemoryCache(ttl_s, max_entries)
