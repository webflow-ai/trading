import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import api.index as index_module


@pytest.fixture(autouse=True)
def no_real_redis(monkeypatch):
    """Tests must never touch the real production Redis — disabled by
    default for every test. A test that specifically wants to exercise
    Redis logic should monkeypatch `redis_cmd` with a fake instead of
    re-enabling KV_URL/KV_TOKEN against the real instance."""
    monkeypatch.setattr(index_module, "KV_URL", None)
    monkeypatch.setattr(index_module, "KV_TOKEN", None)


@pytest.fixture(autouse=True)
def clear_in_memory_caches():
    """The module-level caches persist across tests otherwise (they're
    plain dicts on the module, not reset between test functions)."""
    index_module.pcr_cache.clear()
    index_module.chain_store.clear()
    index_module.expiry_cache.clear()
    yield
    index_module.pcr_cache.clear()
    index_module.chain_store.clear()
    index_module.expiry_cache.clear()


@pytest.fixture
def fake_redis(monkeypatch):
    """An in-memory stand-in for Upstash's REST API, keyed the same way
    (RPUSH/EXPIRE/LRANGE/GET/SET) — lets persistence logic be tested for
    real without any network call."""
    store: dict = {}

    async def fake_redis_cmd(*args):
        cmd = args[0]
        if cmd == "RPUSH":
            key, value = args[1], args[2]
            store.setdefault(key, []).append(value)
            return len(store[key])
        if cmd == "EXPIRE":
            return 1
        if cmd == "LRANGE":
            key = args[1]
            return store.get(key, [])
        if cmd == "SET":
            key, value = args[1], args[2]
            store[key] = value
            return "OK"
        if cmd == "GET":
            key = args[1]
            return store.get(key)
        return None

    monkeypatch.setattr(index_module, "redis_cmd", fake_redis_cmd)
    monkeypatch.setattr(index_module, "KV_URL", "fake://redis")
    monkeypatch.setattr(index_module, "KV_TOKEN", "fake-token")
    return store
