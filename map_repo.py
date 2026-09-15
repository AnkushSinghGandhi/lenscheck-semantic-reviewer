#!/usr/bin/env python3
"""lenscheck map — the whole-repo view.

The PR reviewer answers "what *changed*". This answers "what *is* here": every endpoint in the repo,
grouped by Django app, ranked worst-first, with the same 7-edge facts and flow graph. Same engine as
the diff — just with the comparison turned off. Reductive by default: lead with the risky ones.

    python3 map_repo.py <repo-or-url> [--risky] [--app NAME] [--json out.json] [--out map.md]
"""
import argparse
import json
import sys

from extractor.analyzer import analyze_repo
from diff_pr import (endpoint_risk, auth_str, items, ops_of, is_open, unknowns, build_graph,
                     CRIT, HIGH, MED, LOW)
from gitutil import ensure_local

SEV_ORDER = {CRIT: 0, HIGH: 1, MED: 2, LOW: 3}


def _clean_why(w):
    """The shared risk `why` is phrased for a *new* endpoint (the diff). Re-phrase it for the map,
    where every endpoint already exists."""
    return (w.replace("new endpoint — ", "")
             .replace("new *unauthenticated* endpoint performs", "unauthenticated — performs")
             .replace("new endpoint ", "")
             .replace("new read-only endpoint", "read-only")
             .replace("new ", ""))


def app_of(ep):
    """The Django app an endpoint belongs to, from its file path. Drops an `apps/`|`src/` wrapper;
    files sitting at the repo root fall into `(root)`. A cheap, honest first cut — refine later with
    INSTALLED_APPS if needed."""
    parts = [p for p in (ep.file or "").split("/") if p]
    if parts and parts[0] in ("apps", "src") and len(parts) > 1:
        parts = parts[1:]
    return parts[0] if len(parts) > 1 else "(root)"


def tags(ep):
    """Short behavioural tags for the whole-repo list (what the endpoint touches)."""
    o = ops_of(ep)
    t = []
    if is_open(ep):
        t.append("open")
    for k in ("read", "write", "external", "async", "cache"):
        if o.get(k):
            t.append(k)
    if items(ep, "e6_pii"):
        t.append("pii")
    return t


def build_map(repo, risky=False, app=None):
    """Analyze the whole repo → apps, each with its endpoints ranked worst-first + a risk rollup."""
    eps = analyze_repo(ensure_local(repo))
    buckets = {}
    for ep in eps:
        sev, why = endpoint_risk(ep)
        if risky and sev not in (CRIT, HIGH):
            continue
        rec = {"route": ep.route, "handler": ep.handler, "auth": auth_str(ep), "sev": sev,
               "why": _clean_why(why), "tags": tags(ep), "loc": f"{ep.file}:{ep.line}",
               "unknowns": unknowns(ep), "graph": build_graph(ep)}
        buckets.setdefault(app_of(ep), []).append(rec)

    apps = []
    for name, recs in buckets.items():
        if app and name != app:
            continue
        recs.sort(key=lambda r: (SEV_ORDER[r["sev"]], r["route"]))
        counts = {s: sum(1 for r in recs if r["sev"] == s) for s in (CRIT, HIGH, MED, LOW)}
        apps.append({"app": name, "endpoints": recs, "counts": counts, "worst": recs[0]["sev"]})
    apps.sort(key=lambda a: (SEV_ORDER[a["worst"]], -len(a["endpoints"]), a["app"]))
    return {"repo": repo, "total": len(eps), "shown": sum(len(a["endpoints"]) for a in apps), "apps": apps}


def _counts_str(c):
    return "  ".join(f"{s}{c[s]}" for s in (CRIT, HIGH, MED, LOW) if c[s])


def render_md(m):
    L = [f"# Backend map — {m['repo']}", ""]
    total_risky = sum(a["counts"][CRIT] + a["counts"][HIGH] for a in m["apps"])
    L.append(f"**{m['shown']} endpoints** across **{len(m['apps'])} apps** · "
             f"**{total_risky}** worth a look ({CRIT}+{HIGH}). Ranked worst-first.\n")
    for a in m["apps"]:
        L.append(f"## {a['worst']} `{a['app']}` — {len(a['endpoints'])} endpoints  ({_counts_str(a['counts'])})")
        L.append("")
        L.append("| | route | auth | touches | why |")
        L.append("|--|-------|------|---------|-----|")
        for r in a["endpoints"]:
            L.append(f"| {r['sev']} | `{r['route']}` | {r['auth']} | {' · '.join(r['tags']) or '—'} | "
                     f"{r['why'] if r['sev'] in (CRIT, HIGH) else ''} |")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(prog="lenscheck map",
                                 description="Whole-repo endpoint map, grouped by app, worst-first.")
    ap.add_argument("repo", nargs="?", default=".", help="local path or git URL")
    ap.add_argument("--risky", action="store_true", help="only 🔴/🟠 endpoints (the security view)")
    ap.add_argument("--app", help="only this app")
    ap.add_argument("--json", dest="json_out", help="write the full map (with graphs) as JSON")
    ap.add_argument("--out", help="write the Markdown map to a file (else prints)")
    args = ap.parse_args()

    m = build_map(args.repo, risky=args.risky, app=args.app)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.json_out}: {m['shown']} endpoints, {len(m['apps'])} apps")
    md = render_md(m)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"wrote {args.out}")
    elif not args.json_out:
        print(md)


if __name__ == "__main__":
    main()
