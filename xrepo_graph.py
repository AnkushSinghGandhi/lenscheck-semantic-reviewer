"""xrepo_graph.py — the cross-repo dependency graph and change impact.

Turns `crossrepo.build()` output (shared tables, API links, task links) into one graph and answers
"which endpoints in OTHER repos does a change reach, and through what?".

    node  ep:<repo>:<file:line>   an endpoint (route, celery task, command, signal receiver)
          table:<db.>table        a shared table
    edge  writes / reads          endpoint → table
          calls                   endpoint → endpoint (HTTP)
          dispatches              endpoint → task endpoint (Celery)

Impact walks from the changed endpoints (seeds):
    data      a changed or affected endpoint's output reaches someone: the readers of a table it
              writes, the route it calls, the task it dispatches. Keeps propagating (to max_depth).
    contract  a changed seed's interface has consumers: callers of its route, dispatchers of its task.
    co-writer another writer of a table a seed writes (the multi-writer conflict).
Contract and co-writer hits are reported but not walked further. A path through a `tail` (unverified)
link is `verify`, never `exact`. Only hops the cross-repo data knows are followed: a chain through a
table that just one repo touches is not seen.
"""
from collections import deque


def _ep(repo, e):
    return f"ep:{repo}:{e.get('loc', '')}"


def build(data):
    """{nodes: {id: attrs}, edges: [{src, kind, dst, match, label}]} from crossrepo output."""
    nodes, edges = {}, []

    def ep(repo, e, route=None):
        nid = _ep(repo, e)
        n = nodes.setdefault(nid, {"type": "endpoint", "repo": repo, "route": e.get("route", ""),
                                   "handler": e.get("handler", ""), "loc": e.get("loc", "")})
        if route and not n["route"]:
            n["route"] = route
        return nid

    for s in data.get("shared_tables", []):
        name = (s["database"] + "." if s.get("database") else "") + s["table"]
        tid = f"table:{name}"
        nodes[tid] = {"type": "table", "name": name}
        for repo, acc in s.get("by_repo", {}).items():
            for kind, key in (("writes", "written_by"), ("reads", "read_by")):
                for e in acc.get(key, []):
                    edges.append({"src": ep(repo, e), "kind": kind, "dst": tid, "match": "exact",
                                  "label": f"{kind} {name}"})
    for l in data.get("api_links", []):
        edges.append({"src": ep(l["from_repo"], l["from"]), "kind": "calls",
                      "dst": ep(l["to_repo"], l["to"], route=l["to"].get("route")),
                      "match": l.get("match", "tail"),
                      "label": " ".join(x for x in ("calls", l.get("method", ""), l.get("call", "")) if x)})
    for l in data.get("task_links", []):
        to = {"route": f"task:{l['to']['task']}", "handler": l["to"]["task"], "loc": l["to"].get("loc", "")}
        edges.append({"src": ep(l["from_repo"], l["from"]), "kind": "dispatches",
                      "dst": ep(l["to_repo"], to), "match": l.get("match", "tail"),
                      "label": l.get("dispatch", "dispatches")})
    by_src, by_dst = {}, {}
    for e in edges:
        by_src.setdefault(e["src"], []).append(e)
        by_dst.setdefault(e["dst"], []).append(e)
    return {"nodes": nodes, "edges": edges, "by_src": by_src, "by_dst": by_dst}


def seeds(graph, repo, changed_files, changed_routes=()):
    """Endpoints of `repo` (display name, or a NAME@REF of it) declared in a changed file, or whose route
    the semantic diff says changed (that catches a change made inside a helper the endpoint calls)."""
    files, routes = set(changed_files), set(changed_routes)
    return sorted(nid for nid, n in graph["nodes"].items()
                  if n["type"] == "endpoint" and (n["repo"] == repo or n["repo"].startswith(repo + "@"))
                  and (n["loc"].split(":")[0] in files or n["route"] in routes))


def _steps(graph, node, is_seed):
    """(next_node, kind, [edges walked]) one hop from `node`."""
    for w in graph["by_src"].get(node, []):
        if w["kind"] == "writes":
            for r in graph["by_dst"].get(w["dst"], []):
                if r["src"] != node:
                    if r["kind"] == "reads":
                        yield r["src"], "data", [w, r]
                    elif r["kind"] == "writes" and is_seed:
                        yield r["src"], "co-writer", [w, r]
        elif w["kind"] in ("calls", "dispatches"):
            yield w["dst"], "data", [w]
    if is_seed:
        for e in graph["by_dst"].get(node, []):
            if e["kind"] in ("calls", "dispatches"):
                yield e["src"], "contract", [e]


def impact(graph, seed_ids, max_depth=3):
    """Breadth-first: each other-repo endpoint the seeds reach, once, at its shallowest depth, with the
    path that explains it."""
    seed_ids = list(seed_ids)
    seed_repos = {graph["nodes"][s]["repo"] for s in seed_ids if s in graph["nodes"]}
    best = {s: None for s in seed_ids}           # node -> (parent, kind, edges, depth)
    q = deque((s, 0) for s in seed_ids)
    while q:
        node, d = q.popleft()
        if d >= max_depth:
            continue
        for nxt, kind, walked in _steps(graph, node, d == 0):
            if nxt in best:
                continue
            best[nxt] = (node, kind, walked, d + 1)
            if kind == "data":
                q.append((nxt, d + 1))

    affected = []
    for nid, info in best.items():
        if info is None or graph["nodes"][nid]["repo"] in seed_repos:
            continue
        path, cur = [], nid
        while best[cur] is not None:
            parent, kind, walked, _ = best[cur]
            path.append((cur, kind, walked))
            cur = parent
        path.reverse()
        hops = [_label(graph["nodes"][cur])]                    # cur is the seed now
        match = "exact"
        for n, kind, walked in path:
            hops += [_hop(kind, walked), _label(graph["nodes"][n])]
            if any(w["match"] != "exact" for w in walked):
                match = "verify"
        n = graph["nodes"][nid]
        affected.append({"repo": n["repo"], "route": n["route"], "handler": n["handler"], "loc": n["loc"],
                         "depth": info[3], "kind": info[1], "confidence": match, "path": hops})
    affected.sort(key=lambda a: (a["depth"], a["repo"], a["loc"]))
    repos = {}
    for a in affected:
        repos[a["repo"]] = repos.get(a["repo"], 0) + 1
    return {"seeds": [graph["nodes"][s] for s in seed_ids if s in graph["nodes"]],
            "affected": affected, "repos": repos}


def _hop(kind, walked):
    """How one step reads in a path, in the direction of the impact."""
    if kind == "contract":
        return f"← {walked[0]['label']}"                  # the consumer calls / dispatches the seed
    if kind == "co-writer":
        return f"{walked[0]['label']} (also written by)"
    return walked[0]["label"]                             # writes T (then read by) · calls · dispatches


def _label(n):
    return f"{n['repo']} {n['handler'] or n['route']} ({n['loc']})"


def render_markdown(imp):
    """The review section. Empty string when nothing outside the changed repo is reached."""
    if not imp or not imp.get("affected"):
        return ""
    by_kind = {"data": "gets changed data", "contract": "depends on the changed interface",
               "co-writer": "also writes the same table"}
    L = ["## 🔗 Cross-repo impact", "",
         f"This change reaches **{len(imp['affected'])} endpoint(s) in {len(imp['repos'])} other "
         f"repo(s)**: " + ", ".join(f"`{r}` ({n})" for r, n in sorted(imp["repos"].items())) + ".", ""]
    for a in imp["affected"]:
        flag = "" if a["confidence"] == "exact" else " ⚠ verify (matched on a partial route/name)"
        L.append(f"- **{a['repo']}** `{a['handler'] or a['route']}` `{a['loc']}` — {by_kind[a['kind']]}"
                 f" (depth {a['depth']}){flag}")
        L.append(f"  - path: " + " → ".join(f"`{h}`" for h in a["path"]))
    L.append("")
    return "\n".join(L)
