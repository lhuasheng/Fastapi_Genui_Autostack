"""Milestone 1 — static relationship graph. Run: python -m pytest tests/ (or this file directly)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_probe import (  # noqa: E402
    classify_role,
    collection_segments,
    entity_fields,
    infer_static_graph,
    resolve_schema,
)
from openapi_to_tools import parse_endpoints  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "petstore_nested.json"
SPEC = json.loads(FIXTURE.read_text())
ENDPOINTS = parse_endpoints(SPEC)
GRAPH = infer_static_graph(ENDPOINTS, SPEC)


def _edges(kind):
    return {(e["src"], e["dst"]) for e in GRAPH.edges if e["kind"] == kind}


def test_collection_segments_strips_generic_and_params():
    assert collection_segments("/api/v1/users/{id}/orders/{oid}") == ["users", "orders"]
    assert collection_segments("/users") == ["users"]


def test_resolve_schema_cycle_guard():
    spec = {"components": {"schemas": {"Node": {"type": "object", "properties": {
        "child": {"$ref": "#/components/schemas/Node"}}}}}}
    out = resolve_schema(spec, {"$ref": "#/components/schemas/Node"})
    assert out["properties"]["child"] == {"$ref_cycle": "Node"}


def test_entity_fields_unwraps_array_not_domain_items():
    # Order has a domain property literally named "items"; must NOT be treated as envelope.
    order = {"$ref": "#/components/schemas/Order"}
    fields, is_array, primary = entity_fields(SPEC, order)
    assert "user_id" in fields and "items" in fields
    assert is_array is False
    assert primary == "Order"


def test_entity_fields_unwraps_real_envelope():
    # No top-level "id" + pagination markers => treat as envelope, return element fields.
    spec = {"components": {"schemas": {"Thing": {"type": "object", "properties": {
        "id": {"type": "integer"}, "label": {"type": "string"}}}}}}
    envelope = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}},
        "total": {"type": "integer"}, "page": {"type": "integer"}}}
    fields, is_array, primary = entity_fields(spec, envelope)
    assert is_array is True
    assert set(fields) == {"id", "label"}
    assert primary == "Thing"


def test_nodes_are_path_resources():
    assert set(GRAPH.nodes) == {"users", "orders"}


def test_contains_edge_from_path_nesting():
    assert ("users", "orders") in _edges("contains")


def test_references_edge_from_fk_field():
    assert ("orders", "users") in _edges("references")


def test_roles():
    roles = {op["path"] + ":" + op["method"]: op["role"]
             for n in GRAPH.nodes.values() for op in n["operations"]}
    assert roles["/users:GET"] == "list"
    assert roles["/users:POST"] == "create"
    assert roles["/users/{user_id}:GET"] == "detail"
    assert roles["/users/{user_id}/orders:GET"] == "nested_list"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
