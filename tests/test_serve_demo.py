"""serve.py hosted-demo pieces: repo allowlisting, the delete-after-use clone cache, the demo
manifest, and end-to-end pre-baked demo serving (no cloud key, no cloning)."""

import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve  # noqa: E402


def test_repo_allowed_rules():
    assert serve._repo_allowed("https://github.com/Netflix/dispatch", [])
    assert serve._repo_allowed("https://gitlab.com/a/b", [])
    assert not serve._repo_allowed("/etc/passwd", [])            # no local paths
    assert not serve._repo_allowed("git@github.com:x/y.git", [])  # no ssh
    assert not serve._repo_allowed("https://evil.example/x", [])  # not a known host
    assert serve._repo_allowed("https://github.com/a/b", ["github.com/a"])
    assert not serve._repo_allowed("https://github.com/a/b", ["github.com/c"])


def test_repo_cache_evicts_and_deletes(monkeypatch):
    made = []

    def fake_clone(self, url):
        d = tempfile.mkdtemp(prefix="lc-test-")
        made.append(d)
        return d

    monkeypatch.setattr(serve.RepoCache, "_clone", fake_clone)
    cache = serve.RepoCache(cap=2, ttl=999)
    p1, p2 = cache.get("u1"), cache.get("u2")
    assert os.path.isdir(p1) and os.path.isdir(p2)

    p3 = cache.get("u3")                       # over cap → LRU (u1) evicted + its dir deleted
    assert not os.path.isdir(p1)
    assert os.path.isdir(p2) and os.path.isdir(p3)
    assert cache.get("u2") == p2               # cache hit — no re-clone

    cache.clear()
    assert not any(os.path.isdir(d) for d in made)   # every clone deleted


def test_load_demos_manifest():
    d = tempfile.mkdtemp()
    json.dump([{"id": "x", "title": "T", "repo": "o/r", "pr": 1, "file": "x.json"}],
              open(os.path.join(d, "manifest.json"), "w"))
    demos = serve._load_demos(d)
    assert demos["x"]["pr"] == 1
    assert serve._load_demos(None) == {}
    assert serve._load_demos("/does/not/exist") == {}


def _demo_server():
    d = tempfile.mkdtemp()
    json.dump([{"id": "dispatch-6205", "title": "Slack canvas", "repo": "Netflix/dispatch",
                "pr": 6205, "file": "dispatch.json"}],
              open(os.path.join(d, "manifest.json"), "w"))
    json.dump({"summary": "3 things that matter", "changes": []},
              open(os.path.join(d, "dispatch.json"), "w"))

    cfg = serve._Cfg()
    cfg.public, cfg.demo_dir, cfg.allow = False, d, []
    cfg.demos = serve._load_demos(d)
    cfg.cache = serve.RepoCache()
    cfg.repo = "."
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(cfg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_end_to_end_demo_serving():
    srv = _demo_server()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        demos = json.load(urllib.request.urlopen(base + "/api/demos"))
        assert demos["demos"][0]["id"] == "dispatch-6205"
        review = json.load(urllib.request.urlopen(base + "/api/review?demo=dispatch-6205"))
        assert review["summary"] == "3 things that matter"
        # unknown demo → 404
        try:
            urllib.request.urlopen(base + "/api/review?demo=nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
