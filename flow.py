#!/usr/bin/env python3
"""`lenscheck flow` — the ordered flow of one HTTP route / management command / celery task / signal.

Resolve a query against the repo's entrypoints, re-derive the chosen entrypoint, and print the steps it
runs in order: DB reads/writes (with literal `where`/`set` values), external calls, async dispatches,
helper calls (nested), early exits — each with the `if`/`for`/`try` conditions guarding it. Pure stdlib,
no LLM; facts come from the same extractor the rest of lenscheck uses.
"""
from __future__ import annotations

import argparse
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


def entry_endpoint(repo, cand):
    """Re-derive the Endpoint object for a candidate, with its `lc_walk` attached."""
    index = RepoIndex(repo)
    kind = cand.get("kind", "http")
    route, handler = cand.get("route", ""), cand.get("handler", "")
    if kind == "http":
        for r, h, urls_file in parse_endpoints(index):
            if (r, h) == (route, handler):
                ep = analyze_handler(handler, index, near=urls_file)
                ep.route = route
                return ep, index
        try:
            import frameworks
        except ImportError:
            from extractor import frameworks
        for ep in frameworks.discover(index):
            if (ep.route, ep.handler) == (route, handler):
                return ep, index
    else:
        for ep in job_entrypoints(index, only=route):
            if ep.handler == handler:
                return ep, index
    return None, index


def build_flow(repo, query, pick=None, keep_all=False):
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
    return {
        "schema": 1,
        "generated_by": "lenscheck flow",
        "repo": os.path.abspath(repo),
        "repo_meta": crossrepo._repo_meta(repo),
        "entry": {"route": ep.route, "handler": ep.handler, "kind": ep.kind,
                  "loc": f"{ep.file}:{ep.line}"},
        "steps": tr["steps"],
        "tables": tables,
        "stats": tr["stats"],
    }


def _walk_steps(steps):
    for step in steps:
        yield step
        yield from _walk_steps(step.get("children") or [])


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
    if step.get("set"):
        text += " set{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in step["set"].items()) + "}"
    if step.get("ref"):
        text += f" (see {step['ref']})"
    if step.get("truncated"):
        text += " ✂ not followed (depth/budget cap)"
    if step.get("loc"):
        text += f"  {step['loc']}"
    return text


def _render_steps(steps, prefix):
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
        if step.get("boundary"):
            lines.append(child_prefix + f"═══ {step['boundary']} ═══")
        lines.extend(_render_steps(step.get("children") or [], child_prefix))
    return lines


def render_ascii(flow):
    e = flow["entry"]
    lines = [f"FLOW {e['kind']} {e['route']} → {e['handler']} ({e['loc']})"]
    lines.extend(_render_steps(flow["steps"], ""))
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(prog="lenscheck flow",
                                description="Ordered steps one entrypoint executes.")
    p.add_argument("repo")
    p.add_argument("query", nargs="?")
    p.add_argument("--list", action="store_true", help="list every entrypoint candidate")
    p.add_argument("--pick", type=int, metavar="N", help="pick candidate N (1-based)")
    p.add_argument("--all", action="store_true", help="keep every step, including bare helpers")
    p.add_argument("--out", metavar="FILE", help="write the flow as JSON")
    args = p.parse_args()

    if args.list:
        for c in candidates(args.repo):
            print(f"{c.get('kind', ''):8} {c.get('route', ''):30} {c.get('handler', ''):20} {c.get('loc', '')}")
        return 0
    if not args.query:
        p.print_usage(sys.stderr)
        return 2

    try:
        flow = build_flow(args.repo, args.query, pick=args.pick, keep_all=args.all)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
