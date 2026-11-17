#!/usr/bin/env python3
"""`lenscheck flow` — the ordered flow of one HTTP route / management command / celery task / signal.

Resolve a query against the repo's entrypoints, re-derive the chosen entrypoint, and print the steps it
runs in order: DB reads/writes (with literal `where`/`set` values), external calls, async dispatches,
helper calls (nested), early exits — each with the `if`/`for`/`try` conditions guarding it. Pure stdlib,
no LLM; facts come from the same extractor the rest of lenscheck uses.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

from extractor.analyzer import RepoIndex, parse_endpoints, analyze_handler, job_entrypoints
from extractor.trace import trace


class Ambiguous(Exception):
    """More than one entrypoint could match; `ranked` carries the best candidates."""

    def __init__(self, ranked):
        self.ranked = ranked
        super().__init__("ambiguous query")


def candidates(repo):
    import factindex
    return factindex.load(repo, with_calls=True)["endpoints"]


def _tokens(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return [p.lower() for p in re.split(r"[^A-Za-z0-9]+", text) if len(p) >= 2]


def _tok_match(q, c):
    if q == c:
        return True
    return len(q) >= 3 and len(c) >= 3 and (q.startswith(c) or c.startswith(q))


def _rank(cands, query):
    qtokens = _tokens(query)
    scored = []
    for cand in cands:
        rh = _tokens(cand.get("route", "")) + _tokens(cand.get("handler", ""))
        path = _tokens(cand.get("loc", ""))
        matched, score = set(), 0
        for q in qtokens:
            if any(_tok_match(q, t) for t in rh):
                score += 2
                matched.add(q)
            elif any(_tok_match(q, t) for t in path):
                score += 1
                matched.add(q)
        scored.append((score, len(cand.get("route") or ""), cand, matched))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(s, c, m) for s, _n, c, m in scored], qtokens


def _choose(scored, qtokens, pick):
    ranked = [c for _s, c, _m in scored]
    if pick is not None:
        if 1 <= pick <= len(ranked):
            return ranked[pick - 1]
        raise Ambiguous(ranked[:5])
    if not scored or scored[0][0] <= 0:
        raise Ambiguous([])
    top = scored[0][0]
    tops = [x for x in scored if x[0] == top]
    if len(tops) == 1 and set(qtokens) <= tops[0][2]:
        return tops[0][1]
    raise Ambiguous(ranked[:5])


def resolve(cands, query, pick=None):
    """Pick the one entrypoint `query` names, or raise `Ambiguous` with the ranked candidates."""
    exact = [c for c in cands
             if c.get("handler") == query
             or c.get("route") == query
             or query in (c.get("full_routes") or [])
             or c.get("route") in ("command:" + query, "task:" + query)]
    if len(exact) == 1:
        return exact[0]
    pool = exact if len(exact) > 1 else cands
    scored, qtokens = _rank(pool, query)
    if not exact and not any(s > 0 for s, _c, _m in scored):
        raise Ambiguous([])
    return _choose(scored, qtokens, pick)


def entry_endpoint(repo, cand, index=None):
    """Re-derive the Endpoint object for a candidate, with its `lc_walk` attached."""
    index = index or RepoIndex(repo)
    kind = cand.get("kind", "http")
    route, handler = cand.get("route", ""), cand.get("handler", "")
    # cross-repo hops re-derive many entries on one index: parse the routes / decorator endpoints once
    memo = index.__dict__.setdefault("_flow_entry_memo", {})
    if kind == "http":
        if "routes" not in memo:
            memo["routes"] = parse_endpoints(index)
        for r, h, urls_file in memo["routes"]:
            if (r, h) == (route, handler):
                ep = analyze_handler(handler, index, near=urls_file)
                ep.route = route
                return ep, index
        # decorator routes (Flask/FastAPI): the candidate's loc names its file, so parse just that one
        # instead of frameworks.discover() re-parsing and re-analyzing the whole repo
        try:
            import frameworks
        except ImportError:
            from extractor import frameworks
        path = os.path.join(index.root, cand.get("loc", "").rsplit(":", 1)[0])
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            tree = None
        if tree is not None:
            prefixes, consts = frameworks._prefix_map(tree), index.consts_for(path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == handler:
                    for methods, r in frameworks._routes_of(node, prefixes, consts):
                        if r == route:
                            return frameworks._make_endpoint(node, r, methods, path, index), index
    else:
        for ep in job_entrypoints(index, only=route):
            if ep.handler == handler:
                return ep, index
    return None, index


def build_flow(repo, query, pick=None, keep_all=False, crossrepo_paths=None, follow=None):
    cand = resolve(candidates(repo), query, pick=pick)
    ep, index = entry_endpoint(repo, cand)
    if ep is None:
        raise LookupError(f"entry not re-derivable: {cand.get('route')} -> {cand.get('handler')}")
    tr = trace(ep, index, keep_all=keep_all)
    tables = {}
    for step in _walk_steps(tr["steps"]):
        if step.get("kind") in ("read", "write") and step.get("model"):
            tbl = index.table_for(step["model"])[0]
            if tbl:
                tables[step["model"]] = tbl
    import crossrepo
    flow = {
        "schema": 1,
        "generated_by": "lenscheck flow",
        "repo": os.path.abspath(repo),
        "repo_meta": crossrepo._repo_meta(repo),
        "entry": {"route": ep.route, "handler": ep.handler, "kind": ep.kind,
                  "loc": f"{ep.file}:{ep.line}"},
        "steps": tr["steps"],
        "tables": tables,
        "stats": tr["stats"],
        "hops": [],
    }
    origin, origin_path, origin_cleanup = crossrepo._resolve(repo)
    flow["origin"] = origin
    flow["repo_paths"] = {origin: os.path.abspath(repo)}
    try:
        if crossrepo_paths:
            paths = {origin: origin_path}
            cleanups = []
            try:
                for spec in crossrepo_paths:
                    name, path, cleanup = crossrepo._resolve(spec)
                    paths[name] = path
                    cleanups.append(cleanup)
                    if "@" not in spec:
                        flow["repo_paths"][name] = os.path.abspath(spec)
                per = {name: crossrepo._provenance(path, True, True, worktree=("@" in name))
                       for name, path in paths.items()}
                indexes = {origin: index}
                flow["hops"] = _build_hops(origin, flow, index, per, paths, indexes, follow or [])
                flow["repos"] = {name: crossrepo._repo_meta(path) for name, path in paths.items()}
            finally:
                for cleanup in cleanups:
                    try:
                        cleanup()
                    except Exception:
                        pass
    finally:
        try:
            origin_cleanup()
        except Exception:
            pass
    return flow


def _prefix(steps, hop_id):
    """Deep copy of a step tree with every id/ref namespaced under the hop."""
    import copy
    out = copy.deepcopy(steps)

    def walk(items):
        for step in items:
            if "id" in step:
                step["id"] = f"{hop_id}.{step['id']}"
            if "ref" in step:
                step["ref"] = f"{hop_id}.{step['ref']}"
            walk(step.get("children") or [])
    walk(out)
    return out


def _build_hops(origin, flow, index, per, paths, indexes, follow):
    import crossrepo
    import provenance

    def idx_of(name):
        if name not in indexes:
            indexes[name] = RepoIndex(paths[name])
        return indexes[name]

    def merged_cmap(models, idx):
        out = {}
        for model in models:
            out.update(idx.column_map(model))
        return out

    def col(key, cmap):
        head = key.split("__")[0]
        return cmap.get(head, head)

    def entry_ref(cand):
        return {"route": cand.get("route", ""), "handler": cand.get("handler", ""),
                "kind": cand.get("kind", "http"), "loc": cand.get("loc", "")}

    def find_cand(repo_name, key):
        for e in per.get(repo_name, {}).get("endpoints", []):
            if all(e.get(k, "") == key.get(k, "") for k in ("route", "handler", "loc")):
                return e
        return None

    when_of, writes_by_table = {}, {}

    def scan(steps, chain):              # depth-first in source order, so the first write is the first one run
        for step in steps:
            when = chain + list(step.get("guards") or [])
            when_of[step["id"]] = when
            if step.get("kind") == "write" and step.get("model"):
                tbl = index.table_for(step["model"])[0]
                if tbl and tbl != "<instance>":
                    writes_by_table.setdefault(tbl, []).append(step)
            scan(step.get("children") or [], when)
    scan(flow["steps"], [])

    hops = []
    next_id = [1]
    reader_traces = {}

    def expand(repo_name, cand, hop_id, via, match, extra):
        idx = idx_of(repo_name)
        ep, _ = entry_endpoint(paths[repo_name], cand, index=idx)
        if ep is None:
            return None
        return {"id": hop_id, "via": via, **extra, "repo": repo_name, "match": match,
                "entry": entry_ref(cand), "steps": _prefix(trace(ep, idx)["steps"], hop_id)}

    # http hops
    api_links = crossrepo._api_links(per)
    for link in api_links:
        if link.get("from_repo") != origin or link["from"].get("loc") != flow["entry"]["loc"]:
            continue
        from_step = None
        for step in _walk_steps(flow["steps"]):
            if step.get("kind") == "external" and provenance._call_path(
                    f"{step['text']} @ {step['loc']}") == link["call"]:
                from_step = step
                break
        if from_step is None:
            continue
        cand = find_cand(link["to_repo"], link["to"])
        if cand is None:
            continue
        hop_id = f"h{next_id[0]}"
        hop = expand(link["to_repo"], cand, hop_id, "http", link["match"],
                     {"from_step": from_step["id"], "when": when_of.get(from_step["id"], [])})
        if hop is not None:
            next_id[0] += 1
            hops.append(hop)

    # celery hops
    task_links = crossrepo._task_links(per)
    for link in task_links:
        if link.get("from_repo") != origin or link["from"].get("loc") != flow["entry"]["loc"]:
            continue
        dispatch_loc = link["from"].get("dispatch_loc")
        from_step = None
        for step in _walk_steps(flow["steps"]):
            if step.get("kind") == "async" and step.get("loc") == dispatch_loc:
                from_step = step
                break
        if from_step is None:
            continue
        key = {"handler": link["to"].get("task", ""), "loc": link["to"].get("loc", "")}
        cand = None
        for e in per.get(link["to_repo"], {}).get("endpoints", []):
            if e.get("kind") == "task" and e.get("handler") == key["handler"] \
                    and e.get("loc") == key["loc"]:
                cand = e
                break
        if cand is None:
            continue
        hop_id = f"h{next_id[0]}"
        hop = expand(link["to_repo"], cand, hop_id, "celery", link["match"],
                     {"from_step": from_step["id"], "when": when_of.get(from_step["id"], [])})
        if hop is not None:
            next_id[0] += 1
            hops.append(hop)

    # table hops
    for table in sorted(writes_by_table):
        write_steps = writes_by_table[table]
        for repo_name in sorted(per):
            if repo_name == origin:
                continue
            info = per[repo_name].get("tables", {}).get(table) or {}
            read_by = info.get("read_by")
            if not read_by:
                continue
            cmap_o = merged_cmap(dict.fromkeys(s["model"] for s in write_steps), index)
            cmap_r = merged_cmap(info.get("models") or [], idx_of(repo_name))
            writer_set = {}
            for step in write_steps:
                for k, v in (step.get("set") or {}).items():
                    writer_set[col(k, cmap_o)] = v
            writer_cols = set(writer_set)
            other_writer_cols = {col(f, cmap_r) for f in info.get("written_columns") or []}
            distinctive = writer_cols - other_writer_cols
            reader_keys = {(e.get("route", ""), e.get("handler", ""), e.get("loc", ""))
                           for e in read_by}
            cands = [e for e in per[repo_name].get("endpoints", [])
                     if (e.get("route", ""), e.get("handler", ""), e.get("loc", "")) in reader_keys]
            idx_r = idx_of(repo_name)
            traced = []
            for cand in cands:
                ckey = (repo_name, cand.get("route", ""), cand.get("handler", ""), cand.get("loc", ""))
                if ckey not in reader_traces:    # one reader often reads several of the written tables
                    ep, _ = entry_endpoint(paths[repo_name], cand, index=idx_r)
                    reader_traces[ckey] = (ep, trace(ep, idx_r)["steps"] if ep is not None else None)
                ep, steps = reader_traces[ckey]
                cand_out = {"route": cand.get("route", ""), "handler": cand.get("handler", ""),
                            "loc": cand.get("loc", ""), "kind": cand.get("kind", "http"),
                            "score": [False, 0, 0], "contradicted": False,
                            "reasons": [], "expanded": False}
                if ep is None:
                    cand_out["reasons"].append("not traceable")
                    traced.append((cand, cand_out, None))
                    continue
                on_t =[s for s in _walk_steps(steps)
                        if s.get("model") in (info.get("models") or [])
                        or idx_r.table_for(s.get("model") or "")[0] == table]
                touched = set()
                for step in on_t:
                    for field in ("where", "where_not", "set"):
                        for k in (step.get(field) or {}):
                            touched.add(col(k, cmap_r))
                touched -= {"pk", "id"}
                contradicted, why = False, None
                for step in on_t:
                    for k, v in (step.get("where") or {}).items():
                        c = col(k, cmap_r)
                        if c in writer_set and v != "?" and writer_set[c] != "?" \
                                and v != writer_set[c]:
                            contradicted, why = True, (c, v, writer_set[c])
                            break
                    if contradicted:
                        break
                    for k, v in (step.get("where_not") or {}).items():
                        c = col(k, cmap_r)
                        if c in writer_set and v != "?" and writer_set[c] == v:
                            contradicted, why = True, (c, v, writer_set[c])
                            break
                    if contradicted:
                        break
                reasons = []
                only = sorted(touched & distinctive)
                if only:
                    reasons.append(f"uses {', '.join(only)} — only {origin} is seen writing them")
                shared = sorted((touched & writer_cols) - distinctive)
                if shared:
                    reasons.append(f"shares {', '.join(shared)}")
                if contradicted:
                    c, v, w = why
                    reasons.append(f"✗ filters {c}={v}, this flow writes {c}={w}")
                score = [not contradicted, len(touched & distinctive), len(touched & writer_cols)]
                cand_out.update({"score": score, "contradicted": contradicted, "reasons": reasons})
                traced.append((cand, cand_out, steps))
            traced.sort(key=lambda x: x[1]["route"])
            traced.sort(key=lambda x: x[1]["score"], reverse=True)

            top = traced[0] if traced else None
            forced = next((t for t in traced if f"{repo_name}:{t[1]['route']}" in follow), None)
            chosen = forced
            if chosen is None and top is not None and top[1]["score"][0]:
                if len(traced) == 1 or (top[1]["score"] > traced[1][1]["score"]
                                        and top[1]["score"][1] >= 1):
                    chosen = top
            if chosen is not None and chosen[2] is None:
                chosen = None
            match = None
            if chosen is not None:
                match = "forced" if chosen is forced else "ranked"

            hop_id = f"h{next_id[0]}"
            next_id[0] += 1
            first = write_steps[0]
            hop = {"id": hop_id, "via": "table", "table": table,
                   "from_step": first["id"], "when": when_of.get(first["id"], []),
                   "repo": repo_name, "match": match,
                   "entry": entry_ref(chosen[0]) if chosen is not None else None,
                   "candidates": [t[1] for t in traced]}
            if chosen is not None:
                t_cand, t_out, t_steps = chosen
                t_out["expanded"] = True
                hop["steps"] = _prefix(t_steps, hop_id)
            hops.append(hop)
    return hops



def _walk_steps(steps):
    for step in steps:
        yield step
        yield from _walk_steps(step.get("children") or [])


def _new_phase(head, repo):
    return {"id": None, "repo": repo, "kind": head.get("kind", "entry"),
            "title": head.get("title", ""), "via": head.get("via", ""),
            "when": list(head.get("when") or []), "root": head.get("root"),
            "hop": head.get("hop"), "tables": [], "externals": [], "cuts": 0, "screens": []}


def _record_fact(phase, step, owner):
    """Feed one step's facts into `phase` and remember which phase owns its id."""
    if step.get("id") is not None:
        owner[step["id"]] = phase
    kind = step.get("kind")
    if kind in ("read", "write"):
        key = step.get("table") or step.get("text")
        if key:
            entry = next((t for t in phase["tables"] if t["table"] == key), None)
            if entry is None:
                entry = {"table": key, "ops": [], "n": 0}
                phase["tables"].append(entry)
            entry["n"] += 1
            op = "R" if kind == "read" else "W"
            if op not in entry["ops"]:
                entry["ops"].append(op)
    elif kind == "external":
        dest = step.get("target") or step.get("text")
        if dest and dest not in phase["externals"]:
            phase["externals"].append(dest)
    if step.get("truncated"):
        phase["cuts"] += 1


def _phases_of(steps, repo, head, owner=None):
    """Depth-first phases: a celery boundary starts a new phase for its task body."""
    owner = {} if owner is None else owner
    phases = []

    def walk(items, phase):
        for step in items:
            if step.get("boundary") and not step.get("ref"):
                _record_fact(phase, step, owner)     # the dispatch line stays where it runs; its body moves on
                sub = _new_phase({"kind": "celery", "via": "celery",
                                  "title": f"celery task {step.get('target', '')}",
                                  "root": step.get("id")}, repo)
                phases.append(sub)
                walk(step.get("children") or [], sub)
                continue
            _record_fact(phase, step, owner)
            if step.get("ref"):
                continue
            walk(step.get("children") or [], phase)

    first = _new_phase(head, repo)
    phases.append(first)
    walk(steps, first)
    return phases


def story(flow):
    """The flow as repo lanes / phases, plus the final phase id owning every step."""
    owner = {}
    e = flow.get("entry") or {}
    origin = flow.get("origin") or flow.get("repo") or ""
    p1 = _phases_of(flow.get("steps") or [], origin,
                    {"kind": "entry", "via": "",
                     "title": f"{e.get('kind', 'http')} {e.get('route', '')} → "
                              f"{e.get('handler', '')}"}, owner)
    origin_phases = list(p1)
    hops = flow.get("hops") or []

    after = {}
    for hop in hops:
        if hop.get("via") not in ("http", "celery"):
            continue
        he = hop.get("entry") or {}
        k = "http" if hop["via"] == "http" else "celery-hop"
        sub = _phases_of(hop.get("steps") or [], hop.get("repo") or "",
                         {"kind": k, "via": f"{hop['via']} ({hop.get('match')})",
                          "title": f"{he.get('kind', 'http')} {he.get('route', '')} → "
                                   f"{he.get('handler', '')}",
                          "hop": hop.get("id"), "when": hop.get("when", [])}, owner)
        anchor = owner.get(hop.get("from_step"))
        after.setdefault(id(anchor), []).append((anchor, sub))

    phases = []
    for phase in origin_phases:
        phases.append(phase)
        for anchor, sub in after.get(id(phase), []):
            phases.extend(sub)
    for anchor, sub in after.get(id(None), []):
        phases.extend(sub)

    origin_index = {s.get("id"): i for i, s in enumerate(_walk_steps(flow.get("steps") or []))}
    table_hops = [h for h in hops if h.get("via") == "table"]
    table_hops.sort(key=lambda h: origin_index.get(h.get("from_step"), 10 ** 9))
    for hop in table_hops:
        expanded = next((c for c in hop.get("candidates") or [] if c.get("expanded")), None)
        entry = expanded or hop.get("entry")
        if entry:
            title = f"{entry.get('kind', 'http')} {entry.get('route', '')} → {entry.get('handler', '')}"
        else:
            title = f"no clear reader — {len(hop.get('candidates') or [])} candidates"
        sub = _new_phase({"kind": "table", "via": f"reads {hop.get('table', '')}",
                          "when": hop.get("when", []), "hop": hop.get("id"),
                          "title": title}, hop.get("repo") or "")
        sub["candidates"] = [{"route": c.get("route", ""), "expanded": bool(c.get("expanded")),
                              "reasons": list(c.get("reasons") or [])}
                             for c in hop.get("candidates") or []]
        phases.append(sub)
        for step in hop.get("steps") or []:
            _walk_facts(step, sub, owner)

    for i, phase in enumerate(phases, 1):
        phase["id"] = f"p{i}"
        for table in phase["tables"]:
            table["ops"] = sorted(table["ops"])
    step_phase = {sid: phase["id"] for sid, phase in owner.items()}
    return phases, step_phase


def _walk_facts(step, phase, owner):
    _record_fact(phase, step, owner)
    if step.get("ref"):
        return
    for child in step.get("children") or []:
        _walk_facts(child, phase, owner)


def table_timeline(flow, phases, step_phase):
    """Every read/write in execution order, grouped by table (first touch first)."""
    phase_index = {p["id"]: i for i, p in enumerate(phases)}
    events, counter = {}, [0]

    def walk(steps, repo):
        for step in steps:
            if step.get("ref"):
                continue
            if step.get("kind") in ("read", "write"):
                key = step.get("table") or step.get("text")
                if key:
                    ev = {"op": "R" if step["kind"] == "read" else "W", "repo": repo,
                          "phase": step_phase.get(step.get("id")), "step": step.get("id"),
                          "loc": step.get("loc")}
                    for field in ("where", "where_not", "set"):
                        if step.get(field):
                            ev[field] = step[field]
                    ev["_dfs"] = counter[0]
                    counter[0] += 1
                    events.setdefault(key, []).append(ev)
            walk(step.get("children") or [], repo)

    walk(flow.get("steps") or [], flow.get("origin") or flow.get("repo") or "")
    for hop in flow.get("hops") or []:
        walk(hop.get("steps") or [], hop.get("repo") or "")

    def sort_key(ev):
        return (phase_index.get(ev["phase"], 10 ** 9), ev["_dfs"])

    out = []
    for key, evs in events.items():
        evs.sort(key=sort_key)
        out.append({"table": key, "written": any(ev["op"] == "W" for ev in evs), "events": evs})
    out.sort(key=lambda item: sort_key(item["events"][0]))
    for item in out:
        for ev in item["events"]:
            del ev["_dfs"]
    return out


_MARKERS = {"read": "R", "write": "W", "external": "→", "async": "⇢", "cache": "$",
            "call": "call", "exit": "⛔"}


def _fmt(v):
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v) if not isinstance(v, str) else v


def _step_line(step):
    text = f"{_MARKERS.get(step['kind'], '')} {step.get('text', '')}"
    if step.get("where"):
        text += " where{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in step["where"].items()) + "}"
    if step.get("where_not"):
        text += " not{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in step["where_not"].items()) + "}"
    if step.get("set"):
        text += " set{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in step["set"].items()) + "}"
    if step.get("ref"):
        text += f" (see {step['ref']})"
    if step.get("truncated"):
        text += " ✂ not followed (depth/budget cap)"
    if step.get("loc"):
        text += f"  {step['loc']}"
    return text


_EMPTY_HOP = "(no reads, writes, calls or exits found there)"


def _render_steps(steps, prefix, hops=None):
    lines, prev = [], []
    for i, step in enumerate(steps):
        last = i == len(steps) - 1
        conn = "└─ " if last else "├─ "
        child_prefix = prefix + ("   " if last else "│  ")
        for guard in step.get("guards", []):
            if guard not in prev:
                lines.append(prefix + conn + "? " + guard)
        prev = step.get("guards", [])
        lines.append(prefix + conn + _step_line(step))
        for hop in (hops or {}).get(step.get("id"), []):
            route = (hop.get("entry") or {}).get("route", "")
            lines.append(child_prefix + f"═══ {hop['via']} → {hop['repo']} {route} ({hop['match']}) ═══")
            lines.extend(_render_steps(hop.get("steps") or [], child_prefix) or [child_prefix + _EMPTY_HOP])
        if step.get("boundary"):
            lines.append(child_prefix + f"═══ {step['boundary']} ═══")
        lines.extend(_render_steps(step.get("children") or [], child_prefix, hops))
    return lines


def render_ascii(flow):
    e = flow["entry"]
    hops = flow.get("hops") or []
    inline = {}
    for hop in hops:
        if hop.get("via") in ("http", "celery"):
            inline.setdefault(hop.get("from_step"), []).append(hop)
    lines = [f"FLOW {e['kind']} {e['route']} → {e['handler']} ({e['loc']})"]
    lines.extend(_render_steps(flow["steps"], "", inline))
    for hop in hops:
        if hop.get("via") != "table":
            continue
        lines.append(f"═══ later · {hop['repo']} reads {hop['table']} "
                     f"(written at {hop['from_step']}) ═══")
        when = hop.get("when") or []
        if when:
            text = " › ".join(when)
            lines.append("when: " + (text if len(text) <= 120 else text[:120] + "…"))
        for cand in hop.get("candidates") or []:
            star = "★" if cand.get("expanded") else " "
            lines.append(f"{star} {cand.get('route', '')}  {cand.get('handler', '')}  "
                         f"[{'; '.join(cand.get('reasons') or [])}]")
        if hop.get("match"):
            lines.extend(_render_steps(hop.get("steps") or [], "") or ["  " + _EMPTY_HOP])
        else:
            lines.append(f"  pick one: --follow {hop['repo']}:<route>")
    return "\n".join(lines)


def write_html(flow, path, screens=()):
    """One self-contained page: story lanes, table timeline, step tree + source snippets."""
    import base64

    phases, step_phase = story(flow)
    timeline = table_timeline(flow, phases, step_phase)

    sources = {}

    def collect(steps, repo):
        for step in _walk_steps(steps):
            loc = step.get("loc")
            if not loc:
                continue
            key = f"{repo}:{loc}"
            if key in sources:
                continue
            if repo in (flow.get("repo_paths") or {}):
                rel, _, line = loc.rpartition(":")
                try:
                    number = int(line)
                except ValueError:
                    sources[key] = {"found": False, "path": rel, "line": line}
                    continue
                import serve
                sources[key] = serve.source_snippet(flow["repo_paths"][repo], "WORKTREE",
                                                    rel, number, ctx=5)
            else:
                sources[key] = {"found": False}

    collect(flow.get("steps") or [], flow.get("origin") or flow.get("repo") or "")
    for hop in flow.get("hops") or []:
        collect(hop.get("steps") or [], hop.get("repo") or "")

    exts = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}
    for item in screens or []:
        step_id, raw = None, item
        if "=" in item:
            left, right = item.split("=", 1)
            if re.match(r"^(h\d+\.)?s\d+$", left):
                step_id, raw = left, right
        ext = os.path.splitext(raw)[1].lower().lstrip(".")
        if ext not in exts:
            print(f"flow: skipped screenshot {raw}: unsupported extension", file=sys.stderr)
            continue
        try:
            size = os.path.getsize(raw)
        except OSError:
            print(f"flow: skipped screenshot {raw}: file not found", file=sys.stderr)
            continue
        if size > 5 * 1024 * 1024:
            print(f"flow: skipped screenshot {raw}: larger than 5 MB", file=sys.stderr)
            continue
        phase = None
        if step_id is None:
            phase = phases[0] if phases else None
        else:
            pid = step_phase.get(step_id)
            if pid is None:
                print(f"flow: skipped screenshot {raw}: unknown step {step_id}", file=sys.stderr)
                continue
            phase = next((p for p in phases if p["id"] == pid), None)
        if phase is None:
            print(f"flow: skipped screenshot {raw}: no phase holds {step_id}", file=sys.stderr)
            continue
        try:
            with open(raw, "rb") as f:
                blob = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            print(f"flow: skipped screenshot {raw}: cannot read", file=sys.stderr)
            continue
        phase["screens"].append(f"data:image/{exts[ext]};base64,{blob}")

    data = {"flow": flow, "story": phases, "step_phase": step_phase,
            "timeline": timeline, "sources": sources}
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web",
                            "flow.template.html")
    with open(tpl_path, encoding="utf-8") as f:
        template = f.read()
    import diff_pr
    html = template.replace("__FLOW_DATA__",
                            json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    html = diff_pr.inline_glossary(html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    p = argparse.ArgumentParser(prog="lenscheck flow",
                                description="Ordered steps one entrypoint executes.")
    p.add_argument("repo")
    p.add_argument("query", nargs="?")
    p.add_argument("--list", action="store_true", help="list every entrypoint candidate")
    p.add_argument("--pick", type=int, metavar="N", help="pick candidate N (1-based)")
    p.add_argument("--all", action="store_true", help="keep every step, including bare helpers")
    p.add_argument("--out", metavar="FILE", help="write the flow as JSON")
    p.add_argument("--crossrepo", nargs="+", metavar="PATH",
                   help="follow the flow into these other repos")
    p.add_argument("--follow", action="append", metavar="REPO:ROUTE",
                   help="force a table-hop reader (repeatable)")
    p.add_argument("--html", metavar="FILE", help="write a self-contained interactive HTML page")
    p.add_argument("--screen", action="append", metavar="[STEP=]PATH",
                   help="attach a screenshot to a step (repeatable)")
    args = p.parse_args()

    if args.list:
        for c in candidates(args.repo):
            print(f"{c.get('kind', ''):8} {c.get('route', ''):30} {c.get('handler', ''):20} {c.get('loc', '')}")
        return 0
    if not args.query:
        p.print_usage(sys.stderr)
        return 2

    try:
        flow = build_flow(args.repo, args.query, pick=args.pick, keep_all=args.all,
                          crossrepo_paths=args.crossrepo, follow=args.follow)
    except Ambiguous as e:
        if e.ranked:
            for i, c in enumerate(e.ranked, 1):
                print(f"{i}. {c.get('kind', '')}  {c.get('route', '')}  {c.get('handler', '')}  {c.get('loc', '')}")
            print("hint: --pick N")
        else:
            print(f"no entrypoint matches {args.query!r}", file=sys.stderr)
        return 2
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(render_ascii(flow))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(flow, f, indent=2, ensure_ascii=False)
            f.write("\n")
    if args.html:
        write_html(flow, args.html, args.screen or [])
        print(f"wrote {args.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
