#!/usr/bin/env python3
"""lenscheck map — the whole-repo view.

The PR reviewer answers "what *changed*". This answers "what *is* here": every endpoint in the repo,
grouped by Django app, ranked worst-first, with the same 7-edge facts and flow graph. Same engine as
the diff — just with the comparison turned off. Reductive by default: lead with the risky ones.

    python3 map_repo.py <repo-or-url> [--risky] [--app NAME] [--json out.json] [--out map.md]
"""
import argparse
import ast
import json
import os
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


_APP_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "migrations", "static",
             "media", "tests", "test"}


def resolve_apps(root):
    """Map the repo's real Django apps to their directories: `{reldir: label}`.

    Primary signal is **INSTALLED_APPS** in any `settings*.py` (local entries only — django.* and
    third-party are dropped because their module path isn't a directory in the repo). Anything with
    an `apps.py` (an AppConfig package) is folded in as a fallback, so apps missing from a
    dynamically-built INSTALLED_APPS are still found. One pass over the tree."""
    installed, apps_py = {}, {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _APP_SKIP]
        if "apps.py" in files:
            apps_py[os.path.relpath(dirpath, root)] = os.path.basename(dirpath)
        for fn in files:
            if not (fn.endswith(".py") and "settings" in fn):
                continue
            try:
                tree = ast.parse(open(os.path.join(dirpath, fn), encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Assign)
                        and any(getattr(t, "id", None) == "INSTALLED_APPS" for t in node.targets)):
                    continue
                for elt in getattr(node.value, "elts", []):
                    v = getattr(elt, "value", None)
                    if not isinstance(v, str):
                        continue
                    mod = v[:v.rindex(".apps.")] if (".apps." in v and v.endswith("Config")) else v
                    reldir = mod.replace(".", "/")
                    if os.path.isdir(os.path.join(root, reldir)):     # a local app, not a library
                        installed[reldir] = reldir.split("/")[-1]
    apps = dict(apps_py)
    apps.update(installed)                                            # INSTALLED_APPS labels win
    return apps


def app_of(ep, apps):
    """Which app an endpoint belongs to — the longest app directory that prefixes its file path.
    Falls back to the top path segment (minus an `apps/`|`src/` wrapper) when nothing matches."""
    f = ep.file or ""
    best = None
    for reldir, label in apps.items():
        if reldir != "." and (f == reldir or f.startswith(reldir + "/")):
            if best is None or len(reldir) > len(best[0]):
                best = (reldir, label)
    if best:
        return best[1]
    parts = [p for p in f.split("/") if p]
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
    local = ensure_local(repo)
    apps_index = resolve_apps(local)
    eps = analyze_repo(local)
    buckets = {}
    for ep in eps:
        sev, why = endpoint_risk(ep)
        if risky and sev not in (CRIT, HIGH):
            continue
        rec = {"route": ep.route, "handler": ep.handler, "auth": auth_str(ep), "sev": sev,
               "why": _clean_why(why), "tags": tags(ep), "loc": f"{ep.file}:{ep.line}",
               "unknowns": unknowns(ep), "graph": build_graph(ep)}
        buckets.setdefault(app_of(ep, apps_index), []).append(rec)

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


def write_html(m, path):
    """A self-contained, shareable map — the interactive `web/map.html`, with the data baked in so it
    opens from a file:// with no server (same trick as `review --html`)."""
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "map.html")
    tpl = open(tpl_path, encoding="utf-8").read()
    inject = "<script>window.__MAP_EMBEDDED__=" + json.dumps(m, ensure_ascii=False) + ";</script>\n"
    html = tpl.replace("<script>", inject + "<script>", 1)   # runs before the page's own script
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(prog="lenscheck map",
                                 description="Whole-repo endpoint map, grouped by app, worst-first.")
    ap.add_argument("repo", nargs="?", default=".", help="local path or git URL")
    ap.add_argument("--risky", action="store_true", help="only 🔴/🟠 endpoints (the security view)")
    ap.add_argument("--app", help="only this app")
    ap.add_argument("--json", dest="json_out", help="write the full map (with graphs) as JSON")
    ap.add_argument("--html", dest="html_out", help="write a self-contained interactive HTML report")
    ap.add_argument("--out", help="write the Markdown map to a file (else prints)")
    args = ap.parse_args()

    m = build_map(args.repo, risky=args.risky, app=args.app)
    wrote_any = False
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.json_out}: {m['shown']} endpoints, {len(m['apps'])} apps")
        wrote_any = True
    if args.html_out:
        write_html(m, args.html_out)
        print(f"wrote {args.html_out}: open it in a browser — no server needed")
        wrote_any = True
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(render_md(m))
        print(f"wrote {args.out}")
        wrote_any = True
    if not wrote_any:
        print(render_md(m))


if __name__ == "__main__":
    main()
