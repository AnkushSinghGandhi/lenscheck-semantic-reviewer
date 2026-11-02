"""Cross-repo provenance — which DB tables are written/read by endpoints across MORE THAN
ONE repo (a multi-service system sharing a database). Reuses `provenance.build()` per repo
(the same endpoint extractor), so every cross-repo link is endpoint-level (route / handler /
file:line), not just a table name. Flags cross-service MULTIPLE WRITERS — the consistency
hazard when two services write the same table.

Local, key-free. JSON contract on stdout (like `provenance`); optional self-contained HTML
with live search + kind filters + collapsible rows.
"""
import argparse
import hashlib
import html as _h
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import provenance
import xrepo_graph


def _resolve(spec):
    """'path' or 'path@branch'. Returns (display_name, analyze_path, cleanup_fn).
    For a branch, check it out into a throwaway git worktree so the caller's
    working tree is never touched (non-destructive, parallel-safe)."""
    repo, sep, branch = spec.rpartition("@")
    if not sep or not os.path.isdir(repo):      # no '@', or left side isn't a dir -> plain path
        repo, branch = spec, None
    base = os.path.basename(os.path.abspath(repo)) or repo
    if not branch:
        return base, repo, (lambda: None)
    if branch.startswith("-"):           # it lands in git's argv: `repo@--foo` must never become an option
        raise RuntimeError(f"refusing an option-like git ref: {branch!r}")
    wt = tempfile.mkdtemp(prefix="lc_crossrepo_")
    r = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", "--force", wt, branch],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(wt, ignore_errors=True)
        raise RuntimeError(f"git worktree for {repo}@{branch} failed: {r.stderr.strip()[-200:]}")
    return f"{base}@{branch}", wt, (
        lambda: (subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                                capture_output=True), shutil.rmtree(wt, ignore_errors=True)))

_CSS = """
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#fafafa;color:#1a1a1a}
.wrap{max-width:1100px;margin:0 auto;padding:1.4rem 2rem 3rem}
h1{font-size:1.35rem;margin:0 0 .2rem}.sub{color:#555;margin:0 0 .8rem;font-size:13px}
.toolbar{position:sticky;top:0;background:rgba(250,250,250,.96);backdrop-filter:blur(4px);padding:.7rem 0;margin-bottom:1rem;border-bottom:1px solid #e5e7eb;z-index:5}
.toolbar input[type=search]{width:100%;padding:.55rem .8rem;font-size:14px;border:1px solid #d1d5db;border-radius:8px;margin-bottom:.55rem}
.chips{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:.35rem;padding:.2rem .6rem;border:1px solid #d1d5db;border-radius:999px;font-size:12px;cursor:pointer;user-select:none;background:#fff}
.chip.mw{border-color:#f1b0b7}.chip.pc{border-color:#f0dca0}.chip input{margin:0}
.btn{font-size:12px;color:#2471a3;cursor:pointer;background:none;border:none;padding:0 .3rem}
#count{margin-left:auto;color:#777;font-size:12px}
.card{background:#fff;border:1px solid #e5e7eb;border-left:4px solid #cbd5e1;border-radius:8px;margin-bottom:.45rem;overflow:hidden}
.card[data-kind=multi_writer]{border-left-color:#c0392b}.card[data-kind=producer_consumer]{border-left-color:#d4a017}
summary{cursor:pointer;padding:.55rem .9rem;font-size:13px;display:flex;align-items:center;gap:.5rem;list-style:none}
summary::-webkit-details-marker{display:none}summary:hover{background:#f8fafc}
summary code{font-family:ui-monospace,Menlo,monospace;font-weight:600}
.meta{color:#999;font-weight:400;font-size:12px;margin-left:auto}
.body{padding:.2rem .9rem 1rem;border-top:1px solid #f0f0f0}
.repos{display:flex;gap:1rem;flex-wrap:wrap;margin-top:.5rem}
.repo{flex:1;min-width:260px;border-left:3px solid #ddd;padding-left:.7rem}.repo.absent{opacity:.45}
.rw{margin:.3rem 0}.w{color:#c0392b;font-weight:700;font-size:11px}.r{color:#2471a3;font-weight:700;font-size:11px}
ul{margin:.2rem 0;padding-left:1rem}li{font-size:12px}code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.h{color:#555}.loc{color:#999;font-size:11px}.none{color:#bbb}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:4px;font-size:11px;font-weight:600;background:#eef;color:#446}
.badge.red{background:#fdecea;color:#c0392b}.badge.amber{background:#fef3cd;color:#8a6d0b}.more{color:#999;list-style:none}
#empty{display:none;color:#999;padding:2rem;text-align:center}
.legend{color:#666;font-size:12px;margin:.2rem 0 .9rem;line-height:1.7}
.legend b{font-weight:600}
"""

_JS = """<script>
(function(){
 var q=document.getElementById('q'),count=document.getElementById('count'),empty=document.getElementById('empty');
 var kfs=[].slice.call(document.querySelectorAll('.kf'));
 var cards=[].slice.call(document.querySelectorAll('.card'));
 function apply(){
  var term=q.value.trim().toLowerCase(),kinds={};
  kfs.forEach(function(k){kinds[k.value]=k.checked;});
  var n=0;
  cards.forEach(function(c){
   var ok=(!term||c.getAttribute('data-table').indexOf(term)>=0)&&kinds[c.getAttribute('data-kind')];
   c.style.display=ok?'':'none';if(ok)n++;
  });
  count.textContent=n+' shown';empty.style.display=n?'none':'block';
 }
 q.addEventListener('input',apply);
 kfs.forEach(function(k){k.addEventListener('change',apply);});
 document.getElementById('expand').addEventListener('click',function(){cards.forEach(function(c){if(c.style.display!=='none')c.open=true;});});
 document.getElementById('collapse').addEventListener('click',function(){cards.forEach(function(c){c.open=false;});});
 q.focus();apply();
})();
</script>"""

_BADGE = {
    "multi_writer": ("badge red", "cross-service multi-writer"),
    "producer_consumer": ("badge amber", "producer &rarr; consumer"),
    "read_only": ("badge", "read-only shared"),
}
_ORDER = {"multi_writer": 0, "producer_consumer": 1, "read_only": 2}

_PARAM = re.compile(r"[<>{}():\\?+*\[\]|^$]")    # a param segment: <int:pk> {id} :id (?P<pk>…), or a regex fragment (v?\d+)


def _seg(s):
    return "*" if (not s or _PARAM.search(s)) else s.lower()


def _path_segs(path):
    return [_seg(s) for s in path.strip("/").split("/") if s]


def _route_segs(route):
    r = route.strip().lstrip("^").rstrip("$")        # tolerate re_path anchors
    return [_seg(s) for s in r.strip("/").split("/") if s]


def _route_match(call_segs, route_segs):
    """True if a route pattern is a segment-suffix of an outbound call path, params as wildcards, with
    ≥1 real literal matched. Suffix (not equality) so a proxy mount prefix (`/svc/api/x` vs route `api/x`)
    still links. Fed an include()-chained FULL route this is precise; fed a bare leaf it's a heuristic."""
    if not route_segs or len(route_segs) > len(call_segs):
        return False
    lits = 0
    for c, r in zip(call_segs[-len(route_segs):], route_segs):
        if r == "*":
            continue
        if c == "*" or c != r:
            return False
        lits += 1
    return lits >= 1


def _link_targets(e):
    """The route(s) an endpoint is served at: its include()-chained full routes when known (precise),
    else the local leaf pattern (heuristic)."""
    full = e.get("full_routes") or []
    return [(r, True) for r in full] or [(e.get("route", ""), False)]


def _api_links(per):
    """Cross-repo HTTP coupling: repo A's endpoint makes an outbound call whose path matches a route
    served by repo B — the complement to shared-table coupling. `match` is "exact" when the call path IS
    the full route (trustworthy), else "tail" (a leaf-only route, or an unexplained extra prefix — verify)."""
    routes = {name: [(e, r, _route_segs(r), full) for e in data.get("endpoints", [])
                     for r, full in _link_targets(e)]
              for name, data in per.items()}
    links = {}
    for name, data in per.items():
        for e in data.get("endpoints", []):
            for call in e.get("calls", []):
                cs = _path_segs(call)
                if not cs:
                    continue
                for other, rlist in routes.items():
                    if other == name:
                        continue
                    for tgt, r, rsegs, full in rlist:
                        if not _route_match(cs, rsegs):
                            continue
                        key = (name, e["loc"], other, tgt["loc"], call)
                        exact = full and len(cs) == len(rsegs)
                        if key in links and (links[key]["match"] == "exact" or not exact):
                            continue                   # keep the first hit, but let an exact one replace a tail
                        links[key] = {
                            "from_repo": name, "call": call, "match": "exact" if exact else "tail",
                            "from": {k: e.get(k, "") for k in ("route", "handler", "loc")},
                            "to_repo": other,
                            "to": {"route": r, "handler": tgt.get("handler", ""), "loc": tgt.get("loc", "")}}
    return sorted(links.values(), key=lambda x: (x["from_repo"], x["to_repo"], x["call"]))


def _task_links(per):
    """Cross-repo Celery coupling: repo A dispatches a task (`send_task('name')`, `fn.delay()`) that repo B
    defines. `match` is "exact" when a `send_task` name is the task's explicit or full default name, else
    "tail" (only the function name matches: verify). A repo that defines a same-named task itself is taken
    to be calling its own, so it never links out."""
    tasks = {name: [e for e in data.get("endpoints", []) if e.get("kind") == "task"]
             for name, data in per.items()}

    def match(mech, target, t):
        names = set(t.get("task_names") or []) - {t["handler"]}
        if mech == "celery.send_task" and "." in target and \
                (target in names or any(n.endswith("." + target) for n in names)):
            return "exact"                       # explicit name, or the default one under another import root
        return "tail" if target.rsplit(".", 1)[-1] == t["handler"] else None

    links = {}
    for name, data in per.items():
        own = {n for t in tasks[name] for n in (t.get("task_names") or [])}
        own_fns = {t["handler"] for t in tasks[name]}
        for e in data.get("endpoints", []):
            for d in e.get("dispatches", []):
                tgt, mech = d["target"], d["mech"]
                if tgt in own or tgt.rsplit(".", 1)[-1] in own_fns:
                    continue
                for other, tlist in tasks.items():
                    if other == name:
                        continue
                    for t in tlist:
                        m = match(mech, tgt, t)
                        if not m:
                            continue
                        key = (name, d["loc"], other, t["loc"])
                        if key in links and (links[key]["match"] == "exact" or m != "exact"):
                            continue
                        links[key] = {
                            "from_repo": name, "dispatch": f"{mech} -> {tgt}", "match": m,
                            "from": {"route": e.get("route", ""), "handler": e.get("handler", ""),
                                     "loc": e.get("loc", ""), "dispatch_loc": d["loc"]},
                            "to_repo": other,
                            "to": {"task": t["handler"], "names": t.get("task_names") or [],
                                   "loc": t.get("loc", "")}}
    return sorted(links.values(), key=lambda x: (x["from_repo"], x["to_repo"], x["dispatch"]))


def _repo_meta(path):
    """Commit + dirty flag of an analyzed repo, so a report says exactly which code it describes."""
    def git(*a):
        r = subprocess.run(["git", "--no-optional-locks", "-C", path, *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def _tool_version():
    try:
        from importlib.metadata import version
        return version("lenscheck-semantic-reviewer")
    except Exception:                            # running from a source checkout
        return None


def _db_of(name, db_map):
    """The database a repo's tables live in: its alias from `--db NAME=ALIAS` (matched on the display name
    or the bare folder name of a NAME@BRANCH), else "" (the shared default)."""
    for k, v in (db_map or {}).items():
        if name == k or name.startswith(k + "@"):
            return v
    return ""


def _changed_files(repo_path, since):
    """Repo-relative paths changed in `repo_path`: diff vs REF (`since`), else uncommitted changes
    vs HEAD (staged + unstaged + untracked, so a brand-new module counts)."""
    try:
        if since:
            r = subprocess.run(["git", "-C", repo_path, "diff", "--name-only", since],
                               capture_output=True, text=True)
            return set(r.stdout.split())
        r = subprocess.run(["git", "-C", repo_path, "status", "--porcelain"], capture_output=True, text=True)
        return {ln[3:] for ln in r.stdout.splitlines() if ln.strip()}
    except Exception:
        return set()


def _touches(loc, changed_files):
    return bool(loc) and loc.split(":")[0] in changed_files


def _blast(data, changed_name, changed_files):
    """Keep only the cross-repo tables + API-links that the changed files in `changed_name` touch.
    Each kept table flags whether the change was a WRITE (bigger blast — downstream readers / other
    writers can break)."""
    tables = []
    for s in data["shared_tables"]:
        acc = s["by_repo"].get(changed_name) or {}
        w = [e for e in acc.get("written_by", []) if _touches(e.get("loc"), changed_files)]
        r = [e for e in acc.get("read_by", []) if _touches(e.get("loc"), changed_files)]
        if not w and not r:
            continue
        s = dict(s)
        s["blast"] = {"changed_write": bool(w), "changed_endpoints": [e["loc"] for e in w + r]}
        tables.append(s)
    links = [l for l in data.get("api_links", [])
             if (l["from_repo"] == changed_name and _touches(l["from"]["loc"], changed_files))
             or (l["to_repo"] == changed_name and _touches(l["to"]["loc"], changed_files))]
    return tables, links


def _blast_tasks(data, changed_name, changed_files):
    """Keep only the cross-repo task links the changed files touch, on either side."""
    return [l for l in data.get("task_links", [])
            if (l["from_repo"] == changed_name and (_touches(l["from"]["loc"], changed_files)
                                                    or _touches(l["from"]["dispatch_loc"], changed_files)))
            or (l["to_repo"] == changed_name and _touches(l["to"]["loc"], changed_files))]


_ENGINE_FP = None


def _engine_fingerprint():
    """Hash of the extraction code, folded into the cache key. `provenance.SCHEMA` only bumps on a
    JSON-*shape* change, so a recall/precision fix to the extractor (e.g. the `cls.x()` follow) leaves
    it unchanged — without this a clean repo would keep returning pre-upgrade facts from a stale entry."""
    global _ENGINE_FP
    if _ENGINE_FP is None:
        import extractor.analyzer
        import extractor.frameworks
        h = hashlib.sha1()
        for mod in (provenance, extractor.analyzer, extractor.frameworks):
            try:
                with open(mod.__file__, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"?")
        _ENGINE_FP = h.hexdigest()[:12]
    return _ENGINE_FP


# Pin the fingerprint to the code loaded NOW. Computed lazily, a long-running `serve` that loaded the old
# extractor would hash the files edited on disk since → cache old-code facts under the NEW key.
# shortcut: still a ms window between the extractor import and this line; exact = hash the loaded code
_engine_fingerprint()


def _cache_dir():
    d = os.path.join(tempfile.gettempdir(), "lenscheck_provcache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d


def _clean_head(path):
    """HEAD sha when `path` is a git repo with a CLEAN tree, else None. A dirty tree or non-repo
    returns None → that repo always re-extracts, so a cached result is never stale.
    shortcut: only clean checkouts are cached; upgrade to a content fingerprint if dirty re-runs matter."""
    try:
        h = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
        st = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True)
        if h.returncode or st.returncode or st.stdout.strip():
            return None
        return h.stdout.strip()
    except Exception:
        return None


def _provenance(path, with_calls, use_cache):
    """provenance.build, cached by git HEAD. A commit sha is globally unique, so the sha alone keys
    it — same repo re-run (or an @branch worktree at the same commit) hits instantly."""
    if not use_cache:
        return provenance.build(path, with_calls=with_calls)
    head, d = _clean_head(path), _cache_dir()
    if not head or not d:
        return provenance.build(path, with_calls=with_calls)
    fp = os.path.join(d, hashlib.sha1(
        f"{head}|{with_calls}|{provenance.SCHEMA}|{_engine_fingerprint()}".encode()).hexdigest() + ".json")
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    data = provenance.build(path, with_calls=with_calls)
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    return data


def build(repos, writes_only=False, schema=None, use_cache=True, db_map=None):
    per, meta = {}, {}
    cleanups = []
    try:
        for spec in repos:                       # each is 'path' or 'path@branch'
            name, path, cleanup = _resolve(spec)
            cleanups.append(cleanup)
            per[name] = _provenance(path, True, use_cache)
            meta[name] = _repo_meta(path)
    finally:
        for c in cleanups:
            try:
                c()
            except Exception:
                pass

    # keyed by (database alias, table): same-named tables in different databases are different tables
    merged = {}
    for name, data in per.items():
        db = _db_of(name, db_map)
        for tbl, rec in data.get("tables", {}).items():
            if tbl == "<unresolved>":          # a per-repo "couldn't name this write" bucket — not a real
                continue                       # shared table; two repos' unresolved writes aren't the same one
            merged.setdefault((db, tbl), {})[name] = {
                "written_by": rec.get("written_by", []),
                "read_by": rec.get("read_by", []),
            }

    shared = []
    for db, tbl in sorted(merged):
        slot = merged[(db, tbl)]
        if len(slot) < 2:
            continue                     # touched by only one repo — not cross-repo
        writers = sorted(r for r, a in slot.items() if a["written_by"])
        kind = ("multi_writer" if len(writers) >= 2
                else "producer_consumer" if len(writers) == 1 else "read_only")
        if writes_only and kind == "read_only":
            continue
        shared.append({
            "table": tbl, "database": db, "repos": sorted(slot), "writers": writers,
            "readers": sorted(r for r, a in slot.items() if a["read_by"]),
            "multi_writer": len(writers) >= 2, "kind": kind,
            "unique_keys": [list(k) for k in (schema or {}).get(tbl, [])], "by_repo": slot,
        })

    api_links = _api_links(per)
    task_links = _task_links(per)

    def n(k):
        return sum(1 for s in shared if s["kind"] == k)
    out = {
        "schema": 1, "generated_by": "lenscheck crossrepo", "repos": sorted(per),
        "writes_only": writes_only,
        "summary": {"repos": len(per), "shared_tables": len(shared),
                    "multi_writer": n("multi_writer"),
                    "producer_consumer": n("producer_consumer"), "read_only": n("read_only"),
                    "api_links": len(api_links), "task_links": len(task_links)},
        "per_repo": {n2: d.get("summary", {}) for n2, d in per.items()},
        "repo_meta": {n2: meta[n2] for n2 in sorted(meta)}, "tool_version": _tool_version(),
        "shared_tables": shared,
        "api_links": api_links,
        "task_links": task_links,
    }
    if db_map:
        out["databases"] = {name: _db_of(name, db_map) for name in sorted(per)}
    return out


def _eps(lst, limit=6):
    if not lst:
        return '<span class="none">—</span>'
    rows = "".join(
        f'<li><code>{_h.escape(e.get("route",""))}</code> '
        f'<span class="h">{_h.escape(e.get("handler",""))}</span> '
        f'<span class="loc">{_h.escape(e.get("loc",""))}</span></li>' for e in lst[:limit])
    more = f'<li class="more">+{len(lst) - limit} more</li>' if len(lst) > limit else ""
    return f"<ul>{rows}{more}</ul>"


def write_html(data, path):
    repos = data["repos"]
    cards = []
    for s in sorted(data["shared_tables"], key=lambda x: (_ORDER[x["kind"]], x["table"])):
        per = []
        for r in repos:
            acc = s["by_repo"].get(r)
            if not acc:
                per.append(f'<div class="repo absent"><b>{_h.escape(r)}</b> '
                           f'<span class="none">not touched</span></div>')
                continue
            per.append(
                f'<div class="repo"><b>{_h.escape(r)}</b>'
                f'<div class="rw"><span class="w">WRITES</span> {_eps(acc["written_by"])}</div>'
                f'<div class="rw"><span class="r">READS</span> {_eps(acc["read_by"])}</div></div>')
        cls, label = _BADGE[s["kind"]]
        uk = s.get("unique_keys") or []
        uk_html = ""
        if uk:
            keys = _h.escape("; ".join("+".join(k) for k in uk))
            uk_html = (f'<div style="color:#c0392b;font-weight:600;font-size:12px;margin:.1rem 0 .4rem">'
                       f'⚠ coordinate on unique key <code>{keys}</code> — both writers must agree</div>'
                       if s["kind"] == "multi_writer" else
                       f'<div class="meta" style="margin:.1rem 0 .4rem">unique key: <code>{keys}</code></div>')
        cards.append(
            f'<details class="card" data-kind="{s["kind"]}" data-table="{_h.escape(s["table"].lower())}">'
            f'<summary><code>{_h.escape((s["database"] + ".") if s.get("database") else "")}'
            f'{_h.escape(s["table"])}</code> <span class="{cls}">{label}</span>'
            f'<span class="meta">writers: {", ".join(s["writers"]) or "—"}</span></summary>'
            f'<div class="body">{uk_html}<div class="repos">{"".join(per)}</div></div></details>')

    su = data["summary"]
    scope = "write-coupled only" if data.get("writes_only") else "all shared"
    ro_chip = ("" if data.get("writes_only") else
               f'<label class="chip"><input type="checkbox" class="kf" value="read_only" checked> '
               f'read-only ({su["read_only"]})</label>')
    head = ('<!doctype html><html><head><meta charset="utf-8">'
            '<title>Lenscheck — cross-repo provenance</title><style>' + _CSS + '</style></head>'
            '<body><div class="wrap">')
    hdr = (f'<h1>Cross-repo provenance</h1>'
           f'<p class="sub">{", ".join(_h.escape(r) for r in repos)} · '
           f'{su["shared_tables"]} tables ({scope})</p>'
           '<p class="legend">'
           '🔴 <b>multi-writer</b>: 2+ repos WRITE this table → they can race/overwrite, coordinate schema &amp; write logic. · '
           '🟡 <b>producer→consumer</b>: one repo writes, others only read → the writer owns the shape; if it changes, readers break. · '
           '<b>read-only</b>: no repo writes it (shared lookup) → usually safe.'
           '</p>')
    toolbar = (
        '<div class="toolbar">'
        '<input id="q" type="search" placeholder="filter by table name…">'
        '<div class="chips">'
        f'<label class="chip mw"><input type="checkbox" class="kf" value="multi_writer" checked> '
        f'🔴 multi-writer ({su["multi_writer"]})</label>'
        f'<label class="chip pc"><input type="checkbox" class="kf" value="producer_consumer" checked> '
        f'🟡 producer→consumer ({su["producer_consumer"]})</label>'
        f'{ro_chip}'
        '<button class="btn" id="expand">expand all</button>'
        '<button class="btn" id="collapse">collapse all</button>'
        '<span id="count"></span></div></div>')
    body = '<div id="list">' + "".join(cards) + '</div><div id="empty">no tables match</div>'
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + hdr + _blast_html(data) + _impact_html(data) + toolbar + body
                + _api_links_html(data) + _task_links_html(data) + "</div>" + _JS + "</body></html>")


def _blast_html(data):
    b = data.get("blast")
    if not b:
        return ""
    files = "".join(f"<li><code>{_h.escape(f)}</code></li>" for f in b["changed_files"][:40])
    more = (f'<li class="more">+{len(b["changed_files"]) - 40} more</li>'
            if len(b["changed_files"]) > 40 else "")
    return (f'<div style="background:#fff8e1;border:1px solid #f0dca0;border-radius:8px;padding:.7rem 1rem;margin:.4rem 0 1rem">'
            f'<b>💥 Blast radius</b> — changes in <code>{_h.escape(b["changed_repo"])}</code> '
            f'(vs {_h.escape(b["since"])}) couple to <b>{b["tables"]}</b> shared table(s) and '
            f'<b>{b["links"]}</b> API link(s) in the other repo(s). Fix below, then check those endpoints.'
            f'<details style="margin-top:.4rem"><summary style="cursor:pointer;font-size:12px;color:#8a6d0b">'
            f'{len(b["changed_files"])} changed files</summary>'
            f'<ul style="margin:.3rem 0">{files}{more}</ul></details></div>')


def _impact_html(data):
    imp = data.get("impact") or {}
    if not imp.get("affected"):
        return ""
    rows = "".join(
        f'<li><b>{_h.escape(a["repo"])}</b> <code>{_h.escape(a["handler"] or a["route"])}</code> '
        f'<span class="loc">{_h.escape(a["loc"])}</span> — {_h.escape(a["kind"])}, depth {a["depth"]}'
        + ('' if a["confidence"] == "exact" else ' <span class="badge amber">verify</span>')
        + f'<div class="loc">{" → ".join(_h.escape(h) for h in a["path"])}</div></li>'
        for a in imp["affected"])
    return (f'<h2 style="font-size:1.05rem;margin:1rem 0 .2rem">🔗 Impact in other repos '
            f'({len(imp["affected"])})</h2>'
            f'<ul style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:.6rem 1.4rem">{rows}</ul>')


def _api_links_html(data):
    links = data.get("api_links") or []
    if not links:
        return ""
    def badge(l):
        return ('<span class="badge" style="background:#e6f4ea;color:#1e7e34">exact</span>'
                if l.get("match") == "exact" else '<span class="badge amber">tail · verify</span>')
    rows = "".join(
        f'<li>{badge(l)} <b>{_h.escape(l["from_repo"])}</b> <code>{_h.escape(l["from"]["handler"])}</code> '
        f'<span class="loc">{_h.escape(l["from"]["loc"])}</span> &rarr; call <code>{_h.escape(l["call"])}</code> '
        f'&rarr; <b>{_h.escape(l["to_repo"])}</b> <code>{_h.escape(l["to"]["handler"])}</code> '
        f'<span class="h">{_h.escape(l["to"]["route"])}</span> '
        f'<span class="loc">{_h.escape(l["to"]["loc"])}</span></li>' for l in links)
    n_exact = sum(1 for l in links if l.get("match") == "exact")
    return (f'<h2 style="font-size:1.05rem;margin:1.6rem 0 .2rem">Cross-repo API calls ({len(links)})</h2>'
            f'<p class="legend"><b>exact</b> ({n_exact}): the call path is the endpoint\'s full '
            f'include()-chained route — trustworthy. · <b>tail</b> ({len(links) - n_exact}): matched on a '
            'leaf route or with an extra prefix (e.g. a proxy mount) — <b>verify</b>.</p>'
            f'<ul style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:.6rem 1.4rem">{rows}</ul>')


def _task_links_html(data):
    links = data.get("task_links") or []
    if not links:
        return ""
    def badge(l):
        return ('<span class="badge" style="background:#e6f4ea;color:#1e7e34">exact</span>'
                if l.get("match") == "exact" else '<span class="badge amber">tail · verify</span>')
    rows = "".join(
        f'<li>{badge(l)} <b>{_h.escape(l["from_repo"])}</b> <code>{_h.escape(l["from"]["handler"])}</code> '
        f'<span class="loc">{_h.escape(l["from"]["dispatch_loc"])}</span> &rarr; <code>{_h.escape(l["dispatch"])}</code> '
        f'&rarr; <b>{_h.escape(l["to_repo"])}</b> task <code>{_h.escape(l["to"]["task"])}</code> '
        f'<span class="loc">{_h.escape(l["to"]["loc"])}</span></li>' for l in links)
    n_exact = sum(1 for l in links if l.get("match") == "exact")
    return (f'<h2 style="font-size:1.05rem;margin:1.6rem 0 .2rem">Cross-repo Celery tasks ({len(links)})</h2>'
            f'<p class="legend"><b>exact</b> ({n_exact}): <code>send_task</code> uses the task\'s explicit or '
            f'full default name. · <b>tail</b> ({len(links) - n_exact}): only the function name matches '
            '(e.g. <code>fn.delay()</code>) — <b>verify</b>.</p>'
            f'<ul style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:.6rem 1.4rem">{rows}</ul>')


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="lenscheck crossrepo",
        description="Table provenance ACROSS repos (endpoint-level). Flags cross-service multiple writers.")
    ap.add_argument("repos", nargs="+",
                    help="two or more repos as PATH or PATH@BRANCH (read-only; a branch is "
                         "analyzed in a throwaway git worktree, your checkout is untouched)")
    ap.add_argument("--out", metavar="FILE", help="write the JSON")
    ap.add_argument("--html", metavar="FILE", help="write a self-contained HTML report")
    ap.add_argument("--writes-only", action="store_true",
                    help="drop read-only-shared tables; keep only write coupling (multi-writer + producer/consumer)")
    ap.add_argument("--schema", metavar="FILE",
                    help="DB-schema JSON (table -> unique keys, from schema_dump) so multi-writer "
                         "tables get a column-level 'coordinate on unique key' rule")
    ap.add_argument("--no-cache", action="store_true",
                    help="re-extract every repo (default: cache per-repo provenance by git HEAD for clean trees)")
    ap.add_argument("--changed", metavar="PATH",
                    help="blast-radius mode: a repo (also in REPOS) whose changed files seed the radius — "
                         "show only the cross-repo tables/API-links those changes touch")
    ap.add_argument("--since", metavar="REF",
                    help="with --changed: diff against REF (default: uncommitted changes vs HEAD)")
    ap.add_argument("--db", metavar="NAME=ALIAS", action="append", default=[],
                    help="which database a repo's tables live in (NAME = repo folder name). Tables only "
                         "join across repos with the same alias; repos not listed share one default "
                         "database. Repeat per repo, e.g. --db orders=main --db billing=main --db audit=logs")
    ap.add_argument("--compact", action="store_true", help="single-line JSON")
    a = ap.parse_args(argv)

    db_map = {}
    for item in a.db:
        k, sep, v = item.partition("=")
        if not sep or not k.strip() or not v.strip():
            print(f"lenscheck crossrepo: --db expects NAME=ALIAS, got {item!r}", file=sys.stderr)
            return 3
        db_map[k.strip()] = v.strip()

    for r in a.repos:
        head, sep, _ = r.rpartition("@")
        path = head if (sep and os.path.isdir(head)) else r
        if not os.path.isdir(path):
            print(f"lenscheck crossrepo: not a directory: {path}", file=sys.stderr)
            return 3
    if len(a.repos) < 2:
        print("lenscheck crossrepo: need at least two repos", file=sys.stderr)
        return 3
    try:
        import schema_dump
        data = build(a.repos, writes_only=a.writes_only, schema=schema_dump.load(a.schema),
                     use_cache=not a.no_cache, db_map=db_map)
    except Exception as e:                       # never leak a traceback onto parsed stdout
        print(f"lenscheck crossrepo: failed: {e}", file=sys.stderr)
        return 3
    unknown = [k for k in db_map if not any(n == k or n.startswith(k + "@") for n in data["repos"])]
    if unknown:                                  # a typo would silently fall back to the default database
        print(f"lenscheck crossrepo: --db names not among analyzed repos {data['repos']}: {unknown}",
              file=sys.stderr)
        return 3

    if a.changed:
        base = a.changed.rpartition("@")[0] or a.changed
        cbase = os.path.basename(os.path.abspath(base))
        cname = next((r for r in data["repos"] if r == cbase or r.startswith(cbase + "@")), None)
        if not cname:
            print(f"lenscheck crossrepo: --changed '{cbase}' not among analyzed repos {data['repos']}",
                  file=sys.stderr)
            return 3
        cfiles = _changed_files(base, a.since)
        graph = xrepo_graph.build(data)
        data["impact"] = xrepo_graph.impact(graph, xrepo_graph.seeds(graph, cname, cfiles))
        btables, blinks = _blast(data, cname, cfiles)
        btasks = _blast_tasks(data, cname, cfiles)
        data["shared_tables"], data["api_links"], data["task_links"] = btables, blinks, btasks
        data["summary"]["task_links"] = len(btasks)
        for k in ("multi_writer", "producer_consumer", "read_only"):
            data["summary"][k] = sum(1 for t in btables if t["kind"] == k)
        data["summary"]["shared_tables"] = len(btables)
        data["summary"]["api_links"] = len(blinks)
        data["blast"] = {"changed_repo": cname, "since": a.since or "uncommitted vs HEAD",
                         "changed_files": sorted(cfiles), "tables": len(btables), "links": len(blinks)}

    if a.html:
        write_html(data, a.html)
        print(f"wrote {a.html}: open it in a browser — no server needed", file=sys.stderr)
    blob = json.dumps(data, separators=(",", ":")) if a.compact else json.dumps(data, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(blob)
    s = data["summary"]
    scope = f" · blast radius of {data['blast']['changed_repo']}" if data.get("blast") else ""
    print(f"lenscheck crossrepo: {s['repos']} repos · {s['shared_tables']} tables · "
          f"{s['multi_writer']} multi-writer · {s['producer_consumer']} producer/consumer · "
          f"{s['read_only']} read-only · {s.get('api_links', 0)} API links · "
          f"{s.get('task_links', 0)} task links{scope}", file=sys.stderr)
    if not a.out and not a.html:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
