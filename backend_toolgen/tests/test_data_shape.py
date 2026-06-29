"""M2 (unify) — per-operation data_shape in api_graph. Run directly or via pytest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_probe import ProbeConfig, build_api_graph, derive_data_shape, infer_static_graph  # noqa: E402
from openapi_to_tools import parse_endpoints  # noqa: E402

HERE = Path(__file__).parent
SPEC = json.loads((HERE / "fixtures" / "petstore_nested.json").read_text())
ENDPOINTS = parse_endpoints(SPEC)
GRAPH = infer_static_graph(ENDPOINTS, SPEC)


def _ops(api_graph):
    return {op["slug"]: op for n in api_graph["nodes"] for op in n["operations"]}


def test_static_data_shape_from_schema_without_probe():
    g = build_api_graph(GRAPH, ENDPOINTS, None, SPEC)
    ops = _ops(g)
    # list_users -> array of User -> collection of objects
    lu = ops["list_users"]["data_shape"]
    assert lu["cardinality"] == "collection"
    assert "array_of_objects" in lu["hints"]
    assert lu["item_fields"]["id"]["role"] == "id"
    # get_user -> single object
    assert ops["get_user"]["data_shape"]["cardinality"] == "object"


def test_field_roles_and_ref_targets():
    g = build_api_graph(GRAPH, ENDPOINTS, None, SPEC)
    order = _ops(g)["get_order"]["data_shape"]
    fields = order["item_fields"]
    assert fields["user_id"]["role"] == "id"
    assert fields["total"]["role"] == "measure"
    # order references users via user_id -> surfaced as a ref target
    assert {"field": "user_id", "resource": "users"} in order.get("ref_targets", [])
    assert "has_ids" in order["hints"]


def test_example_driven_shape_via_replay():
    cfg = ProbeConfig.from_auth(
        "http://127.0.0.1:8077", "bearer:testtoken",
        cassette_dir=HERE / "fixtures" / "cassettes_m3", samples=2, budget=0, replay=True)
    from api_probe import run_probe
    probe_out = run_probe(ENDPOINTS, GRAPH, cfg, spec=SPEC)
    g = build_api_graph(GRAPH, ENDPOINTS, probe_out, SPEC)
    gu = _ops(g)["get_user"]["data_shape"]
    # real probed example shows created_at -> timestamp role
    assert "created_at" in gu["item_fields"]
    assert gu["item_fields"]["created_at"]["role"] == "timestamp"


def test_derive_data_shape_numeric_series():
    shape = derive_data_shape(
        {"role": "detail"},
        {"zre": [0.1, 0.2, 0.3], "zim": [0.0, 0.1, 0.05], "label": "EIS"},
        None, None, [])
    assert "numeric_series" in shape["hints"]
    assert shape["cardinality"] == "object"


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
