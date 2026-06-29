#!/usr/bin/env python3
"""
API discovery layer for openapi_to_tools.

Two phases sit between EXTRACT (parse_endpoints) and CLUSTER:

  DISCOVER (static, no network)
    - resolve_schema / collect_component_refs : expand $ref against components.
    - classify_role                           : list | detail | nested_list | create | ...
    - infer_static_graph                      : resource nodes + contains/references/
                                                composition edges (from path nesting,
                                                FK-like field names, $ref composition).

  PROBE (live, read-only)  [milestone 2+, added later]
    - topological crawl seeded from parameterless GETs, harvesting IDs.

Milestone 1 emits two artifacts from the doc alone:
  api_graph.json     machine-readable graph
  api_analysis.md    resource hierarchy tree + relationship list with evidence

Standalone (M1) usage:
    python api_probe.py --file openapi.json -o ./apigen_out
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from openapi_model import Endpoint, _snake, parse_endpoints

# Path segments that carry no resource meaning.
_GENERIC_SEGMENTS = {"api", "rest", "graphql"}


# =============================================================================
# $ref resolution
# =============================================================================

def _ref_name(schema: Any) -> str | None:
    """Component name if `schema` is a top-level {"$ref": "#/components/schemas/X"}."""
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        return schema["$ref"].rsplit("/", 1)[-1]
    return None


def resolve_schema(spec: dict[str, Any], schema: Any, _seen: frozenset[str] = frozenset()) -> Any:
    """Best-effort expansion of `$ref`s against components/schemas, cycle-guarded.

    Cycles resolve to {"$ref_cycle": name} so callers never recurse forever.
    """
    if not isinstance(schema, dict):
        return schema

    name = _ref_name(schema)
    if name is not None:
        if name in _seen:
            return {"$ref_cycle": name}
        target = (spec.get("components", {}).get("schemas", {}) or {}).get(name)
        if target is None:
            return {"$ref_unresolved": name}
        return resolve_schema(spec, target, _seen | {name})

    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: resolve_schema(spec, pv, _seen) for pk, pv in v.items()}
        elif k in {"items", "additionalProperties"}:
            out[k] = resolve_schema(spec, v, _seen)
        elif k in {"allOf", "anyOf", "oneOf"} and isinstance(v, list):
            out[k] = [resolve_schema(spec, x, _seen) for x in v]
        else:
            out[k] = v
    return out


def collect_component_refs(schema: Any, _depth: int = 0) -> set[str]:
    """All component names referenced anywhere within `schema` (pre-resolution)."""
    found: set[str] = set()
    if _depth > 50 or not isinstance(schema, (dict, list)):
        return found
    if isinstance(schema, list):
        for x in schema:
            found |= collect_component_refs(x, _depth + 1)
        return found
    n = _ref_name(schema)
    if n:
        found.add(n)
    for v in schema.values():
        found |= collect_component_refs(v, _depth + 1)
    return found


def entity_fields(spec: dict[str, Any], schema: Any) -> tuple[list[str], bool, str | None]:
    """Return (field_names, is_array, primary_component_name) for a response schema.

    Unwraps a top-level array to its item schema. Also unwraps common pagination
    envelopes ({"items": [...], "total": ...}) to the element schema.
    """
    if schema is None:
        return [], False, None
    primary = _ref_name(schema)
    resolved = resolve_schema(spec, schema)
    is_array = False

    if isinstance(resolved, dict) and resolved.get("type") == "array":
        is_array = True
        items = resolved.get("items") or {}
        primary = _ref_name(schema.get("items", {})) or primary
        resolved = items

    props = resolved.get("properties", {}) if isinstance(resolved, dict) else {}
    # Unwrap pagination envelope only when real pagination markers sit beside the
    # array — so a domain object with a property literally named "items" is left alone.
    _PAGE_MARKERS = {"total", "count", "page", "pages", "next", "previous",
                     "has_more", "limit", "offset", "cursor", "total_count"}
    # A domain entity carries its own "id"; a pagination envelope does not.
    if not is_array and "id" not in props and _PAGE_MARKERS & set(props):
        for key in ("items", "data", "results"):
            inner = props.get(key)
            if isinstance(inner, dict) and inner.get("type") == "array":
                is_array = True
                item_schema = inner.get("items") or {}
                raw_items = (schema.get("properties", {}) or {}).get(key, {}).get("items", {})
                primary = _ref_name(raw_items) or primary
                props = item_schema.get("properties", {}) if isinstance(item_schema, dict) else {}
                break

    return list(props.keys()), is_array, primary


# =============================================================================
# Path / resource derivation
# =============================================================================

def collection_segments(path: str) -> list[str]:
    """Static, resource-bearing path segments in order.

    /api/v1/users/{id}/orders/{oid}  -> ["users", "orders"]
    /users                           -> ["users"]
    """
    segs: list[str] = []
    for s in path.split("/"):
        if not s or s.startswith("{"):
            continue
        if s.lower() in _GENERIC_SEGMENTS:
            continue
        if s.lower().rstrip("0123456789") in {"v"}:  # v1, v2, ...
            continue
        if s.isdigit():  # concrete id leaked into a filled path
            continue
        segs.append(_snake(s))
    return segs


def resource_of(path: str) -> str:
    segs = collection_segments(path)
    return segs[-1] if segs else "root"


def _ends_with_param(path: str) -> bool:
    last = [s for s in path.split("/") if s]
    return bool(last) and last[-1].startswith("{")


def _singular(name: str) -> str:
    """Naive stem for FK matching: users -> user, categories -> category."""
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("ses"):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def classify_role(ep: Endpoint, response_is_array: bool) -> str:
    m = ep.method.upper()
    if m == "POST":
        return "create"
    if m in {"PUT", "PATCH"}:
        return "update"
    if m == "DELETE":
        return "delete"
    # GET (and HEAD/OPTIONS treated as reads)
    if _ends_with_param(ep.path):
        return "detail"
    if ep.path_params:
        # has params but ends static, e.g. /users/{id}/orders
        return "nested_list" if response_is_array else "detail"
    return "list" if response_is_array else "detail"


# =============================================================================
# Static graph
# =============================================================================

@dataclass
class StaticGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    _edge_keys: set[tuple] = field(default_factory=set, repr=False)

    def node(self, name: str, source: str) -> dict[str, Any]:
        n = self.nodes.setdefault(
            name, {"name": name, "sources": [], "operations": []}
        )
        if source not in n["sources"]:
            n["sources"].append(source)
        return n

    def add_edge(self, src: str, dst: str, kind: str, evidence: str, field_name: str | None = None) -> None:
        if src == dst:
            return
        key = (src, dst, kind, field_name)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        edge = {"src": src, "dst": dst, "kind": kind, "evidence": evidence}
        if field_name:
            edge["field"] = field_name
        self.edges.append(edge)

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes.values()), "edges": self.edges}


def infer_static_graph(endpoints: list[Endpoint], spec: dict[str, Any]) -> StaticGraph:
    g = StaticGraph()

    # First pass: register resource nodes from path segments so FK matching has targets.
    for ep in endpoints:
        for seg in collection_segments(ep.path):
            g.node(seg, "path")

    resource_stems = {_singular(name): name for name in g.nodes}

    for ep in endpoints:
        segs = collection_segments(ep.path)
        res = segs[-1] if segs else "root"
        node = g.node(res, "path")

        fields, is_array, primary = entity_fields(spec, ep.response_schema)
        role = classify_role(ep, is_array)
        node["operations"].append(
            {"slug": ep.slug, "method": ep.method, "path": ep.path, "role": role,
             "response_entity": primary}
        )

        # contains: parent collection -> child collection (path nesting)
        for parent, child in zip(segs, segs[1:]):
            g.add_edge(parent, child, "contains", "path-nesting")

        # references: FK-like field names pointing at another resource
        body_fields, _, _ = entity_fields(spec, ep.request_body)
        for fname in set(fields) | set(body_fields):
            stem = _singular(_snake(fname))
            for suffix in ("_id", "_uuid", "_uid"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            target = resource_stems.get(_singular(stem))
            if target and target != res:
                g.add_edge(res, target, "references", "fk-field", field_name=fname)

        # composition: response component embeds other components
        if primary:
            for comp in collect_component_refs(ep.response_schema):
                comp_node = _snake(comp)
                if comp_node != res and comp_node in g.nodes:
                    g.add_edge(res, comp_node, "composition", "ref-embed")

    return g


# =============================================================================
# PROBE (live, read-only crawl)
# =============================================================================

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_ID_KEY_RE = re.compile(r"^(id|pk|uuid|guid)$|_(id|uuid|uid|guid)$", re.I)
_REDACT_KEY_RE = re.compile(r"email|token|password|passwd|secret|api[_-]?key|ssn|phone|authorization", re.I)
_REDACTED = "***REDACTED***"


@dataclass
class ProbeConfig:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    budget: int = 200            # max real network calls
    samples: int = 3             # ids tried per parameterized endpoint
    timeout: float = 30.0
    rate: float = 0.0            # requests/sec throttle (0 = unlimited)
    max_rounds: int = 6          # dependency-resolution passes
    cassette_dir: Path | None = None
    replay: bool = False         # use cassette only, never touch network
    verify_tls: bool = False
    truncate_items: int = 5      # arrays truncated to first N in stored bodies

    @classmethod
    def from_auth(cls, base_url: str, auth: str | None, **kw: Any) -> "ProbeConfig":
        headers, cookies = parse_auth(auth)
        return cls(base_url=base_url.rstrip("/"), headers=headers, cookies=cookies, **kw)


def parse_auth(auth: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Parse a `--probe-auth` spec into (headers, cookies).

    bearer:TOKEN            -> Authorization: Bearer TOKEN
    header:X-API-Key=abc    -> X-API-Key: abc
    cookie:session=abc      -> cookie session=abc
    """
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    if not auth:
        return headers, cookies
    for part in auth.split(";"):
        part = part.strip()
        if not part:
            continue
        scheme, _, rest = part.partition(":")
        scheme = scheme.lower()
        if scheme == "bearer":
            headers["Authorization"] = f"Bearer {rest}"
        elif scheme == "header":
            k, _, v = rest.partition("=")
            headers[k.strip()] = v.strip()
        elif scheme == "cookie":
            k, _, v = rest.partition("=")
            cookies[k.strip()] = v.strip()
        else:
            raise ValueError(f"unknown auth scheme: {scheme!r} (use bearer:/header:/cookie:)")
    return headers, cookies


@dataclass
class ProbeResult:
    slug: str
    method: str
    path: str                       # template, e.g. /users/{user_id}
    url: str                        # concrete url called
    params: dict[str, Any]
    status: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    from_cache: bool = False
    body: Any = None                # redacted + truncated


class IdPool:
    """Identifier values harvested from responses, keyed by resource node name."""

    def __init__(self, stems: dict[str, str]):
        self._stems = stems                       # singular-stem -> node name
        self.values: dict[str, list[Any]] = {}

    def add(self, resource: str, value: Any) -> None:
        if value is None or isinstance(value, (dict, list, bool)):
            return
        bucket = self.values.setdefault(resource, [])
        if value not in bucket and len(bucket) < 50:
            bucket.append(value)

    def get(self, resource: str) -> list[Any]:
        return self.values.get(resource, [])

    def node_for_field(self, field_name: str) -> str | None:
        n = _snake(field_name)
        for suffix in ("_id", "_uuid", "_uid", "_guid"):
            if n.endswith(suffix):
                n = n[: -len(suffix)]
                break
        return self._stems.get(_singular(n))

    def harvest(self, body: Any, resource: str, _depth: int = 0) -> None:
        """Walk a response and bucket ids. Plain `id` -> `resource`; `<x>_id` -> x's node."""
        if _depth > 8:
            return
        if isinstance(body, list):
            for item in body:
                self.harvest(item, resource, _depth + 1)
            return
        if not isinstance(body, dict):
            return
        for k, v in body.items():
            if isinstance(v, (dict, list)):
                # nested object: its `id` belongs to the field's own resource if known
                child_res = self.node_for_field(k) or resource
                self.harvest(v, child_res, _depth + 1)
                continue
            if _ID_KEY_RE.search(k):
                target = resource if _snake(k) in {"id", "pk", "uuid", "guid"} else (
                    self.node_for_field(k) or resource)
                self.add(target, v)


def _param_resource(path: str, param: str, stems: dict[str, str]) -> str | None:
    """Which resource's ids satisfy `{param}` in `path`."""
    n = _snake(param)
    for suffix in ("_id", "_uuid", "_uid", "_guid"):
        if n.endswith(suffix):
            node = stems.get(_singular(n[: -len(suffix)]))
            if node:
                return node
    if n in {"id", "pk", "uuid", "guid"}:
        # static segment immediately preceding "{param}"
        toks = [t for t in path.split("/") if t]
        for i, t in enumerate(toks):
            if t == "{" + param + "}" and i > 0 and not toks[i - 1].startswith("{"):
                return _snake(toks[i - 1])
    return stems.get(_singular(n))


def _assignments(ep: Endpoint, pool: IdPool, stems: dict[str, str], samples: int) -> list[dict[str, Any]]:
    """Up to `samples` concrete {param: id} fillings, or [] if any param has no ids."""
    import itertools

    per_param: list[list[Any]] = []
    for p in ep.path_params:
        res = _param_resource(ep.path, p, stems)
        vals = pool.get(res) if res else []
        if not vals:
            return []
        per_param.append(vals[:samples])
    combos = list(itertools.product(*per_param))[:samples]
    return [dict(zip(ep.path_params, combo)) for combo in combos]


def _redact(obj: Any, _depth: int = 0) -> Any:
    if _depth > 12:
        return obj
    if isinstance(obj, dict):
        return {k: (_REDACTED if _REDACT_KEY_RE.search(k) else _redact(v, _depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x, _depth + 1) for x in obj]
    return obj


def _truncate(obj: Any, n: int, _depth: int = 0) -> Any:
    if _depth > 12:
        return obj
    if isinstance(obj, list):
        return [_truncate(x, n, _depth + 1) for x in obj[:n]]
    if isinstance(obj, dict):
        return {k: _truncate(v, n, _depth + 1) for k, v in obj.items()}
    return obj


def _cassette_key(method: str, url: str, params: dict[str, Any] | None) -> str:
    blob = json.dumps({"m": method.upper(), "u": url, "p": params or {}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _load_cassette(cfg: ProbeConfig, key: str) -> dict[str, Any] | None:
    if not cfg.cassette_dir:
        return None
    f = cfg.cassette_dir / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text())
    return None


def _save_cassette(cfg: ProbeConfig, key: str, rec: dict[str, Any]) -> None:
    if not cfg.cassette_dir:
        return
    cfg.cassette_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cassette_dir / f"{key}.json").write_text(json.dumps(rec, indent=2))


def _request(client: httpx.Client, method: str, url: str, params: dict[str, Any] | None,
             cfg: ProbeConfig) -> tuple[int | None, Any, str | None]:
    """One network call with bounded retries. Returns (status, json_body|None, error)."""
    for attempt in range(3):
        try:
            r = client.request(method, url, params=params)
        except Exception as e:  # noqa: BLE001
            return None, None, f"{type(e).__name__}: {e}"
        if r.status_code == 429 and attempt < 2:
            time.sleep(min(float(r.headers.get("Retry-After", 1)), 10))
            continue
        if 500 <= r.status_code < 600 and attempt < 2:
            time.sleep(0.5 * (attempt + 1))
            continue
        body: Any = None
        if "application/json" in r.headers.get("content-type", ""):
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = None
        return r.status_code, body, None
    return None, None, "retries exhausted"


# --- M3: view variants + spec drift --------------------------------------------

_VIEW_PARAM_RE = re.compile(
    r"expand|include|fields|view|format|detail|embed|with|verbosity|depth|hydrate", re.I)
_JSON_TYPE = {dict: "object", list: "array", bool: "boolean",
              int: "integer", float: "number", str: "string", type(None): "null"}


def _is_view_param(qp: dict[str, Any]) -> bool:
    """A query param likely to reshape the response (bool/enum, or a known name)."""
    return bool(qp.get("enum")) or qp.get("type") == "boolean" or bool(
        _VIEW_PARAM_RE.search(qp.get("name", "")))


def _variant_values(qp: dict[str, Any]) -> list[Any]:
    """Concrete values to try for a view param. Only probe when guessable (enum/bool)."""
    if qp.get("enum"):
        return list(qp["enum"])[:4]
    if qp.get("type") == "boolean":
        return ["true"]
    return []  # free-form strings (e.g. ?fields=a,b) aren't safely guessable


def _response_keys(body: Any) -> set[str]:
    """Top-level keys of the response element (first item if a list)."""
    if isinstance(body, list):
        return set(body[0].keys()) if body and isinstance(body[0], dict) else set()
    if isinstance(body, dict):
        return set(body.keys())
    return set()


def _compute_drift(spec: dict[str, Any], declared: Any, observed: Any) -> dict[str, Any] | None:
    """Compare an observed response element against the declared (resolved) schema."""
    if declared is None or observed is None:
        return None
    resolved = resolve_schema(spec, declared)
    if isinstance(resolved, dict) and resolved.get("type") == "array":
        resolved = resolved.get("items") or {}
    element = observed[0] if isinstance(observed, list) and observed else observed
    if not isinstance(element, dict) or not isinstance(resolved, dict):
        return None
    props = resolved.get("properties", {}) or {}
    required = set(resolved.get("required", []) or [])
    declared_names = set(props)
    observed_names = set(element)

    missing = sorted((declared_names - observed_names))
    undeclared = sorted((observed_names - declared_names)) if props else []
    type_mismatch = []
    for name in declared_names & observed_names:
        if _REDACT_KEY_RE.search(name):
            continue
        want = props[name].get("type") if isinstance(props[name], dict) else None
        got = _JSON_TYPE.get(type(element[name]))
        if want and got and want != got and not (want == "number" and got == "integer"):
            type_mismatch.append({"field": name, "declared": want, "observed": got})

    if not (missing or undeclared or type_mismatch):
        return None
    return {
        "missing_declared": missing,
        "missing_required": sorted(set(missing) & required),
        "undeclared_observed": undeclared,
        "type_mismatch": type_mismatch,
    }


def run_probe(endpoints: list[Endpoint], graph: StaticGraph, cfg: ProbeConfig,
              spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only topological crawl. Mutating methods are refused outright.

    When `spec` is provided, also discovers param-driven view variants and records
    drift between the declared schema and observed responses.
    """
    stems = {_singular(name): name for name in graph.nodes}
    pool = IdPool(stems)
    results: list[ProbeResult] = []
    calls = 0  # real network calls counted against budget
    baseline: dict[str, tuple[str, set[str]]] = {}   # slug -> (filled_path, response keys)
    drift: dict[str, Any] = {}
    by_slug = {ep.slug: ep for ep in endpoints}

    reads = [ep for ep in endpoints if ep.method.upper() in _READ_METHODS]
    skipped_writes = [ep.slug for ep in endpoints if ep.method.upper() not in _READ_METHODS]

    client = httpx.Client(timeout=cfg.timeout, verify=cfg.verify_tls,
                          follow_redirects=True, headers=cfg.headers, cookies=cfg.cookies)

    def do_call(ep: Endpoint, filled_path: str, params: dict[str, Any]) -> tuple[bool, Any]:
        nonlocal calls
        if ep.method.upper() not in _READ_METHODS:
            return False, None  # hard safety net
        url = f"{cfg.base_url}{filled_path}"
        key = _cassette_key(ep.method, url, params)
        cached = _load_cassette(cfg, key)
        if cached is not None:
            status, body, error, from_cache = cached["status"], cached.get("body"), cached.get("error"), True
        elif cfg.replay:
            results.append(ProbeResult(ep.slug, ep.method, ep.path, url, params,
                                       error="no-cassette (replay mode)"))
            return False, None
        else:
            if calls >= cfg.budget:
                return False, None
            if cfg.rate:
                time.sleep(1.0 / cfg.rate)
            t0 = time.time()
            status, body, error = _request(client, ep.method, url, params, cfg)
            calls += 1
            latency = int((time.time() - t0) * 1000)
            _save_cassette(cfg, key, {"status": status, "error": error, "latency_ms": latency,
                                      "body": _truncate(_redact(body), cfg.truncate_items)})
            from_cache = False

        ok = status is not None and 200 <= (status or 0) < 300
        # Harvest ids from the FULL body (cassette already stored truncated/redacted).
        if ok and body is not None:
            pool.harvest(body, resource_of(ep.path))  # template, not the filled path
            if not params and ep.slug not in baseline:   # baseline = no optional params
                baseline[ep.slug] = (filled_path, _response_keys(body))
                if spec is not None and ep.response_schema is not None:
                    d = _compute_drift(spec, ep.response_schema, body)
                    if d:
                        drift[ep.slug] = d

        stored = body if from_cache else _truncate(_redact(body), cfg.truncate_items)
        results.append(ProbeResult(
            ep.slug, ep.method, ep.path, url, params, status=status,
            error=error, from_cache=from_cache, body=stored,
            latency_ms=(cached or {}).get("latency_ms") if from_cache else None,
        ))
        return ok, body

    # Round 0: parameterless reads (collections) seed the id pool.
    done: set[int] = set()
    for i, ep in enumerate(reads):
        if not ep.path_params:
            do_call(ep, ep.path, {})
            done.add(i)

    # Rounds 1..N: fill path params from the pool as ids become available.
    for _ in range(cfg.max_rounds):
        progressed = False
        for i, ep in enumerate(reads):
            if i in done:
                continue
            for assign in _assignments(ep, pool, stems, cfg.samples):
                filled = ep.path
                for p, v in assign.items():
                    filled = filled.replace("{" + p + "}", str(v))
                do_call(ep, filled, {})
                done.add(i)
                progressed = True
        if not progressed:
            break

    # View-variant pass: re-call reached endpoints varying one view param at a time,
    # diffing the response key set against the baseline (no-param) call.
    views: dict[str, list[dict[str, Any]]] = {}
    for ep in reads:
        if ep.slug not in baseline:
            continue
        filled_path, base_keys = baseline[ep.slug]
        for qp in ep.query_params:
            if not _is_view_param(qp):
                continue
            for val in _variant_values(qp):
                ok, body = do_call(ep, filled_path, {qp["name"]: val})
                if not ok:
                    continue
                vkeys = _response_keys(body)
                added, removed = sorted(vkeys - base_keys), sorted(base_keys - vkeys)
                if added or removed:
                    views.setdefault(ep.slug, []).append(
                        {"param": qp["name"], "value": val, "added": added, "removed": removed})

    client.close()
    return {
        "results": [asdict(r) for r in results],
        "id_pool": pool.values,
        "views": views,
        "drift": drift,
        "stats": {
            "network_calls": calls,
            "total_results": len(results),
            "budget": cfg.budget,
            "replay": cfg.replay,
            "view_variants": sum(len(v) for v in views.values()),
            "drift_endpoints": len(drift),
            "skipped_write_endpoints": skipped_writes,
            "unreached_endpoints": [reads[i].slug for i in range(len(reads)) if i not in done],
        },
    }


# =============================================================================
# Merge static graph + probe evidence
# =============================================================================

def _probe_index(probe_out: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """slug -> {example, observed_fields, status} from the first 2xx result per slug."""
    idx: dict[str, dict[str, Any]] = {}
    if not probe_out:
        return idx
    for r in probe_out["results"]:
        if not r.get("params") and r.get("status") and 200 <= r["status"] < 300:
            idx.setdefault(r["slug"], {
                "status": r["status"],
                "example": r.get("body"),
                "observed_fields": sorted(_response_keys(r.get("body"))),
            })
    return idx


# --- data shape (the descriptor the UI bridge matches on) ---------------------

_PY_JSON = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"),
            (list, "array"), (dict, "object"))
_TS_RE = re.compile(r"(^|_)(at|date|time|timestamp|datetime)$|created|updated", re.I)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    for py, name in _PY_JSON:
        if isinstance(v, py):
            return name
    return "string"


def _is_num_array(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)


def _field_role(name: str, jtype: str, value: Any = None, enum: Any = None) -> str | None:
    if _ID_KEY_RE.search(name):
        return "id"
    if _TS_RE.search(name) or (isinstance(value, str) and _ISO_RE.match(value)):
        return "timestamp"
    if enum:
        return "category"
    if jtype in {"number", "integer"}:
        return "measure"
    if jtype == "string":
        return "label"
    return None


def _fields_from_example(el: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in el.items():
        t = _json_type(v)
        d: dict[str, Any] = {"type": t}
        role = _field_role(k, t, value=v)
        if role:
            d["role"] = role
        out[k] = d
    return out


def _fields_from_schema(spec: dict[str, Any] | None, schema: Any) -> tuple[dict[str, Any], bool]:
    if schema is None:
        return {}, False
    resolved = resolve_schema(spec or {}, schema)
    is_array = isinstance(resolved, dict) and resolved.get("type") == "array"
    if is_array:
        resolved = resolved.get("items") or {}
    props = resolved.get("properties", {}) if isinstance(resolved, dict) else {}
    out: dict[str, Any] = {}
    for name, ps in props.items():
        t = ps.get("type", "any") if isinstance(ps, dict) else "any"
        enum = ps.get("enum") if isinstance(ps, dict) else None
        d: dict[str, Any] = {"type": t}
        role = _field_role(name, t if isinstance(t, str) else "", enum=enum)
        if role:
            d["role"] = role
        if enum:
            d["enum"] = enum
        out[name] = d
    return out, is_array


def derive_data_shape(op: dict[str, Any], example: Any, ep: Endpoint | None,
                      spec: dict[str, Any] | None, ref_targets: list[dict[str, str]]) -> dict[str, Any]:
    """Normalized data descriptor for one operation (see schema/data-shape.schema.json)."""
    hints: set[str] = set()
    item_fields: dict[str, Any] = {}
    cardinality = "object"

    if example is not None:
        if isinstance(example, list):
            cardinality = "collection"
            first = example[0] if example else None
            if isinstance(first, dict):
                item_fields = _fields_from_example(first)
                hints.add("array_of_objects")
            elif _is_num_array(example):
                hints.add("numeric_series")
        elif isinstance(example, dict):
            cardinality = "object"
            item_fields = _fields_from_example(example)
            if any(_is_num_array(v) for v in example.values()):
                hints.add("numeric_series")
            if item_fields and all(not isinstance(v, (dict, list)) for v in example.values()):
                hints.add("key_value")
        else:
            cardinality = "scalar"
    else:
        item_fields, is_array = _fields_from_schema(spec, ep.response_schema if ep else None)
        cardinality = "collection" if (is_array or op.get("role") in {"list", "nested_list"}) else (
            "object" if item_fields else "scalar")
        if cardinality == "collection" and item_fields:
            hints.add("array_of_objects")

    if any(f.get("role") == "id" for f in item_fields.values()):
        hints.add("has_ids")

    shape: dict[str, Any] = {
        "cardinality": cardinality,
        "item_fields": item_fields,
        "hints": sorted(hints),
    }
    if ref_targets:
        shape["ref_targets"] = ref_targets
    return shape


def build_api_graph(graph: StaticGraph, endpoints: list[Endpoint],
                    probe_out: dict[str, Any] | None = None,
                    spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Consolidated graph: static nodes/edges + live evidence (examples/views/drift/ids)
    + a normalized `data_shape` per operation for the UI bridge."""
    idx = _probe_index(probe_out)
    views = (probe_out or {}).get("views", {})
    drift = (probe_out or {}).get("drift", {})
    id_pool = (probe_out or {}).get("id_pool", {})
    ep_by_slug = {ep.slug: ep for ep in endpoints}

    g = graph.to_dict()
    refs_by_res: dict[str, list[dict[str, str]]] = {}
    for e in g["edges"]:
        if e["kind"] == "references" and e.get("field"):
            refs_by_res.setdefault(e["src"], []).append({"field": e["field"], "resource": e["dst"]})

    for node in g["nodes"]:
        name = node["name"]
        node["observed"] = any(op["slug"] in idx for op in node["operations"])
        node["id_count"] = len(id_pool.get(name, []))
        for op in node["operations"]:
            info = idx.get(op["slug"])
            if info:
                op["observed_status"] = info["status"]
                op["observed_fields"] = info["observed_fields"]
            if op["slug"] in views:
                op["view_variants"] = views[op["slug"]]
            if op["slug"] in drift:
                op["drift"] = drift[op["slug"]]
            op["data_shape"] = derive_data_shape(
                op, (info or {}).get("example"), ep_by_slug.get(op["slug"]), spec,
                refs_by_res.get(name, []),
            )
    g["examples"] = {slug: info["example"] for slug, info in idx.items()}
    g["probe"] = {"stats": probe_out["stats"], "id_pool": id_pool} if probe_out else None
    return g


def annotation_enrichment(graph_dict: dict[str, Any], endpoints: list[Endpoint]) -> dict[str, dict[str, Any]]:
    """Per-slug context for the ANNOTATE prompt: example, observed fields, deps, views, drift."""
    refs_by_res: dict[str, list[dict[str, Any]]] = {}
    for e in graph_dict["edges"]:
        if e["kind"] in {"references", "contains"}:
            refs_by_res.setdefault(e["src"], []).append(e)
    out: dict[str, dict[str, Any]] = {}
    for node in graph_dict["nodes"]:
        for op in node["operations"]:
            ep = next((e for e in endpoints if e.slug == op["slug"]), None)
            out[op["slug"]] = {
                "role": op["role"],
                "required_path_params": ep.path_params if ep else [],
                "observed_fields": op.get("observed_fields"),
                "example": graph_dict["examples"].get(op["slug"]),
                "view_variants": op.get("view_variants"),
                "drift": op.get("drift"),
                "relationships": [
                    {"to": e["dst"], "kind": e["kind"], "via": e.get("field")}
                    for e in refs_by_res.get(node["name"], [])
                ],
            }
    return out


# =============================================================================
# Rendering
# =============================================================================

def render_graph_json(graph: StaticGraph) -> dict[str, Any]:
    return graph.to_dict()


def _hierarchy_tree(graph: StaticGraph) -> list[str]:
    contains: dict[str, list[str]] = {}
    children: set[str] = set()
    for e in graph.edges:
        if e["kind"] == "contains":
            contains.setdefault(e["src"], []).append(e["dst"])
            children.add(e["dst"])
    roots = sorted(n for n in graph.nodes if n not in children)
    lines: list[str] = []

    def walk(name: str, depth: int, seen: frozenset[str]) -> None:
        lines.append(f"{'  ' * depth}- {name}")
        if name in seen:
            return
        for c in sorted(contains.get(name, [])):
            walk(c, depth + 1, seen | {name})

    for r in roots:
        walk(r, 0, frozenset())
    return lines


def render_analysis_md(graph: StaticGraph, endpoints: list[Endpoint],
                       probe_out: dict[str, Any] | None = None,
                       spec: dict[str, Any] | None = None) -> str:
    g = build_api_graph(graph, endpoints, probe_out, spec)
    nodes = {n["name"]: n for n in g["nodes"]}
    out: list[str] = ["# API Analysis", ""]
    out.append(f"_{len(endpoints)} endpoints · {len(nodes)} resources · {len(g['edges'])} relationships_")
    if g.get("probe"):
        s = g["probe"]["stats"]
        out.append(f"_probe: {s['network_calls']} calls · {s.get('view_variants', 0)} view variants "
                   f"· {s.get('drift_endpoints', 0)} endpoints with drift_")
    out += ["", "## Resource hierarchy", ""]
    tree = _hierarchy_tree(graph)
    out += tree if tree else ["_(no nesting detected)_"]

    rels = [e for e in g["edges"] if e["kind"] in {"references", "composition"}]
    out += ["", "## Relationships", ""]
    if rels:
        out += ["| from | → | to | kind | evidence |", "|---|---|---|---|---|"]
        for e in rels:
            via = f" (`{e['field']}`)" if e.get("field") else ""
            out.append(f"| {e['src']} | → | {e['dst']} | {e['kind']}{via} | {e['evidence']} |")
    else:
        out.append("_(none detected)_")

    out += ["", "## Operations by resource", ""]
    for name in sorted(nodes):
        ops = nodes[name]["operations"]
        if not ops:
            continue
        seen = " ✓ probed" if nodes[name].get("observed") else ""
        ids = f" · {nodes[name]['id_count']} ids" if nodes[name].get("id_count") else ""
        out.append(f"### {name}{seen}{ids}")
        for op in ops:
            mark = f" → {op['observed_status']}" if op.get("observed_status") else ""
            out.append(f"- `{op['role']}` — {op['method']} {op['path']}{mark}")
            for v in op.get("view_variants", []) or []:
                out.append(f"  - view `{v['param']}={v['value']}`: +{v['added']} -{v['removed']}")
            if op.get("drift"):
                d = op["drift"]
                bits = []
                if d["missing_declared"]:
                    bits.append(f"missing {d['missing_declared']}")
                if d["undeclared_observed"]:
                    bits.append(f"undeclared {d['undeclared_observed']}")
                if d["type_mismatch"]:
                    bits.append(f"type {d['type_mismatch']}")
                out.append(f"  - ⚠ drift: {'; '.join(bits)}")
        out.append("")
    return "\n".join(out) + "\n"


def write_static_artifacts(graph: StaticGraph, endpoints: list[Endpoint], out_dir: Path,
                           probe_out: dict[str, Any] | None = None,
                           spec: dict[str, Any] | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "api_graph.json").write_text(
        json.dumps(build_api_graph(graph, endpoints, probe_out, spec), indent=2, default=str))
    (out_dir / "api_analysis.md").write_text(render_analysis_md(graph, endpoints, probe_out, spec))


def add_probe_args(ap: argparse.ArgumentParser) -> None:
    """Shared --probe-* flags (used by this CLI and openapi_to_tools)."""
    ap.add_argument("--probe", action="store_true", help="Run the read-only live crawl")
    ap.add_argument("--probe-base-url", help="Base URL to crawl (default: spec servers[0])")
    ap.add_argument("--probe-auth", default=os.environ.get("API_AUTH"),
                    help="bearer:TOK | header:K=V | cookie:K=V (env API_AUTH)")
    ap.add_argument("--probe-budget", type=int, default=200, help="Max network calls")
    ap.add_argument("--probe-samples", type=int, default=3, help="Ids tried per parameterized endpoint")
    ap.add_argument("--probe-replay", action="store_true", help="Use cassette only, never touch network")
    ap.add_argument("--probe-cache", type=Path, help="Cassette directory for record/replay")


def probe_config_from_args(args: argparse.Namespace, spec: dict[str, Any],
                           spec_url: str | None) -> ProbeConfig | None:
    if not getattr(args, "probe", False) and not getattr(args, "probe_replay", False):
        return None
    base = args.probe_base_url or _default_base_url(spec, spec_url)
    if not base:
        raise SystemExit("probe: no base url — pass --probe-base-url")
    return ProbeConfig.from_auth(
        base, args.probe_auth,
        budget=args.probe_budget, samples=args.probe_samples,
        replay=args.probe_replay, cassette_dir=args.probe_cache,
    )


def _default_base_url(spec: dict[str, Any], spec_url: str | None) -> str | None:
    servers = spec.get("servers") or []
    if servers and servers[0].get("url", "").startswith("http"):
        return servers[0]["url"].rstrip("/")
    if spec_url:
        from urllib.parse import urlsplit
        u = urlsplit(spec_url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    return None


# =============================================================================
# Standalone CLI — static graph + optional live probe
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="API discovery: static graph + optional live probe.")
    ap.add_argument("--file", type=Path, required=True, help="Local openapi.json file")
    ap.add_argument("-o", "--out", type=Path, default=Path("./apigen_out"))
    add_probe_args(ap)
    args = ap.parse_args()

    spec = json.loads(args.file.read_text())
    endpoints = parse_endpoints(spec)
    graph = infer_static_graph(endpoints, spec)

    probe_out = None
    cfg = probe_config_from_args(args, spec, None)
    if cfg:
        print(f"Probing {cfg.base_url} (read-only) ...", file=sys.stderr)
        probe_out = run_probe(endpoints, graph, cfg, spec=spec)

    write_static_artifacts(graph, endpoints, args.out, probe_out, spec)

    extra = ""
    if probe_out:
        s = probe_out["stats"]
        extra = f", {s['network_calls']} probe calls, {s.get('view_variants', 0)} variants, {s.get('drift_endpoints', 0)} drift"
    print(
        f"[ok] {len(endpoints)} endpoints → {len(graph.nodes)} resources, "
        f"{len(graph.edges)} relationships{extra} → {args.out}/api_graph.json, api_analysis.md",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
