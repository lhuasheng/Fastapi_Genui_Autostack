"""Milestone 4 — discovery wired into the generator. Offline (stub LLM + cassette replay)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend_toolgen.api_probe import ProbeConfig  # noqa: E402
from backend_toolgen.openapi_to_tools import LLMClient, build_plan, write_outputs  # noqa: E402

HERE = Path(__file__).parent
SPEC = json.loads((HERE / "fixtures" / "petstore_nested.json").read_text())


def _plan(tmp_cache: Path, probe: bool):
    llm = LLMClient(model="stub", cache_dir=tmp_cache, stub=True)
    cfg = None
    if probe:
        cfg = ProbeConfig.from_auth(
            "http://127.0.0.1:8077", "bearer:testtoken",
            cassette_dir=HERE / "fixtures" / "cassettes_m3", samples=2, budget=0, replay=True)
    return build_plan(SPEC, "fixture", llm, probe_cfg=cfg)


def test_build_plan_static_only(tmp_path=None):
    import tempfile
    cache = Path(tempfile.mkdtemp())
    plan = _plan(cache, probe=False)
    assert plan["probe_stats"] is None
    # relationships derived statically even without probing
    kinds = {(r["from"], r["to"], r["kind"]) for r in plan["relationships"]}
    assert ("users", "orders", "contains") in kinds
    assert ("orders", "users", "references") in kinds
    assert plan["_api_graph"]["probe"] is None


def test_build_plan_with_probe_and_outputs():
    import tempfile
    cache = Path(tempfile.mkdtemp())
    plan = _plan(cache, probe=True)
    assert plan["probe_stats"]["network_calls"] == 0
    assert plan["probe_stats"]["view_variants"] == 1
    assert plan["probe_stats"]["drift_endpoints"] == 1
    # enriched graph carries examples + per-op evidence
    g = plan["_api_graph"]
    assert "get_user" in g["examples"]
    getu = next(op for n in g["nodes"] for op in n["operations"] if op["slug"] == "get_user")
    assert getu["drift"]["missing_declared"] == ["address"]
    assert getu["view_variants"][0]["added"] == ["address"]

    # write_outputs emits discovery artifacts alongside the generated toolset
    out = Path(tempfile.mkdtemp())
    write_outputs(plan, out)
    for f in ("api_graph.json", "api_analysis.md", "tools.json", "plan.json"):
        assert (out / f).exists(), f
    # no PII leaks into artifacts
    assert "ada@example.com" not in (out / "api_graph.json").read_text()


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
