"""Milestone 6 PoC — fold runtime observations into static facts to *sharpen* the graph.

Static analysis honestly stops at deep external calls (E4) and object-level PII taint (E6). Runtime
(DB query logs / OpenTelemetry spans) sees what the app actually did. This merges the two under one
rule, the same "never render not-observed as doesn't-exist" principle the whole product is built on:

    runtime CONFIRMS a suspected edge  (⚠/? → ✓, with trace count)   — upgrade
    runtime REVEALS an unseen edge      (adds an item static missed)  — add
    runtime is SILENT on a static edge  (path not exercised)          — keep it, annotate, NEVER drop

So a merged edge carries both provenances; runtime is additive/confirmatory, never authoritative.

Input `runtime` is a flat list of route-keyed observations (see M6_runtime.md for the contract):
    {"route","method","kind":"db|external|async|pii-egress","target","op"?,"field"?,"n"}
Operates on the `facts.json` dict shape (a list of `to_dict(Endpoint)` dicts); returns a new list.
"""
from __future__ import annotations

import copy

VERIFIED, POTENTIAL, UNKNOWN, NA = "✓", "⚠", "?", "n/a"

_KIND_EDGE = {"db": "e3_db_tables", "external": "e4_external",
              "async": "e5_async", "pii-egress": "e6_pii"}


def _target_of(item: str) -> str:
    """The resolved fact from an edge item string: `Order:write @ f:1` → `Order:write`,
    `requests.post -> host @ f:1` → `requests.post -> host`. Location-stripped for comparison."""
    return item.split(" @ ", 1)[0].strip()


def _confirms(item: str, obs: dict, tables: dict = None) -> bool:
    """Does a runtime observation confirm this static edge item? Route already matched upstream.
    The subtlety: runtime DB logs name the *table* (`order_items`) while static facts name the
    *model* (`OrderItem`) — so for db edges we bridge via the endpoint's model→table map before
    comparing. External/PII edges compare on host + field directly."""
    fact = _target_of(item).lower()
    tgt = str(obs.get("target", "")).lower()
    if not tgt:
        return False
    if obs["kind"] == "db":
        model = _target_of(item).split(":", 1)[0]
        tbl = ((tables or {}).get(model) or {}).get("table", "")
        return tgt in {model.lower(), tbl.lower()} or tgt in fact
    if obs["kind"] == "pii-egress":
        field = str(obs.get("field", "")).lower()
        return (field in fact and tgt in fact) if field else tgt in fact
    return tgt in fact


def merge(facts: list, runtime: list, min_confirm: int = 1) -> list:
    """Return a sharpened copy of `facts` with runtime observations folded in. `min_confirm` is the
    trace-count floor below which an observation is treated as noise (default 1 = any sighting)."""
    out = copy.deepcopy(facts)
    by_route: dict = {}
    for o in runtime:
        if o.get("n", 1) >= min_confirm and o.get("kind") in _KIND_EDGE:
            by_route.setdefault(o["route"], []).append(o)

    for ep in out:
        obs_list = by_route.get(ep.get("route"), [])
        if not obs_list:
            # no runtime coverage for this endpoint — annotate honestly, change nothing else
            continue
        for obs in obs_list:
            edge_key = _KIND_EDGE[obs["kind"]]
            edge = ep.get(edge_key) or {"status": NA, "items": [], "note": ""}
            ep[edge_key] = edge
            n = obs.get("n", 1)
            confirmed = [it for it in edge.get("items", []) if _confirms(it, obs, ep.get("tables"))]
            if confirmed:
                # UPGRADE: a suspected edge is now runtime-proven. Bump ⚠/? → ✓, keep provenance.
                if edge.get("status") in (POTENTIAL, UNKNOWN):
                    edge["status"] = VERIFIED
                edge["note"] = _append_note(edge.get("note", ""),
                                            f"runtime-confirmed ({n} traces)")
            else:
                # ADD: runtime saw an edge static analysis missed (e.g. call 3 hops deep).
                label = _runtime_item(obs)
                edge.setdefault("items", []).append(label)
                if edge.get("status") in (NA, None):
                    edge["status"] = VERIFIED
                edge["note"] = _append_note(edge.get("note", ""),
                                            f"runtime-only edge added ({n} traces)")
    return out


def _runtime_item(obs: dict) -> str:
    """Render a runtime-only observation as an edge item, tagged so provenance is never lost."""
    if obs["kind"] == "db":
        return f"{obs['target']}:{obs.get('op', 'read')} @ runtime (runtime-only)"
    if obs["kind"] == "external":
        return f"runtime -> {obs['target']} @ runtime (runtime-only)"
    if obs["kind"] == "pii-egress":
        return f"{obs.get('field', 'pii')} -> {obs['target']} @ runtime (runtime-traced)"
    return f"{obs.get('target', '?')} @ runtime (runtime-only)"


def _append_note(note: str, add: str) -> str:
    add = add.strip()
    if not note:
        return add
    return note if add in note else f"{note}; {add}"
