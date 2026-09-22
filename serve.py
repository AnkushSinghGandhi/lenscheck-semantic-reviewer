"""serve.py — thin local web UI for Lenscheck.

Serves web/app.html and backs it with the CLOUD (no engine here). `/api/review` and `/api/map`
extract facts locally and call the Lenscheck cloud; `/api/refs` and `/api/source` use local git.
Needs LENSCHECK_API_KEY (same as the CLI). Your source never leaves — only the facts graph.

Hosting the public demo (Phase 1):
    lenscheck serve --demo-dir demo              # serve pre-baked reviews (no key, no cloning)
    lenscheck serve --public --demo-dir demo     # + let visitors paste a public repo URL
The `--public` path shallow-clones each requested repo into a small, delete-after-use cache
(bounded to a few repos, reaped by TTL) so disk stays tiny. Public https git hosts only, with an
optional LENSCHECK_ALLOWED_REPOS allowlist.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cloud
import gitutil

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_PUBLIC_HOSTS = ("https://github.com/", "https://gitlab.com/", "https://bitbucket.org/")


def _git(repo, *a):
    try:
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception:
        return ""


def _commits(repo, n=50):
    out = _git(repo, "log", "--first-parent", f"-{n}", "--pretty=%h%x09%s")
    res = []
    for line in out.splitlines():
        sha, _, subj = line.partition("\t")
        if sha:
            res.append({"sha": sha, "subject": subj})
    return res


def _source_snippet(repo, ref, rel, line, ctx=6):
    blob = _git(repo, "show", f"{ref}:{rel}")
    lines = blob.splitlines()
    lo, hi = max(1, line - ctx), min(len(lines), line + ctx)
    return {"path": rel, "ref": ref, "line": line,
            "lines": [{"n": i, "text": lines[i - 1]} for i in range(lo, hi + 1)]}


def _repo_allowed(url, allow):
    """A pasted repo is reviewable only if it's a public https git URL (and matches the allowlist,
    if one is set). No local paths, no ssh — this runs on a public box."""
    if not gitutil.is_url(url):
        return False
    if not url.startswith(_PUBLIC_HOSTS):
        return False
    if allow and not any(a and a in url for a in allow):
        return False
    return True


class RepoCache:
    """Short-lived shallow clones of public repos for the hosted demo.

    Bounded to `cap` repos: the least-recently-used clone is deleted when full, and any clone older
    than `ttl` seconds is reaped on the next access. So disk never holds more than a few repos and
    every clone is deleted soon after use — the 'delete-after-review' property. Thread-safe.
    """

    def __init__(self, cap=3, ttl=600, depth=50, clone_timeout=60):
        self.cap, self.ttl, self.depth, self.clone_timeout = cap, ttl, depth, clone_timeout
        self._entries = {}                 # url -> {"path": str, "at": float}
        self._lock = threading.Lock()

    def get(self, url):
        now = time.time()
        with self._lock:
            self._reap(now)
            e = self._entries.get(url)
            if e:
                e["at"] = now
                return e["path"]
        path = self._clone(url)            # slow — done outside the lock
        with self._lock:
            if url in self._entries:       # another thread cloned it meanwhile
                shutil.rmtree(path, ignore_errors=True)
                self._entries[url]["at"] = time.time()
                return self._entries[url]["path"]
            while len(self._entries) >= self.cap:
                self._evict_lru()
            self._entries[url] = {"path": path, "at": time.time()}
            return path

    def _clone(self, url):
        tmp = tempfile.mkdtemp(prefix="lc-hosted-")
        try:
            subprocess.run(["git", "clone", "--quiet", f"--depth={self.depth}", url, tmp],
                           check=True, capture_output=True, text=True, timeout=self.clone_timeout)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return tmp

    def _reap(self, now):
        for url in [u for u, e in self._entries.items() if now - e["at"] > self.ttl]:
            self._delete(url)

    def _evict_lru(self):
        self._delete(min(self._entries, key=lambda u: self._entries[u]["at"]))

    def _delete(self, url):
        e = self._entries.pop(url, None)
        if e:
            shutil.rmtree(e["path"], ignore_errors=True)

    def clear(self):
        with self._lock:
            for url in list(self._entries):
                self._delete(url)


def _load_demos(demo_dir):
    """Read the demo manifest → {id: {title, repo, pr, file}}. Empty if no manifest."""
    if not demo_dir:
        return {}
    manifest = os.path.join(demo_dir, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return {e["id"]: e for e in entries if e.get("id") and e.get("file")}


def make_handler(cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def _demo_review(self, demo_id):
            """Serve a pre-baked review JSON (no key, no cloning) — the safe, instant demo path."""
            entry = cfg.demos.get(demo_id)
            if not entry:
                return self._json(404, {"error": f"unknown demo {demo_id!r}"})
            try:
                with open(os.path.join(cfg.demo_dir, entry["file"]), encoding="utf-8") as f:
                    return self._send(200, f.read(), "application/json")
            except OSError:
                return self._json(500, {"error": "demo file missing"})

        def _repo_for(self, g):
            """The repo this request targets. In --public mode a pasted ?repo= is shallow-cloned into
            the delete-after-use cache; otherwise the fixed repo the server started with."""
            req = g("repo")
            if cfg.public and req and req != cfg.repo:
                if not _repo_allowed(req, cfg.allow):
                    raise PermissionError("only public https github/gitlab/bitbucket repos are allowed")
                return cfg.cache.get(req)
            return cfg.repo

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            g = lambda k, d=None: (q.get(k) or [d])[0]      # noqa: E731
            p = u.path
            try:
                if p in ("/", "/index.html", "/app", "/app.html", ""):
                    return self._send(200, open(os.path.join(WEB, "app.html"), "rb").read(),
                                      "text/html; charset=utf-8")
                if p in ("/map", "/map.html"):
                    return self._send(200, open(os.path.join(WEB, "map.html"), "rb").read(),
                                      "text/html; charset=utf-8")
                if p == "/healthz":
                    return self._json(200, {"ok": True})
                if p == "/api/demos":
                    return self._json(200, {"demos": [
                        {"id": e["id"], "title": e.get("title", e["id"]),
                         "repo": e.get("repo"), "pr": e.get("pr")}
                        for e in cfg.demos.values()]})
                if p == "/api/config":
                    return self._json(200, {"repo": None if cfg.public else cfg.repo,
                                            "invariants": None, "locked": bool(cfg.allow),
                                            "public": cfg.public, "has_demos": bool(cfg.demos),
                                            "preload": {}})

                # --- pre-baked demo short-circuits everything below (no key, no clone) ---
                if p == "/api/review" and g("demo"):
                    return self._demo_review(g("demo"))

                repo = self._repo_for(g)
                if p == "/api/refs":
                    return self._json(200, {"repo": repo,
                                            "branches": [b for b in _git(
                                                repo, "for-each-ref", "--format=%(refname:short)",
                                                "refs/heads").splitlines() if b],
                                            "commits": _commits(repo, int(g("n", "50"))),
                                            "current": gitutil.current_branch(repo),
                                            "default": gitutil.default_branch(repo)})
                if p == "/api/prs":
                    out = _git(repo, "log", "--merges", "-30", "--pretty=%h%x09%s")
                    prs = [{"sha": s, "title": t} for s, _, t in
                           (ln.partition("\t") for ln in out.splitlines()) if s]
                    return self._json(200, {"repo": repo, "prs": prs})
                if p == "/api/review":
                    base = g("base") or gitutil.default_branch(repo)
                    head = g("head") or gitutil.current_branch(repo)
                    if g("commit"):
                        base, head = g("commit") + "^", g("commit")
                    if g("merge"):
                        base, head = g("merge") + "^1", g("merge") + "^2"
                    body = {"repo": cloud._repo_name(repo, repo), "pr": g("pr", "0"),
                            "base_facts": cloud._facts_for(repo, base),
                            "head_facts": cloud._facts_for(repo, head)}
                    code, out = cloud._req("POST", "/api/v1/review", body)
                    return self._json(code or 502, out.get("review", out) if code == 200 else out)
                if p == "/api/map":
                    body = {"repo": cloud._repo_name(repo, repo), "risky": g("risky") == "1",
                            "facts": cloud._facts_for(repo, gitutil.current_branch(repo))}
                    code, out = cloud._req("POST", "/api/v1/map", body)
                    return self._json(code or 502, out.get("map", out) if code == 200 else out)
                if p == "/api/source":
                    rel = g("path")
                    if not rel:
                        return self._json(400, {"error": "no path"})
                    return self._json(200, _source_snippet(repo, g("ref") or "HEAD", rel,
                                                           int(g("line", "1")), int(g("ctx", "6"))))
                if p in ("/api/invariants", "/api/blame"):
                    return self._json(200, {"repo": repo, "candidates": [], "confirmed_ids": [],
                                            "note": "use the CLI: lenscheck invariants"})
                return self._json(404, {"error": "not found"})
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
            except (BrokenPipeError, ConnectionError):
                return
            except Exception as e:
                return self._json(500, {"error": str(e)})

        do_HEAD = do_GET

        def do_POST(self):
            return self._json(501, {"error": "not supported in cloud mode — use `lenscheck invariants`"})

    return H


class _Cfg:
    __slots__ = ("repo", "public", "demo_dir", "demos", "allow", "cache")


def main():
    ap = argparse.ArgumentParser(prog="lenscheck serve")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--demo-dir", help="folder of pre-baked reviews (with manifest.json) to serve")
    ap.add_argument("--public", action="store_true",
                    help="let visitors review a pasted public repo (clone → review → delete)")
    ap.add_argument("--allow", action="append", default=[],
                    help="only allow repo URLs containing this substring (repeatable). "
                         "Also read from LENSCHECK_ALLOWED_REPOS (comma-separated).")
    ap.add_argument("--clone-depth", type=int, default=50)
    a = ap.parse_args(sys.argv[1:])

    cfg = _Cfg()
    cfg.public = a.public
    cfg.demo_dir = a.demo_dir
    cfg.demos = _load_demos(a.demo_dir)
    cfg.allow = list(a.allow) + [s.strip() for s in
                                 os.environ.get("LENSCHECK_ALLOWED_REPOS", "").split(",") if s.strip()]
    cfg.cache = RepoCache(depth=a.clone_depth)

    # A pure demo deploy (only pre-baked JSON) needs no key and no local repo. Anything that computes
    # a review (the fixed repo, or a pasted one in --public mode) still needs the cloud key.
    demo_only = bool(cfg.demos) and not a.public and a.repo == "."
    if not demo_only and not cloud.have_token():
        sys.exit("lenscheck: no API key. Request beta access, then "
                 "`export LENSCHECK_API_KEY=sk_live_...` (self-hosting? also set LENSCHECK_API_URL).")
    cfg.repo = None if a.public else gitutil.ensure_local(a.repo)

    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(cfg))
    url = f"http://{a.host}:{a.port}/"
    mode = "public demo" if a.public else ("demo" if demo_only else f"repo: {cfg.repo}")
    print(f"lenscheck serve → {url}   ({mode}, cloud: {cloud.API_URL})"
          + (f", {len(cfg.demos)} pre-baked demo(s)" if cfg.demos else ""))
    if not a.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cfg.cache.clear()


if __name__ == "__main__":
    main()
