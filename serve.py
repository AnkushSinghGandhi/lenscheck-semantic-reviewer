"""serve.py — thin local web UI for Lenscheck.

Serves web/app.html and backs it with the CLOUD (no engine here). `/api/review` and `/api/map`
extract facts locally and call the Lenscheck cloud; `/api/refs` and `/api/source` use local git.
Needs LENSCHECK_API_KEY (same as the CLI). Your source never leaves — only the facts graph.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import cloud
import gitutil

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


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


def make_handler(repo):
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
                if p == "/api/config":
                    return self._json(200, {"repo": repo, "invariants": None, "locked": False,
                                            "preload": {}})
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
            except (BrokenPipeError, ConnectionError):
                return
            except Exception as e:
                return self._json(500, {"error": str(e)})

        do_HEAD = do_GET

        def do_POST(self):
            return self._json(501, {"error": "not supported in cloud mode — use `lenscheck invariants`"})

    return H


def main():
    ap = argparse.ArgumentParser(prog="lenscheck serve")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(sys.argv[1:])
    if not cloud.have_token():
        sys.exit("lenscheck: no API key. Request beta access, then "
                 "`export LENSCHECK_API_KEY=sk_live_...` (self-hosting? also set LENSCHECK_API_URL).")
    repo = gitutil.ensure_local(a.repo)
    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(repo))
    url = f"http://{a.host}:{a.port}/"
    print(f"lenscheck serve → {url}   (repo: {repo}, cloud: {cloud.API_URL})")
    if not a.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
