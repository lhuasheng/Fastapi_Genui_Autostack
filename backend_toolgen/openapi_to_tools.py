#!/usr/bin/env python3
"""
OpenAPI → AI Tools (generic).

Reads any FastAPI/OpenAPI 3.x spec and emits a layered AI toolset:

  <output_dir>/
    plan.json              full plan (groups, hierarchy, actions, LLM trace)
    tools.json             Anthropic-style tool-use specs (flat list)
    skills/<group>/
        SKILL.md           skill description + action catalog
        tool.py            async action dispatcher (one fn per group)
    cli.py                 typer CLI binding all groups as subcommands
    _api_client.py         shared async httpx client

Pipeline:
  1. EXTRACT  – fetch /openapi.json, parse endpoints + schemas.
  2. CLUSTER  – group by OpenAPI tags (fallback: by path prefix when no tags).
  3. SHAPE    – LLM call: should these groups be flat or hierarchical?
                          (with zoom levels above the tag groups)
  4. ANNOTATE – LLM call per group: action names + docstrings + which to exclude.
  5. RENDER   – write SKILL.md, tool.py, tools.json, cli.py.

LLM provider:
  - litellm (any model). Set LITELLM_MODEL env var or pass --model.
  - Or --stub to use a deterministic StubLLM (no API key needed). Useful for
    keyless development and testing.
  - SHA-256 cache at ~/.cache/apigen/ so re-runs against the same spec are free.

Usage:
    python openapi_to_tools.py --url http://localhost:8000/api/openapi.json -o ./out
    python openapi_to_tools.py --file openapi.json -o ./out --stub          # no LLM
    python openapi_to_tools.py --url http://localhost:8000/api/openapi.json \\
        -o ./out --model anthropic/claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import api_probe
from openapi_model import Endpoint, _snake, fetch_spec, parse_endpoints

CACHE_DIR = Path(os.environ.get("APIGEN_CACHE", Path.home() / ".cache" / "apigen"))
DEFAULT_MODEL = os.environ.get("LITELLM_MODEL", "anthropic/claude-sonnet-4-6")


# =============================================================================
# 2. CLUSTER (tag-first, path-prefix fallback)
# =============================================================================

@dataclass
class Group:
    """A flat tag-derived group of endpoints."""
    name: str          # e.g. "users"
    endpoints: list[Endpoint] = field(default_factory=list)
    source: str = "tag"  # "tag" or "prefix"


def cluster_by_tags(endpoints: list[Endpoint]) -> tuple[list[Group], list[Endpoint]]:
    """Group endpoints by their first tag. Untagged endpoints fall through."""
    by_tag: dict[str, Group] = {}
    untagged: list[Endpoint] = []
    for ep in endpoints:
        if not ep.tags:
            untagged.append(ep)
            continue
        tag = _snake(ep.tags[0])
        by_tag.setdefault(tag, Group(name=tag, source="tag")).endpoints.append(ep)
    return list(by_tag.values()), untagged


def cluster_untagged_by_prefix(untagged: list[Endpoint]) -> list[Group]:
    """For endpoints without tags, fall back to clustering by first path segment.

    /api/foo/bar  → group "foo"
    /users/{id}   → group "users"
    """
    by_prefix: dict[str, Group] = {}
    for ep in untagged:
        parts = [p for p in ep.path.split("/") if p and not p.startswith("{")]
        # Skip generic prefixes like "api", "v1"
        cleaned = [p for p in parts if not re.fullmatch(r"v\d+|api", p, re.I)]
        prefix = cleaned[0] if cleaned else (parts[0] if parts else "root")
        prefix = _snake(prefix)
        by_prefix.setdefault(prefix, Group(name=prefix, source="prefix")).endpoints.append(ep)
    return list(by_prefix.values())


def cluster(endpoints: list[Endpoint]) -> list[Group]:
    tagged, untagged = cluster_by_tags(endpoints)
    return tagged + cluster_untagged_by_prefix(untagged)


# =============================================================================
# 3 & 4. LLM PHASES (shape + annotate)
# =============================================================================

class LLMClient:
    """Thin wrapper over litellm with SHA-256 disk cache."""

    def __init__(self, model: str, cache_dir: Path = CACHE_DIR, stub: bool = False):
        self.model = model
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stub = stub
        self.calls: list[dict[str, Any]] = []  # trace, written into plan.json

    def chat_json(self, system: str, user: str, *, label: str) -> dict[str, Any]:
        key = hashlib.sha256(
            f"{self.model}\n{system}\n{user}".encode("utf-8")
        ).hexdigest()
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            self.calls.append({"label": label, "cached": True, "key": key})
            return data

        if self.stub:
            data = _stub_response(label, system, user)
        else:
            data = self._real_call(system, user)

        cache_file.write_text(json.dumps(data, indent=2))
        self.calls.append({"label": label, "cached": False, "key": key})
        return data

    def _real_call(self, system: str, user: str) -> dict[str, Any]:
        try:
            import litellm  # type: ignore
        except ImportError:
            raise SystemExit(
                "litellm not installed. `pip install litellm`, or use --stub."
            )
        resp = litellm.completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content  # type: ignore
        return json.loads(content)


def _stub_response(label: str, system: str, user: str) -> dict[str, Any]:
    """Deterministic dummy LLM output. Echoes the input back in expected shape.

    Lets the rest of the pipeline run end-to-end without a key. Good enough
    for generating scaffolds you'll edit anyway.
    """
    if label == "shape":
        # Default: keep it flat. The user can override later.
        try:
            payload = json.loads(_extract_json_block(user))
            groups = payload.get("groups", [])
            return {
                "shape": "flat",
                "rationale": "STUB: defaulting to flat. Run with --model to get LLM judgement.",
                "levels": [],
                "flat_groups": [g["name"] for g in groups],
            }
        except Exception:
            return {"shape": "flat", "rationale": "STUB", "levels": [], "flat_groups": []}
    if label == "annotate":
        try:
            payload = json.loads(_extract_json_block(user))
            eps = payload.get("endpoints", [])
            return {
                "actions": [
                    {
                        "name": ep.get("slug", f"action_{i}"),
                        "endpoint_index": i,
                        "docstring": ep.get("summary") or f"{ep.get('method')} {ep.get('path')}",
                        "exclude": False,
                    }
                    for i, ep in enumerate(eps)
                ],
                "skill_description": f"STUB skill for {payload.get('group_name')}",
            }
        except Exception:
            return {"actions": [], "skill_description": "STUB"}
    return {}


def _extract_json_block(s: str) -> str:
    """Pull the first ```json ... ``` block out of a prompt, or return the whole thing."""
    m = re.search(r"```json\s*(.*?)\s*```", s, re.DOTALL)
    return m.group(1) if m else s


_SHAPE_SYSTEM = dedent("""\
    You are designing the tool surface an AI agent will use to interact with a backend API.
    You receive a list of endpoint groups (already clustered by OpenAPI tag or path prefix).
    Decide whether the agent sees these groups as FLAT (just a list of skills) or as a
    HIERARCHICAL "zoom" structure (e.g. overview → detail → raw).

    Rules:
    - Prefer FLAT unless there is a clear zoom relationship between groups.
    - HIERARCHICAL is justified when groups form a natural drill-down: e.g. summary endpoints
      vs. per-entity detail endpoints vs. per-entity raw signal/event endpoints.
    - Use `relationships` as primary evidence: a `contains` edge (parent → child resource,
      e.g. users → orders) is a strong signal for a hierarchical drill-down; many `references`
      edges between peers suggest a flat web instead. Prefer this structural evidence over names.
    - Levels are 1-indexed; level 1 = highest-level / most aggregated.
    - You may group multiple tag-groups into one zoom level.

    Output JSON only, matching this schema:
    {
      "shape": "flat" | "hierarchical",
      "rationale": str,                              // 1-2 sentences
      "levels": [                                    // empty if flat
        {"level": int, "name": str, "zoom": str, "groups": [str]}
      ],
      "flat_groups": [str]                           // empty if hierarchical
    }
""")

_ANNOTATE_SYSTEM = dedent("""\
    You are naming actions for a single group of API endpoints that will become one AI tool.
    The tool dispatches via an `action` string parameter.

    For each endpoint, propose:
    - A short snake_case action name (verb-first when reasonable: list_users, get_user, create_order).
    - A one-line docstring (what it does, not how).
    - Whether to EXCLUDE it from the agent's toolset (admin endpoints, internal health checks,
      duplicates, deprecated endpoints — exclude these).

    When an endpoint has a `discovered` block (from a live read-only probe), use it:
    - `observed_fields` / `example` show the REAL response shape — ground the docstring in these.
    - `required_path_params` + `relationships` reveal that a call needs an id obtained from
      another action first (e.g. get an order id from list_orders); mention such prerequisites.
    - `view_variants` show how a query param reshapes the response; note useful ones.
    - `drift` flags where the spec and reality disagree; prefer the observed reality.

    Also write a "skill_description" (~30-80 words) for the SKILL.md frontmatter — describing
    when an AI agent should invoke this skill. Lead with concrete triggers ("Use when the user
    asks about X, Y, or Z").

    Output JSON only:
    {
      "actions": [
        {"name": str, "endpoint_index": int, "docstring": str, "exclude": bool}
      ],
      "skill_description": str
    }
""")


def llm_shape(groups: list[Group], llm: LLMClient,
              relationships: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = {
        "groups": [
            {
                "name": g.name,
                "source": g.source,
                "endpoint_count": len(g.endpoints),
                "sample_paths": [ep.path for ep in g.endpoints[:6]],
                "sample_summaries": [ep.summary for ep in g.endpoints[:6] if ep.summary],
            }
            for g in groups
        ],
        # Real parent/child + reference evidence (from static analysis + live probe),
        # so the flat-vs-hierarchical call uses structure, not just tag names.
        "relationships": relationships or [],
    }
    user = f"Here are the clustered groups:\n\n```json\n{json.dumps(payload, indent=2)}\n```"
    return llm.chat_json(_SHAPE_SYSTEM, user, label="shape")


def llm_annotate(group: Group, llm: LLMClient,
                 enrich: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    enrich = enrich or {}
    payload = {
        "group_name": group.name,
        "endpoints": [
            {
                "endpoint_index": i,
                "slug": ep.slug,
                "method": ep.method,
                "path": ep.path,
                "summary": ep.summary,
                "description": (ep.description or "")[:300],
                "path_params": ep.path_params,
                "query_params": [p["name"] for p in ep.query_params],
                "has_body": ep.request_body is not None,
                "deprecated": ep.deprecated,
                # Live evidence (present only when probing ran): real fields, an example
                # response, view variants, required-id deps, schema drift, relationships.
                "discovered": enrich.get(ep.slug),
            }
            for i, ep in enumerate(group.endpoints)
        ],
    }
    user = f"Group `{group.name}`:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return llm.chat_json(_ANNOTATE_SYSTEM, user, label="annotate")


# =============================================================================
# 5. RENDER
# =============================================================================

_API_CLIENT_PY = dedent('''\
    """Shared async httpx client used by all generated skill modules."""
    from __future__ import annotations
    import os
    import httpx

    BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
    TIMEOUT  = float(os.environ.get("API_TIMEOUT", "30"))

    async def api_request(method: str, path: str, *, params=None, json_body=None) -> dict | list:
        url = f"{BASE_URL.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.request(method, url, params=params, json=json_body)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                return r.json()
            return {"text": r.text, "status": r.status_code}

    async def api_get(path: str, *, params=None):
        return await api_request("GET", path, params=params)
''')


def render_tool_py(group: Group, annot: dict[str, Any]) -> str:
    """One async dispatcher function per group, action-routed.

    We build the source with explicit indentation (no textwrap.dedent) because
    dedent's common-prefix calc breaks when interpolated multi-line strings
    have their own leading whitespace.
    """
    actions = [a for a in annot.get("actions", []) if not a.get("exclude")]
    if not actions:
        return ""

    fn_name = _snake(group.name)
    action_names = [a["name"] for a in actions]
    default = action_names[0]

    lines: list[str] = []
    lines.append(f'"""Auto-generated skill: {group.name}."""')
    lines.append("from __future__ import annotations")
    lines.append("import json")
    lines.append("from .._api_client import api_request")
    lines.append("")
    lines.append("")
    lines.append(f'async def {fn_name}(action: str = "{default}", **kwargs):')
    # docstring
    lines.append(f'    """{group.name} — action-dispatched API tool.')
    lines.append("")
    lines.append("    Actions:")
    for a in actions:
        lines.append(f"      {a['name']:<28} — {a['docstring']}")
    lines.append("")
    lines.append(f"    Parameters:")
    lines.append(f"      action — one of the above (default: {default!r})")
    lines.append(f"      **kwargs — path params, query params, and `body` for write actions")
    lines.append('    """')

    # dispatch body
    for a in actions:
        ep = group.endpoints[a["endpoint_index"]]
        name = a["name"]
        doc = a["docstring"].replace('"', "'")

        # path expression
        if ep.path_params:
            fpath = ep.path
            for p in ep.path_params:
                fpath = fpath.replace(f"{{{p}}}", f"{{kwargs.get('{p}', '')}}")
            path_expr = f'f"{fpath}"'
        else:
            path_expr = repr(ep.path)

        # kwargs to api_request
        extra_kw = ""
        query_names = [p["name"] for p in ep.query_params]
        if query_names:
            params_dict = ", ".join(f'"{n}": kwargs.get("{n}")' for n in query_names)
            extra_kw += f", params={{{params_dict}}}"
        if ep.request_body is not None and ep.method in {"POST", "PUT", "PATCH"}:
            extra_kw += ', json_body=kwargs.get("body")'

        lines.append(f'    if action == "{name}":')
        lines.append(f"        # {doc}")
        lines.append(f"        # {ep.method} {ep.path}")
        lines.append(f'        return await api_request("{ep.method}", {path_expr}{extra_kw})')

    actions_list_repr = json.dumps(action_names)
    lines.append(f'    return {{"error": f"unknown action: {{action}}", "available": {actions_list_repr}}}')
    lines.append("")
    return "\n".join(lines)


def render_skill_md(group: Group, annot: dict[str, Any]) -> str:
    actions = [a for a in annot.get("actions", []) if not a.get("exclude")]
    desc = annot.get("skill_description", f"Tool for the {group.name} API group.")
    rows = "\n".join(
        f"- `{a['name']}` — {a['docstring']}" for a in actions
    )
    excluded = [a for a in annot.get("actions", []) if a.get("exclude")]
    excluded_section = ""
    if excluded:
        ex_rows = "\n".join(
            f"- `{a['name']}` — {a['docstring']}" for a in excluded
        )
        excluded_section = f"\n## Excluded (not exposed)\n\n{ex_rows}\n"

    return dedent(f"""\
        ---
        name: {group.name}
        description: |
          {desc}
        ---

        # {group.name}

        {desc}

        ## Actions

        {rows}
        {excluded_section}
        ## Usage

        Call `{_snake(group.name)}(action="<one of the above>", **kwargs)`.
        Path params and query params go in `kwargs`. For POST/PUT/PATCH, pass `body=<dict>`.
        """)


def render_tools_json(groups_with_annot: list[tuple[Group, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Anthropic tool-use format. One tool per group (action-dispatched)."""
    tools: list[dict[str, Any]] = []
    for group, annot in groups_with_annot:
        actions = [a for a in annot.get("actions", []) if not a.get("exclude")]
        if not actions:
            continue
        action_names = [a["name"] for a in actions]
        # Union of all path/query params across the group's endpoints
        all_props: dict[str, Any] = {
            "action": {
                "type": "string",
                "enum": action_names,
                "description": "Which action to perform within this group.",
            }
        }
        for a in actions:
            ep = group.endpoints[a["endpoint_index"]]
            for p in ep.path_params:
                all_props.setdefault(p, {"type": "string", "description": f"Path param for some actions."})
            for q in ep.query_params:
                all_props.setdefault(q["name"], {
                    "type": q.get("type") or "string",
                    "description": q.get("description") or "",
                })
        tools.append({
            "name": _snake(group.name),
            "description": annot.get("skill_description", f"{group.name} API"),
            "input_schema": {
                "type": "object",
                "properties": all_props,
                "required": ["action"],
            },
        })
    return tools


def render_cli_py(groups_with_annot: list[tuple[Group, dict[str, Any]]]) -> str:
    """A typer CLI that exposes each group as a subcommand."""
    active = [
        (g, annot) for g, annot in groups_with_annot
        if [a for a in annot.get("actions", []) if not a.get("exclude")]
    ]

    lines: list[str] = []
    lines.append('"""Auto-generated CLI binding for the API toolset.')
    lines.append("")
    lines.append("Usage:")
    lines.append("    python cli.py <group> <action> key=value key=value ...")
    lines.append("    python cli.py users get_user user_id=42")
    lines.append('"""')
    lines.append("from __future__ import annotations")
    lines.append("import asyncio, json")
    lines.append("import typer")
    for g, _ in active:
        fn = _snake(g.name)
        lines.append(f"from skills.{fn}.tool import {fn}")
    lines.append("")
    lines.append("app = typer.Typer(no_args_is_help=True)")
    lines.append("")

    for g, _ in active:
        fn = _snake(g.name)
        lines.append(f'@app.command("{fn}")')
        lines.append(f"def _cmd_{fn}(action: str, kv: list[str] = typer.Argument(None)):")
        lines.append(f'    """Invoke the {g.name} skill. Pass kwargs as key=value pairs."""')
        lines.append('    kwargs = dict(p.split("=", 1) for p in (kv or []))')
        lines.append(f"    result = asyncio.run({fn}(action=action, **kwargs))")
        lines.append("    typer.echo(json.dumps(result, indent=2, default=str))")
        lines.append("")

    lines.append('if __name__ == "__main__":')
    lines.append("    app()")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Driver
# =============================================================================

def build_plan(
    spec: dict[str, Any],
    source: str,
    llm: LLMClient,
    probe_cfg: Any | None = None,
) -> dict[str, Any]:
    endpoints = parse_endpoints(spec)
    graph = api_probe.infer_static_graph(endpoints, spec)

    probe_out = None
    if probe_cfg is not None:
        print(f"Probing {probe_cfg.base_url} (read-only) ...", file=sys.stderr)
        probe_out = api_probe.run_probe(endpoints, graph, probe_cfg, spec=spec)

    api_graph = api_probe.build_api_graph(graph, endpoints, probe_out, spec)
    enrich = api_probe.annotation_enrichment(api_graph, endpoints)
    relationships = [{"from": e["src"], "to": e["dst"], "kind": e["kind"]}
                     for e in api_graph["edges"]]

    groups = cluster(endpoints)
    shape = llm_shape(groups, llm, relationships=relationships)

    annotations: list[tuple[Group, dict[str, Any]]] = []
    for g in groups:
        annot = llm_annotate(g, llm, enrich=enrich)
        annotations.append((g, annot))

    return {
        "source": source,
        "endpoint_count": len(endpoints),
        "group_count": len(groups),
        "shape": shape,
        "relationships": relationships,
        "probe_stats": api_graph.get("probe", {}).get("stats") if api_graph.get("probe") else None,
        "groups": [
            {
                "name": g.name,
                "source": g.source,
                "endpoints": [asdict(ep) for ep in g.endpoints],
                "annotation": annot,
            }
            for g, annot in annotations
        ],
        "llm_trace": llm.calls,
        "_annotations": annotations,  # popped before serialization
        "_api_graph": api_graph,      # popped; written to api_graph.json
        "_api_analysis_md": api_probe.render_analysis_md(graph, endpoints, probe_out, spec),
    }


def write_outputs(plan: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    annotations = plan.pop("_annotations")
    api_graph = plan.pop("_api_graph", None)
    api_analysis_md = plan.pop("_api_analysis_md", None)

    # Discovery artifacts (api_graph.json + api_analysis.md)
    if api_graph is not None:
        (out_dir / "api_graph.json").write_text(json.dumps(api_graph, indent=2, default=str))
    if api_analysis_md is not None:
        (out_dir / "api_analysis.md").write_text(api_analysis_md)

    # _api_client.py lives alongside the skills package so relative imports work
    skills_dir = out_dir / "skills"
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / "__init__.py").write_text("")
    (skills_dir / "_api_client.py").write_text(_API_CLIENT_PY)
    for g, annot in annotations:
        gdir = skills_dir / _snake(g.name)
        gdir.mkdir(exist_ok=True)
        (gdir / "SKILL.md").write_text(render_skill_md(g, annot))
        tool_src = render_tool_py(g, annot)
        if tool_src:
            (gdir / "tool.py").write_text(tool_src)
            (gdir / "__init__.py").write_text(f"from .tool import {_snake(g.name)}\n")
        else:
            (gdir / "__init__.py").write_text("")

    # tools.json
    (out_dir / "tools.json").write_text(
        json.dumps(render_tools_json(annotations), indent=2)
    )

    # cli.py
    (out_dir / "cli.py").write_text(render_cli_py(annotations))

    # plan.json (serializable view)
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL to fetch openapi.json from")
    src.add_argument("--file", type=Path, help="Local openapi.json file")
    ap.add_argument("-o", "--out", type=Path, default=Path("./apigen_out"),
                    help="Output directory (default: ./apigen_out)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"litellm model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--stub", action="store_true",
                    help="Use deterministic StubLLM — no API key needed.")
    ap.add_argument("--no-cache", action="store_true", help="Bypass the SHA-256 disk cache")

    api_probe.add_probe_args(ap)
    args = ap.parse_args()

    if args.url:
        print(f"Fetching {args.url} ...", file=sys.stderr)
        spec = fetch_spec(args.url)
        source = args.url
    else:
        spec = json.loads(args.file.read_text())
        source = str(args.file)

    probe_cfg = api_probe.probe_config_from_args(args, spec, args.url)

    cache_dir = CACHE_DIR
    if args.no_cache:
        # Use a one-shot temp dir
        import tempfile
        cache_dir = Path(tempfile.mkdtemp(prefix="apigen_nocache_"))

    llm = LLMClient(model=args.model, cache_dir=cache_dir, stub=args.stub)
    plan = build_plan(spec, source, llm, probe_cfg=probe_cfg)

    write_outputs(plan, args.out)

    n_groups = plan["group_count"]
    n_eps = plan["endpoint_count"]
    shape = plan["shape"].get("shape")
    ps = plan.get("probe_stats")
    probe_note = (f" · probe: {ps['network_calls']} calls, {ps.get('view_variants', 0)} variants, "
                  f"{ps.get('drift_endpoints', 0)} drift") if ps else ""
    print(
        f"\n[ok] {n_eps} endpoints → {n_groups} groups → shape={shape}{probe_note}\n"
        f"     written to {args.out}/ (+ api_graph.json, api_analysis.md)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
