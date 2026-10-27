"""Cross-repo provenance — which DB tables are written/read by endpoints across MORE THAN
ONE repo (a multi-service system sharing a database). Reuses `provenance.build()` per repo
(the same endpoint extractor), so every cross-repo link is endpoint-level (route / handler /
file:line), not just a table name. Flags cross-service MULTIPLE WRITERS — the consistency
hazard when two services write the same table.

Local, key-free. JSON contract on stdout (like `provenance`); optional self-contained HTML
with live search + kind filters + collapsible rows.
"""
import argparse
import html as _h
import json
import os
import shutil
import subprocess
import sys
import tempfile

import provenance


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


def build(repos, writes_only=False):
    per = {}
    cleanups = []
    try:
        for spec in repos:                       # each is 'path' or 'path@branch'
            name, path, cleanup = _resolve(spec)
            cleanups.append(cleanup)
            per[name] = provenance.build(path)
    finally:
        for c in cleanups:
            try:
                c()
            except Exception:
                pass

    merged = {}
    for name, data in per.items():
        for tbl, rec in data.get("tables", {}).items():
            merged.setdefault(tbl, {})[name] = {
                "written_by": rec.get("written_by", []),
                "read_by": rec.get("read_by", []),
            }

    shared = []
    for tbl in sorted(merged):
        slot = merged[tbl]
        if len(slot) < 2:
            continue                     # touched by only one repo — not cross-repo
        writers = sorted(r for r, a in slot.items() if a["written_by"])
        kind = ("multi_writer" if len(writers) >= 2
                else "producer_consumer" if len(writers) == 1 else "read_only")
        if writes_only and kind == "read_only":
            continue
        shared.append({
            "table": tbl, "repos": sorted(slot), "writers": writers,
            "readers": sorted(r for r, a in slot.items() if a["read_by"]),
            "multi_writer": len(writers) >= 2, "kind": kind, "by_repo": slot,
        })

    def n(k):
        return sum(1 for s in shared if s["kind"] == k)
    return {
        "schema": 1, "generated_by": "lenscheck crossrepo", "repos": sorted(per),
        "writes_only": writes_only,
        "summary": {"repos": len(per), "shared_tables": len(shared),
                    "multi_writer": n("multi_writer"),
                    "producer_consumer": n("producer_consumer"), "read_only": n("read_only")},
        "per_repo": {n2: d.get("summary", {}) for n2, d in per.items()},
        "shared_tables": shared,
    }


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
        cards.append(
            f'<details class="card" data-kind="{s["kind"]}" data-table="{_h.escape(s["table"].lower())}">'
            f'<summary><code>{_h.escape(s["table"])}</code> <span class="{cls}">{label}</span>'
            f'<span class="meta">writers: {", ".join(s["writers"]) or "—"}</span></summary>'
            f'<div class="body"><div class="repos">{"".join(per)}</div></div></details>')

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
        f.write(head + hdr + toolbar + body + "</div>" + _JS + "</body></html>")


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
    ap.add_argument("--compact", action="store_true", help="single-line JSON")
    a = ap.parse_args(argv)

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
        data = build(a.repos, writes_only=a.writes_only)
    except Exception as e:                       # never leak a traceback onto parsed stdout
        print(f"lenscheck crossrepo: failed: {e}", file=sys.stderr)
        return 3

    if a.html:
        write_html(data, a.html)
        print(f"wrote {a.html}: open it in a browser — no server needed", file=sys.stderr)
    blob = json.dumps(data, separators=(",", ":")) if a.compact else json.dumps(data, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(blob)
    s = data["summary"]
    print(f"lenscheck crossrepo: {s['repos']} repos · {s['shared_tables']} tables · "
          f"{s['multi_writer']} multi-writer · {s['producer_consumer']} producer/consumer · "
          f"{s['read_only']} read-only", file=sys.stderr)
    if not a.out and not a.html:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
