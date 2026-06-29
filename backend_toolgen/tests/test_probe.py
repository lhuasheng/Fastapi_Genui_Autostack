"""Milestone 2 — read-only crawl. Pure-function tests + offline replay e2e (no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_probe import (  # noqa: E402
    IdPool,
    ProbeConfig,
    _assignments,
    _param_resource,
    _redact,
    _truncate,
    infer_static_graph,
    parse_auth,
    run_probe,
)
from openapi_to_tools import parse_endpoints  # noqa: E402

HERE = Path(__file__).parent
SPEC = json.loads((HERE / "fixtures" / "petstore_nested.json").read_text())
ENDPOINTS = parse_endpoints(SPEC)
GRAPH = infer_static_graph(ENDPOINTS, SPEC)
STEMS = {k.rstrip("s"): k for k in GRAPH.nodes}  # users->user etc (approx; pool builds its own)


def test_parse_auth():
    h, c = parse_auth("bearer:abc")
    assert h == {"Authorization": "Bearer abc"}
    h, c = parse_auth("header:X-API-Key=k; cookie:sid=v")
    assert h == {"X-API-Key": "k"} and c == {"sid": "v"}


def test_idpool_harvest_plain_and_fk():
    stems = {"user": "users", "order": "orders"}
    pool = IdPool(stems)
    pool.harvest([{"id": 1}, {"id": 2}], "users")
    pool.harvest({"id": 10, "user_id": 1}, "orders")
    assert pool.get("users") == [1, 2]   # 1 from list, then user_id=1 dedup
    assert pool.get("orders") == [10]


def test_param_resource():
    stems = {"user": "users", "order": "orders"}
    assert _param_resource("/users/{user_id}", "user_id", stems) == "users"
    assert _param_resource("/users/{id}", "id", stems) == "users"  # preceding segment
    assert _param_resource("/orders/{order_id}", "order_id", stems) == "orders"


def test_assignments_gated_on_available_ids():
    stems = {"user": "users", "order": "orders"}
    pool = IdPool(stems)
    ep = next(e for e in ENDPOINTS if e.path == "/users/{user_id}")
    assert _assignments(ep, pool, stems, 3) == []      # no ids yet
    pool.add("users", 1); pool.add("users", 2)
    assert _assignments(ep, pool, stems, 3) == [{"user_id": 1}, {"user_id": 2}]


def test_redact_and_truncate():
    red = _redact({"email": "a@b.com", "name": "x", "nested": {"api_key": "k"}})
    assert red["email"] == "***REDACTED***" and red["nested"]["api_key"] == "***REDACTED***"
    assert red["name"] == "x"
    assert _truncate([1, 2, 3, 4, 5], 2) == [1, 2]


def test_replay_e2e_offline():
    """Crawl entirely from committed cassettes — no network, deterministic."""
    cfg = ProbeConfig.from_auth(
        "http://127.0.0.1:8077", "bearer:testtoken",
        cassette_dir=HERE / "fixtures" / "cassettes", samples=3, budget=0, replay=True)
    out = run_probe(ENDPOINTS, GRAPH, cfg)
    assert out["stats"]["network_calls"] == 0
    assert out["id_pool"]["users"] == [1, 2]
    assert set(out["id_pool"]["orders"]) == {10, 11, 12}
    reached = {r["path"] for r in out["results"] if r["status"] == 200}
    assert {"/users/{user_id}", "/orders/{order_id}", "/users/{user_id}/orders"} <= reached
    assert all(r["method"] in {"GET", "HEAD", "OPTIONS"} for r in out["results"])
    assert "create_user" in out["stats"]["skipped_write_endpoints"]
    blob = json.dumps(out["results"])
    assert "ada@example.com" not in blob and "REDACTED" in blob


def test_m3_views_and_drift_replay_offline():
    """View-variant + drift discovery, replayed from committed cassettes (no network)."""
    cfg = ProbeConfig.from_auth(
        "http://127.0.0.1:8077", "bearer:testtoken",
        cassette_dir=HERE / "fixtures" / "cassettes_m3", samples=2, budget=0, replay=True)
    out = run_probe(ENDPOINTS, GRAPH, cfg, spec=SPEC)
    assert out["stats"]["network_calls"] == 0
    # view variant: ?expand=address adds the `address` key vs baseline
    gv = out["views"].get("get_user", [])
    assert any(v["param"] == "expand" and "address" in v["added"] for v in gv), gv
    # drift: baseline omits declared `address`, includes undeclared `created_at`
    d = out["drift"].get("get_user")
    assert d and "address" in d["missing_declared"]
    assert "created_at" in d["undeclared_observed"]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
