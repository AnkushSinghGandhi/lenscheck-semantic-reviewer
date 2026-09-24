"""cloud.py — the thin client path: extract facts locally, send them to lenscheck-cloud, print.

This is the public/free side. It runs the extractor (which stays client-side), serializes the facts
graph, and POSTs it — no diff/ranking/invariant logic here (that's the private cloud). Activated when
a token is present (LENSCHECK_API_KEY or ~/.lenscheck/token); otherwise the CLI falls back to the
local engine (on-prem / pre-migration). See DESIGN.md.
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
import urllib.error
import warnings

from extractor import analyze_repo, to_dict
import gitutil

# Quiet Python 3.12's SyntaxWarning noise when the extractor parses the target repo's files.
warnings.filterwarnings("ignore", category=SyntaxWarning)


def _progress(msg):
    print(msg, file=sys.stderr, flush=True)

API_URL = os.environ.get("LENSCHECK_API_URL", "https://api.lenscheck.dev").rstrip("/")
TOKEN_PATH = os.path.expanduser("~/.lenscheck/token")


def token():
    if os.environ.get("LENSCHECK_API_KEY"):
        return os.environ["LENSCHECK_API_KEY"]
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def have_token():
    return bool(token())


# --- transport ---------------------------------------------------------------
def _req(method, path, payload=None, override_token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "lenscheck"}
    tok = override_token or token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(API_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{"error":"http error"}')
    except urllib.error.URLError as e:
        return 0, {"error": f"cannot reach {API_URL}: {e.reason}"}


# --- facts extraction (client-side) ------------------------------------------
def _facts_for(local_repo, ref):
    """Export a clean snapshot at `ref` and extract its facts graph — source never leaves."""
    _progress(f"  · extracting facts @ {ref} (reading the code locally, nothing uploaded yet)…")
    with tempfile.TemporaryDirectory() as tmp:
        gitutil.export(local_repo, ref, tmp)
        eps = analyze_repo(tmp)
        _progress(f"    found {len(eps)} endpoints @ {ref}")
        return {"v": 1, "ref": ref, "endpoints": [to_dict(e) for e in eps]}


def _repo_name(local_repo, given):
    """owner/name if we can read the git remote (so the cloud can verify visibility), else basename."""
    try:
        url = gitutil.sh("git", "-C", local_repo, "config", "--get", "remote.origin.url").strip()
        if url:
            return "/".join(url.rstrip("/").replace(".git", "").split("/")[-2:])
    except Exception:
        pass
    return os.path.basename(os.path.abspath(given))


# --- commands ----------------------------------------------------------------
def review(argv):
    ap = argparse.ArgumentParser(prog="lenscheck review")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--base"); ap.add_argument("--head"); ap.add_argument("--pr", default="0")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--out", help="write the markdown review to FILE (for `lenscheck post`)")
    ap.add_argument("--json", dest="json_out", nargs="?", const="__stdout__",
                    help="write the review JSON to FILE (or print it if no FILE)")
    a = ap.parse_args(argv)
    local = gitutil.ensure_local(a.repo)
    base = a.base or gitutil.default_branch(local)
    head = a.head or gitutil.current_branch(local)
    _progress(f"lenscheck: reviewing {base}..{head}")
    body = {"repo": _repo_name(local, a.repo), "pr": a.pr, "private": a.private,
            "base_facts": _facts_for(local, base), "head_facts": _facts_for(local, head)}
    _progress(f"  · uploading facts to {API_URL} and ranking…")
    code, out = _req("POST", "/api/v1/review", body)
    if code == 200 and isinstance(out, dict) and "review" in out:
        rev = out["review"]
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(rev.get("markdown") or rev.get("title", ""))
        if a.json_out and a.json_out != "__stdout__":
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(rev, f, indent=2)
    _render(code, out, a.json_out == "__stdout__", kind="review")


def map(argv):
    ap = argparse.ArgumentParser(prog="lenscheck map")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--risky", action="store_true"); ap.add_argument("--private", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    local = gitutil.ensure_local(a.repo)
    head = gitutil.current_branch(local)
    _progress(f"lenscheck: mapping {head}")
    body = {"repo": _repo_name(local, a.repo), "private": a.private, "risky": a.risky,
            "facts": _facts_for(local, head)}
    _progress(f"  · uploading facts to {API_URL} and ranking…")
    code, out = _req("POST", "/api/v1/map", body)
    _render(code, out, a.json, kind="map")


def usage(argv):
    code, out = _req("GET", "/api/v1/usage")
    print(json.dumps(out, indent=2))


def digest(argv):
    ap = argparse.ArgumentParser(prog="lenscheck digest")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    code, out = _req("GET", "/api/v1/digest")
    if a.json:
        print(json.dumps(out, indent=2)); return
    if code != 200 or "error" in (out or {}):
        sys.exit(f"lenscheck: {out.get('error', out)} (HTTP {code})")
    t = out.get("totals", {})
    print(f"org: {out.get('org')}  ·  {t.get('reviews', 0)} reviews  ·  "
          f"{t.get('repos', 0)} repos  ·  {t.get('open_risky', 0)} open risky"
          + ("  ·  moat: on" if out.get("moat") else ""))
    for s in ("🔴", "🟠", "🟡", "🟢"):
        print(f"  {s} {out.get('by_tier', {}).get(s, 0)}")
    for r in out.get("repos", [])[:20]:
        print(f"    {r.get('worst') or '·'} {r['repo']}  ({r['prs']} PRs)")


def _sample_commits(repo, n):
    """N commits evenly spaced across first-parent history, oldest -> newest."""
    shas = gitutil.sh("git", "-C", repo, "rev-list", "--first-parent", "HEAD").split()
    if not shas:
        return []
    if len(shas) <= n:
        picks = shas
    else:
        step = (len(shas) - 1) / (n - 1)
        picks = [shas[round(i * step)] for i in range(n)]
    return list(reversed([s[:10] for s in picks]))


def invariants(argv):
    if argv and argv[0] == "confirm":
        return _invariants_confirm(argv[1:])
    ap = argparse.ArgumentParser(prog="lenscheck invariants")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--snapshots", type=int, default=6)
    ap.add_argument("--out", default="invariants.discovered.json")
    a = ap.parse_args(argv)
    local = gitutil.ensure_local(a.repo)
    shas = _sample_commits(local, a.snapshots)
    if not shas:
        sys.exit("lenscheck: no commit history to sample")
    _progress(f"lenscheck: discovering invariants from {len(shas)} snapshots")
    snaps = [{"sha": sha, "facts": _facts_for(local, sha)} for sha in shas]
    _progress("  · uploading facts, mining rules in the cloud…")
    code, out = _req("POST", "/api/v1/invariants", {"snapshots": snaps})
    if code != 200 or "error" in (out or {}):
        sys.exit(f"lenscheck: {out.get('error', out)} (HTTP {code})")
    cands = out.get("candidates", [])
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cands, f, indent=2)
    print(f"\ndiscovered {len(cands)} candidate invariant(s) → {a.out}")
    for c in cands:
        print(f"  [{c.get('kind', '?')}] {c.get('id')} — {c.get('statement', '')}")
    print(f"\nConfirm the real ones (set \"confirmed\": true in {a.out}), then enforce them with:")
    print(f"  lenscheck invariants confirm {a.out}")


def _invariants_confirm(argv):
    path = argv[0] if argv else "invariants.discovered.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        sys.exit(f"lenscheck: no such file {path}")
    confirmed = [c for c in data if c.get("confirmed")]
    if not confirmed:
        sys.exit(f'lenscheck: nothing confirmed in {path} — set "confirmed": true on the rules you want')
    code, out = _req("PUT", "/api/v1/corpus", confirmed)
    if code != 200:
        sys.exit(f"lenscheck: {out.get('error', out)} (HTTP {code})")
    print(f"confirmed {len(confirmed)} invariant(s) — now enforced on every review for your org.")


# --- output ------------------------------------------------------------------
def _render(code, out, as_json, kind):
    if as_json:
        print(json.dumps(out, indent=2)); return
    if code != 200 or "error" in (out or {}):
        sys.exit(f"lenscheck: {out.get('error', out)} (HTTP {code})")
    if kind == "map":
        m = out["map"]
        print(f"{m['total']} endpoints  ·  " +
              "  ".join(f"{s}{n}" for s, n in m["counts"].items()))     # sev keys are emojis
        for e in m["endpoints"]:
            print(f"  {e['severity']} {e['route']:34} {e.get('auth', '')}")
        return
    r = out["review"]
    print(r["title"])
    for c in r["changes"]:
        print(f"  {c['sev']} {c['kind']:16} {c['route']}")             # sev is already an emoji
        if c.get("why"):
            print(f"     {c['why']}")
    for v in r.get("invariants", []):
        print(f"  ⛔ {v.get('rule', v.get('id', 'rule'))}  {v.get('route', '')} — {v.get('why', '')}")
    u = out.get("usage")
    if u:
        print(f"\n  reviews used: {u['used']}/{u['limit']}")
