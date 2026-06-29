#!/usr/bin/env python3
"""
Shared OpenAPI primitives (the EXTRACT stage).

This is the base module both `openapi_to_tools` (the generator) and `api_probe`
(the discovery layer) build on, giving a one-directional import flow:

    openapi_model  ←  api_probe  ←  openapi_to_tools
          ↑___________________________________|

so neither importer needs a lazy/in-function import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx


def _snake(s: str) -> str:
    s = re.sub(r"[^\w]+", "_", s).strip("_")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower() or "x"


@dataclass
class Endpoint:
    method: str
    path: str
    operation_id: str
    summary: str
    description: str
    tags: list[str]
    path_params: list[str]
    query_params: list[dict[str, Any]]
    request_body: dict[str, Any] | None
    response_schema: dict[str, Any] | None
    deprecated: bool

    @property
    def slug(self) -> str:
        """Stable identifier: operationId, or method+path-derived."""
        if self.operation_id:
            return _snake(self.operation_id)
        parts = [p for p in self.path.split("/") if p and not p.startswith("{")]
        return _snake(f"{self.method.lower()}_{'_'.join(parts) or 'root'}")


def fetch_spec(url: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0, verify=False, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def parse_endpoints(spec: dict[str, Any]) -> list[Endpoint]:
    out: list[Endpoint] = []
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(op, dict):
                continue

            path_params = re.findall(r"\{(\w+)\}", path)
            query_params = [
                {
                    "name": p.get("name"),
                    "required": p.get("required", False),
                    "type": (p.get("schema") or {}).get("type", "string"),
                    "enum": (p.get("schema") or {}).get("enum"),
                    "description": p.get("description", ""),
                }
                for p in op.get("parameters", []) or []
                if p.get("in") == "query"
            ]
            rb_schema = None
            rb = op.get("requestBody") or {}
            if isinstance(rb, dict):
                ct = rb.get("content", {}).get("application/json", {})
                rb_schema = ct.get("schema")

            resp_schema = None
            for code, robj in (op.get("responses") or {}).items():
                if code.startswith("2") and isinstance(robj, dict):
                    ct = robj.get("content", {}).get("application/json", {})
                    resp_schema = ct.get("schema")
                    break

            out.append(Endpoint(
                method=method.upper(),
                path=path,
                operation_id=op.get("operationId", ""),
                summary=op.get("summary", ""),
                description=op.get("description", ""),
                tags=op.get("tags", []) or [],
                path_params=path_params,
                query_params=query_params,
                request_body=rb_schema,
                response_schema=resp_schema,
                deprecated=op.get("deprecated", False),
            ))
    return out
