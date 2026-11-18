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
from extractor.trace import trace, first_doc_line


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
    if len(exact) == 1 or (exact and len({c.get("loc") for c in exact}) == 1):
        return exact[0]                     # one view mounted on several urls is still one definition
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


def build_flow(repo, query=None, pick=None, keep_all=False, crossrepo_paths=None, follow=None,
               cand=None, method=None, index=None, indexes=None):
    """One entrypoint's flow. `cand` skips query resolution, `method` keeps one HTTP method of a class view,
    `index`/`indexes` share parsed repos across calls (a journey traces many entries of the same repos)."""
    if cand is None:
        cand = resolve(candidates(repo), query, pick=pick)
    ep, index = entry_endpoint(repo, cand, index=index)
    if ep is None:
        raise LookupError(f"entry not re-derivable: {cand.get('route')} -> {cand.get('handler')}")
    if method:
        walk = getattr(ep, "lc_walk", None) or {}
        nodes = [n for n in walk.get("method_nodes") or [] if getattr(n, "name", "") == method]
        if not nodes:
            raise LookupError(f"{cand.get('handler')} has no `{method}` method")
        ep.lc_walk = {**walk, "method_nodes": nodes}
        ep.methods = [method.upper()]
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
                  "loc": f"{ep.file}:{ep.line}", "methods": list(ep.methods or [])},
        "steps": tr["steps"],
        "tables": tables,
        "stats": tr["stats"],
        "hops": [],
    }
    nodes = (getattr(ep, "lc_walk", None) or {}).get("method_nodes") or []
    doc = first_doc_line(nodes[0]) if nodes else ""
    if doc:
        flow["entry"]["doc"] = doc
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
                indexes = {} if indexes is None else indexes
                indexes.setdefault(origin, index)
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


# ---- journeys: several entrypoints a user goes through, in order ---------------------------

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def resolve_entry(cands, spec):
    """`Handler[.method][@file-fragment]` or `command:x` / `task:x` → (candidate, method or None).
    LookupError when nothing or more than one handler definition matches."""
    spec = str(spec).strip()
    method, name, frag = None, spec, ""
    if spec.startswith(("command:", "task:", "signal:")):
        hits = [c for c in cands if c.get("route") == spec]
    else:
        name, _, frag = spec.partition("@")
        head, dot, tail = name.rpartition(".")
        if dot and tail in _VERBS:
            name, method = head, tail
        hits = [c for c in cands if c.get("handler") == name and (not frag or frag in c.get("loc", ""))]
    uniq = list({c.get("loc"): c for c in hits}.values())     # one view mounted on two urls is one definition
    if len(uniq) == 1:
        return uniq[0], method
    if not uniq:
        stem = name.lower()[:6]
        near = sorted({c.get("handler", "") for c in cands if stem and stem in c.get("handler", "").lower()})[:5]
        raise LookupError(f"no entrypoint for {spec!r}" + (f" — close: {', '.join(near)}" if near else ""))
    raise LookupError(f"{spec!r} matches {len(uniq)} definitions: "
                      + "; ".join(f"{c.get('handler')} @{c.get('loc')}" for c in uniq) + " — add @<file> to pick one")


def load_journey(path):
    """A journey file (.yaml/.yml via PyYAML, or .json): `name`, `title`, `source`, `crossrepo`,
    `steps: [{name, entries: [...]}]`. ValueError naming the file when the shape is wrong."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        data = json.loads(text)
    else:
        try:
            import yaml
        except ImportError:
            raise ValueError(f"{path}: reading YAML needs PyYAML (pip install pyyaml) — or use a .json journey")
        data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError(f"{path}: a journey needs a non-empty `steps` list")
    for i, st in enumerate(data["steps"], 1):
        if not isinstance(st, dict) or not st.get("entries"):
            raise ValueError(f"{path}: step {i} needs `entries`")
    data.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    data.setdefault("title", data["name"])
    return data


def build_journey(repo, spec, crossrepo_paths=None, follow=None, keep_all=False):
    """Trace every entry of a journey (one shared index) and join them into ONE flow whose top-level
    nodes are the journey steps (`boundary: "step"`), so story/timeline/HTML/narrative work unchanged."""
    cands = candidates(repo)
    index, indexes = RepoIndex(repo), {}
    top, hops, tables, repo_paths, table_hops = [], [], {}, {}, set()
    stats = {"calls_followed": 0, "truncated": False}
    base = None
    for k, st in enumerate(spec["steps"], 1):
        node = {"id": f"j{k}", "kind": "call", "text": f"Step {k} · {st.get('name', '')}".strip(),
                "guards": [], "loc": "", "boundary": "step", "children": []}
        for m, entry in enumerate(st["entries"], 1):
            cand, method = resolve_entry(cands, entry)
            f = build_flow(repo, cand=cand, method=method, keep_all=keep_all, crossrepo_paths=crossrepo_paths,
                           follow=follow, index=index, indexes=indexes)
            base = base or f
            pre, e = f"j{k}e{m}", f["entry"]
            enode = {"id": pre, "kind": "call", "guards": [], "loc": e["loc"],
                     "text": f"{'/'.join(e.get('methods') or [])} {e['route']} → {e['handler']}".strip(),
                     "children": _prefix(f["steps"], pre)}
            if e.get("doc"):
                enode["doc"] = e["doc"]
            node["children"].append(enode)
            for h in f.get("hops") or []:
                if h.get("via") == "table":                 # several entries write one table: one hop is enough
                    key = (h.get("table"), h.get("repo"))
                    if key in table_hops:
                        continue
                    table_hops.add(key)
                h = dict(h, id=f"{pre}.{h['id']}", steps=_prefix(h.get("steps") or [], pre))
                if h.get("from_step"):
                    h["from_step"] = f"{pre}.{h['from_step']}"
                hops.append(h)
            tables.update(f.get("tables") or {})
            repo_paths.update(f.get("repo_paths") or {})
            stats["calls_followed"] += (f.get("stats") or {}).get("calls_followed", 0)
            stats["truncated"] = stats["truncated"] or bool((f.get("stats") or {}).get("truncated"))
        top.append(node)
    hops = _merge_reader_hops(hops)                 # different entries' tables can share one reader too
    out = {"schema": 1, "generated_by": "lenscheck flow", "repo": base["repo"], "repo_meta": base["repo_meta"],
           "entry": {"kind": "journey", "route": spec["name"], "handler": spec.get("title") or spec["name"], "loc": ""},
           "journey": {"name": spec["name"], "title": spec.get("title") or spec["name"],
                       "source": spec.get("source", ""),
                       "steps": [{"name": s.get("name", ""), "entries": list(s["entries"])} for s in spec["steps"]]},
           "steps": top, "tables": tables, "stats": stats, "hops": hops,
           "origin": base["origin"], "repo_paths": repo_paths}
    if base.get("repos"):
        out["repos"] = base["repos"]
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
            if step.get("kind") == "write":
                if step.get("one_of"):
                    for tbl in step["one_of"]:
                        if tbl and tbl != "<instance>":
                            writes_by_table.setdefault(tbl, []).append(step)
                elif step.get("model"):
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
            cmap_o = merged_cmap(dict.fromkeys(s["model"] for s in write_steps if s.get("model")), index)
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
    return _merge_reader_hops(hops)


def _merge_reader_hops(hops):
    """One reader that picks up several tables this flow writes is expanded once: later table hops it
    already won fold into the first one's `also_tables` instead of repeating the same trace."""
    first, out = {}, []
    for h in hops:
        e = h.get("entry") or {}
        key = (h.get("repo"), e.get("route"), e.get("handler")) if h.get("via") == "table" and h.get("steps") else None
        if key in first:
            first[key].setdefault("also_tables", []).append(h.get("table"))
            continue
        if key:
            first[key] = h
        out.append(h)
    return out



def _walk_steps(steps):
    for step in steps:
        yield step
        yield from _walk_steps(step.get("children") or [])


def _guard_chains(steps):
    """step id -> guards in force there (ancestors' guards + own), depth-first in source order."""
    out = {}

    def walk(items, chain):
        for step in items:
            when = chain + list(step.get("guards") or [])
            out[step.get("id")] = when
            walk(step.get("children") or [], when)

    walk(steps, [])
    return out


def _exclusive(a, b):
    """True when two guard chains are two branches of the same `if` — they first differ at an
    `if …` vs `else (not: …)` with matching (possibly 80-char-cut) conditions."""
    i = next((j for j, (x, y) in enumerate(zip(a, b)) if x != y), None)
    if i is None:
        return False
    x, y = a[i], b[i]
    if x.startswith("if ") and y.startswith("else (not: "):
        cond_if, cond_else = x, y
    elif y.startswith("if ") and x.startswith("else (not: "):
        cond_if, cond_else = y, x
    else:
        return False
    cx = cond_if[3:].rstrip("…")
    cy = cond_else[len("else (not: "):].rstrip("…").rstrip(")")
    shorter = min(len(cx), len(cy))
    return shorter >= 1 and (cx.startswith(cy) or cy.startswith(cx))


def _mark_alternatives(phases):
    """Mark mutually exclusive phases (`if X` vs `else (not: X)`) as alternatives of one group."""
    groups, open_group = [], None
    for phase in phases:
        if phase.get("kind") == "entry":
            continue
        when = phase.get("when") or []
        if open_group is not None and all(
                _exclusive(when, member.get("when") or []) for member in open_group):
            open_group.append(phase)
        else:
            open_group = [phase]
            groups.append(open_group)
    n = 0
    for group in groups:
        if len(group) < 2:
            continue
        n += 1
        for phase in group:
            other = next(m for m in group if m is not phase)
            wa, wb = list(phase.get("when") or []), list(other.get("when") or [])
            branch = next((wa[i] for i in range(min(len(wa), len(wb))) if wa[i] != wb[i]), None)
            phase["alt"] = {"group": f"a{n}", "branch": branch}


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
            if step.get("boundary") == "step" and not step.get("ref"):      # a journey step: its own phase
                sub = _new_phase({"kind": "step", "via": "next step", "title": step.get("text", ""),
                                  "root": step.get("id")}, repo)
                _record_fact(sub, step, owner)
                phases.append(sub)
                walk(step.get("children") or [], sub)
                continue
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
    chains = _guard_chains(flow.get("steps") or [])
    for hop in hops:
        chains.update(_guard_chains(hop.get("steps") or []))

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
    if e.get("kind") == "journey":                  # the synthetic journey root owns no step — drop its phase
        used = {id(p) for p in owner.values()}
        phases = [p for p in phases if p["kind"] != "entry" or id(p) in used]

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
        also = hop.get("also_tables") or []
        sub = _new_phase({"kind": "table", "via": f"reads {hop.get('table', '')}"
                                                  + (f" + {len(also)} more" if also else ""),
                          "when": hop.get("when", []), "hop": hop.get("id"),
                          "title": title}, hop.get("repo") or "")
        sub["candidates"] = [{"route": c.get("route", ""), "expanded": bool(c.get("expanded")),
                              "reasons": list(c.get("reasons") or [])}
                             for c in hop.get("candidates") or []]
        phases.append(sub)
        for step in hop.get("steps") or []:
            _walk_facts(step, sub, owner)

    for phase in phases:
        if phase.get("kind") == "entry":
            phase["when"] = []
        elif phase.get("kind") == "celery" and phase.get("root") in chains:
            phase["when"] = list(chains[phase["root"]])
    _mark_alternatives(phases)

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
                one_of = list(step.get("one_of") or [])
                single = step.get("table") or step.get("text")
                keys = one_of or ([single] if single else [])
                for key in keys:
                    ev = {"op": "R" if step["kind"] == "read" else "W", "repo": repo,
                          "phase": step_phase.get(step.get("id")), "step": step.get("id"),
                          "loc": step.get("loc")}
                    if one_of:
                        ev["maybe"] = True
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


def collect_sources(flow):
    """`{"<repo>:<file>:<line>": snippet}` for every step loc (origin + hops), read off disk."""
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
    return sources


def write_html(flow, path, screens=(), explain=False, model=None, refresh=False):
    """One self-contained page: the overview infographic (Claude-explained with `explain`, else from facts),
    story lanes, table timeline, step tree + source snippets. Returns the narrative meta."""
    data = page_data(flow, screens, explain=explain, model=model, refresh=refresh)
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
    return data["narrative_meta"]


def page_data(flow, screens=(), explain=False, model=None, refresh=False):
    """Everything the flow page renders (phases, timeline, snippets, narrative) — written into a file by
    `write_html`, returned as JSON by `lenscheck serve`'s /api/flow."""
    import base64

    phases, step_phase = story(flow)
    timeline = table_timeline(flow, phases, step_phase)
    sources = collect_sources(flow)

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

    import narrate
    narrative, meta = None, {"source": "facts"}
    if explain:
        try:
            narrative, meta = narrate.explain(flow, phases, step_phase, sources, timeline,
                                              model=model or narrate.DEFAULT_MODEL, refresh=refresh)
        except narrate.NarrateError as e:
            print(f"flow: --explain failed ({e}); using the facts-only overview", file=sys.stderr)
    if narrative is None:
        narrative = narrate.fallback(flow, phases, step_phase, timeline)

    return {"flow": flow, "story": phases, "step_phase": step_phase,
            "timeline": timeline, "sources": sources, "narrative": narrative, "narrative_meta": meta}


# ---- catalog: every entrypoint, grouped by app, with what it does ---------------------------

def slug(text):
    """File-name-safe name for a flow page (`CampaignTargetingView <int:pk>/targeting/` → `campaigntargetingview-int-pk-targeting`)."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:80] or "flow"


def flow_slug(flow):
    e = flow.get("entry") or {}
    if e.get("kind") == "journey":
        return slug(e.get("route"))
    return slug(f"{e.get('handler', '')} {e.get('route', '')}")


def _schema_summary(fn):
    """`@extend_schema(summary="…")` on a view method, else ""."""
    for d in getattr(fn, "decorator_list", []):
        if isinstance(d, ast.Call) and ast.unparse(d.func).split(".")[-1] == "extend_schema":
            for kw in d.keywords:
                if kw.arg == "summary" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value.strip()
    return ""


def _summarizer(repo):
    """summary(rel_file, handler) — per-method swagger summary or docstring for a view, `help` for a command."""
    trees = {}

    def tree(rel):
        if rel not in trees:
            try:
                with open(os.path.join(repo, rel), encoding="utf-8") as f:
                    trees[rel] = ast.parse(f.read())
            except (OSError, SyntaxError, ValueError):
                trees[rel] = None
        return trees[rel]

    def summary(rel, handler):
        t = tree(rel)
        for node in ast.walk(t) if t is not None else []:
            if isinstance(node, ast.ClassDef) and node.name == handler:
                parts = []
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in _VERBS:
                        s = _schema_summary(m) or first_doc_line(m)
                        if s:
                            parts.append(f"{m.name.upper()}: {s}")
                    elif isinstance(m, ast.Assign) and any(getattr(x, "id", "") == "help" for x in m.targets) \
                            and isinstance(m.value, ast.Constant) and isinstance(m.value.value, str):
                        parts.append(m.value.value.strip().splitlines()[0][:160])    # a management command's help
                return " · ".join(parts) or first_doc_line(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == handler:
                return _schema_summary(node) or first_doc_line(node)
        return ""
    return summary


def list_journeys(directory):
    out = []
    for fn in sorted(os.listdir(directory)) if directory and os.path.isdir(directory) else []:
        if not fn.endswith((".yaml", ".yml", ".json")):
            continue
        path = os.path.join(directory, fn)
        try:
            j = load_journey(path)
            out.append({"name": j["name"], "title": j["title"], "steps": len(j["steps"]),
                        "source": j.get("source", ""), "file": path, "slug": slug(j["name"])})
        except (OSError, ValueError) as e:
            out.append({"name": fn, "title": "", "steps": 0, "file": path, "error": str(e)})
    return out


def catalog(repo, journeys_dir=None):
    """Every entrypoint of the repo (from the cached fact index), grouped by app, with a one-line summary
    and the tables it writes / reads — the "what flows exist here" index."""
    import factindex
    import map_repo
    from types import SimpleNamespace
    prov = factindex.load(repo, with_calls=True)
    writes, reads = {}, {}
    for table, rec in (prov.get("tables") or {}).items():
        if table == "<unresolved>":
            continue
        for e in rec.get("written_by") or []:
            writes.setdefault(e.get("loc"), set()).add(table)
        for e in rec.get("read_by") or []:
            reads.setdefault(e.get("loc"), set()).add(table)
    apps, summary, groups = map_repo.resolve_apps(repo), _summarizer(repo), {}
    for e in prov.get("endpoints") or []:
        rel = str(e.get("loc", "")).rpartition(":")[0]
        app = map_repo.app_of(SimpleNamespace(file=rel), apps)
        groups.setdefault(app, []).append({
            "kind": e.get("kind", "http"), "route": e.get("route", ""),
            "full_route": (e.get("full_routes") or [""])[0], "handler": e.get("handler", ""),
            "methods": list(e.get("methods") or []), "loc": e.get("loc", ""),
            "summary": summary(rel, e.get("handler", "")),
            "writes": sorted(writes.get(e.get("loc"), ())), "reads": sorted(reads.get(e.get("loc"), ())),
            "slug": slug(f"{e.get('handler', '')} {e.get('route', '')}")})
    apps_out = [{"app": a, "entries": sorted(rows, key=lambda r: (r["kind"], r["full_route"] or r["route"]))}
                for a, rows in sorted(groups.items())]
    return {"repo": os.path.abspath(repo), "name": os.path.basename(os.path.abspath(repo)),
            "total": sum(len(a["entries"]) for a in apps_out), "apps": apps_out,
            "journeys": list_journeys(journeys_dir)}


def render_catalog(cat):
    lines = [f"CATALOG {cat['name']} — {cat['total']} entrypoints in {len(cat['apps'])} apps"]
    if cat["journeys"]:
        lines.append("JOURNEYS:")
        for j in cat["journeys"]:
            lines.append(f"  {j['name']}: {j.get('error') or j['title']} ({j['steps']} steps)")
    for a in cat["apps"]:
        lines.append(f"\n== {a['app']} ({len(a['entries'])})")
        for r in a["entries"]:
            line = f"  {r['kind']:7} {r['full_route'] or r['route']}  [{'/'.join(r['methods'])}]  {r['handler']}"
            if r["summary"]:
                line += f" — {r['summary']}"
            if r["writes"]:
                line += f"  (writes: {', '.join(r['writes'])})"
            lines.append(line)
    return "\n".join(lines)


def write_catalog_html(cat, path):
    out_dir = os.path.dirname(os.path.abspath(path))
    for a in cat["apps"]:                           # link the flow pages already generated next to the index
        for r in a["entries"]:
            r["page"] = f"{r['slug']}.html" if os.path.exists(os.path.join(out_dir, f"{r['slug']}.html")) else ""
    for j in cat["journeys"]:
        j["page"] = f"{j.get('slug')}.html" if j.get("slug") and \
            os.path.exists(os.path.join(out_dir, f"{j['slug']}.html")) else ""
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "catalog.template.html")
    with open(tpl, encoding="utf-8") as f:
        html = f.read().replace("__CATALOG_DATA__", json.dumps(cat, ensure_ascii=False).replace("</", "<\\/"))
    import diff_pr
    with open(path, "w", encoding="utf-8") as f:
        f.write(diff_pr.inline_glossary(html))


def _html_target(arg, flow):
    """`--html DIR/` (an existing dir or a trailing slash) → DIR/<slug>.html; else the file as given."""
    if arg.endswith(os.sep) or os.path.isdir(arg):
        os.makedirs(arg, exist_ok=True)
        return os.path.join(arg, flow_slug(flow) + ".html")
    return arg


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
    p.add_argument("--explain", action="store_true",
                   help="have Claude (the `claude` CLI) write the overview; needs --html")
    p.add_argument("--explain-model", metavar="MODEL", help="model for --explain (default claude-opus-5-5)")
    p.add_argument("--refresh-explain", action="store_true", help="ignore the cached explanation")
    p.add_argument("--journey", metavar="FILE", help="trace a journey (several entrypoints in order) from a YAML/JSON file")
    p.add_argument("--catalog", action="store_true", help="every entrypoint grouped by app, with summaries and writes")
    p.add_argument("--journeys", metavar="DIR", help="with --catalog: list the saved journeys in DIR")
    p.add_argument("--json", metavar="FILE", help="with --catalog: write the catalog as JSON")
    args = p.parse_args()
    if args.explain and not args.html:
        p.error("--explain needs --html FILE")

    if args.list:
        for c in candidates(args.repo):
            print(f"{c.get('kind', ''):8} {c.get('route', ''):30} {c.get('handler', ''):20} {c.get('loc', '')}")
        return 0
    if args.catalog:
        cat = catalog(args.repo, args.journeys)
        print(render_catalog(cat))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(cat, f, indent=2, ensure_ascii=False)
        if args.html:
            path = os.path.join(args.html, "index.html") if (args.html.endswith(os.sep) or os.path.isdir(args.html)) \
                else args.html
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            write_catalog_html(cat, path)
            print(f"wrote {path}", file=sys.stderr)
        return 0
    if not args.query and not args.journey:
        p.print_usage(sys.stderr)
        return 2

    try:
        if args.journey:
            try:
                spec = load_journey(args.journey)
            except (OSError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 2
            xrepo = args.crossrepo or [p2 if os.path.isabs(os.path.expanduser(p2)) else
                                       os.path.normpath(os.path.join(args.repo, p2))
                                       for p2 in (spec.get("crossrepo") or [])] or None
            flow = build_journey(args.repo, spec, crossrepo_paths=xrepo, follow=args.follow or spec.get("follow"),
                                 keep_all=args.all)
        else:
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
        if type(e) is not LookupError:      # IndexError / KeyError are bugs, not "entry not found" — show the traceback
            raise
        print(str(e), file=sys.stderr)
        return 1

    print(render_ascii(flow))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(flow, f, indent=2, ensure_ascii=False)
            f.write("\n")
    if args.html:
        args.html = _html_target(args.html, flow)
        meta = write_html(flow, args.html, args.screen or [], explain=args.explain,
                          model=args.explain_model, refresh=args.refresh_explain)
        if meta.get("source", "facts") != "facts":
            st = meta.get("stats") or {}
            print(f"explained by {meta['source']} ({'cached' if meta.get('cached') else 'fresh'}): "
                  f"{st.get('refs_kept', 0)} refs kept, {st.get('refs_dropped', 0)} dropped, "
                  f"{st.get('unverified', 0)} unverified", file=sys.stderr)
        print(f"wrote {args.html}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
