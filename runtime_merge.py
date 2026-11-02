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


def from_contract(observed: dict) -> list:
    """lenscheck-contract's record-mode output (`guard.suggestions()`: {handler: [effect, ...]}) as runtime
    observations for `merge`. Contract handlers are `<module>.<qualname>`, so these carry `handler`
    instead of `route` and match endpoints by name + module. `net:<host>` and `email` become external
    edges; `cache:*` has no runtime edge here. The guard records effects, not counts, so n = 1."""
    out = []
    for handler, effects in (observed or {}).items():
        if handler.startswith("<") or not isinstance(effects, list):    # "<no handler>" = unattributed
            continue
        for eff in effects:
            if isinstance(eff, str) and eff.startswith("net:"):
                out.append({"handler": handler, "kind": "external", "target": eff[4:], "n": 1})
            elif eff == "email":
                out.append({"handler": handler, "kind": "external", "target": "email", "n": 1})
    return out


def _same_handler(ep: dict, handler: str) -> bool:
    """Contract `orders.views.CreateOrder` (or `...CreateOrder.post`) ↔ static endpoint `CreateOrder` (or
    `CreateOrder.post`) declared in `orders/views.py`. The module must agree as a dotted suffix either way
    (import roots differ, e.g. a `src/` layout), so two same-named views in different apps never swap
    effects. No static file → name only."""
    static = str(ep.get("handler", "")).split(".", 1)[0]
    f = str(ep.get("file", "")).replace("\\", "/")
    smod = f[:-3].replace("/", ".") if f.endswith(".py") else ""
    parts = handler.split(".")
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] != static:
            continue
        mod = ".".join(parts[:i])
        if not smod or smod == mod or smod.endswith("." + mod) or mod.endswith("." + smod):
            return True
    return False


def merge(facts: list, runtime: list, min_confirm: int = 1) -> list:
    """Return a sharpened copy of `facts` with runtime observations folded in. `min_confirm` is the
    trace-count floor below which an observation is treated as noise (default 1 = any sighting).
    Observations are keyed by `route`, or by `handler` (see `from_contract`)."""
    out = copy.deepcopy(facts)
    by_route: dict = {}
    by_handler: list = []
    for o in runtime:
        if o.get("n", 1) >= min_confirm and o.get("kind") in _KIND_EDGE:
            if o.get("route") is not None:
                by_route.setdefault(o["route"], []).append(o)
            elif o.get("handler"):
                by_handler.append(o)

    for ep in out:
        obs_list = by_route.get(ep.get("route"), []) + \
            [o for o in by_handler if _same_handler(ep, o["handler"])]
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


_EDGE_LABEL = {"e3_db_tables": "DB", "e4_external": "external call", "e5_async": "async dispatch",
               "e6_pii": "PII egress"}


def load_observations(data) -> list:
    """A runtime file's content: lenscheck-contract's record output ({handler: [effect, ...]}) or a list of
    route-keyed observations. Returns the list `merge` takes."""
    if isinstance(data, dict):
        return from_contract(data)
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict) and o.get("kind") in _KIND_EDGE]
    raise ValueError("runtime file must be a JSON object (lenscheck-contract record output) or a list")


def evidence(facts: list, runtime: list, routes=None) -> list:
    """What runtime adds, per endpoint edge: a static ⚠/? it confirmed, or items only runtime saw.
    `routes` limits it to those endpoints (e.g. the ones a PR changed)."""
    out = []
    for before, after in zip(facts, merge(facts, runtime)):
        if routes is not None and before.get("route") not in routes:
            continue
        for key in _KIND_EDGE.values():
            b, a = before.get(key) or {}, after.get(key) or {}
            if a == b:
                continue
            out.append({"route": before.get("route"), "handler": before.get("handler"), "edge": key,
                        "before": b.get("status", NA), "after": a.get("status", NA),
                        "runtime_only": [i for i in a.get("items", []) if i not in (b.get("items") or [])]})
    return out


def render_section(ev: list, n_obs: int) -> str:
    """The review section. Empty when runtime adds nothing to the changed endpoints."""
    if not ev:
        return ""
    L = ["## 🔬 Runtime evidence", "",
         f"{n_obs} runtime observation(s) checked against the changed endpoints. Runtime only ever adds or "
         "confirms: an edge it didn't see is kept, because the path may just not have run.", ""]
    for x in ev:
        what = _EDGE_LABEL.get(x["edge"], x["edge"])
        if x["runtime_only"]:
            seen = ", ".join(f"`{i.split(' @ ')[0]}`" for i in x["runtime_only"])
            L.append(f"- `{x['route']}` ({x['handler']}): {what} seen only at runtime: {seen}")
        else:
            L.append(f"- `{x['route']}` ({x['handler']}): {what} {x['before']} → {x['after']} confirmed by runtime")
    L.append("")
    return "\n".join(L)
