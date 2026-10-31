"""`lenscheck serve` trust boundary: token gate, repo allowlist, file reads confined to the repo, and no
request value reaching a git argv as an option. Real handler on an ephemeral port — not mocked."""
import contextlib
import json
import os
import subprocess
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

import gitutil
import schema_dump
import serve


@contextlib.contextmanager
def _serve(**over):
    cfg = {"repo": "", "invariants": "", "token": "", "allowed": [], "base_path": "", "preload": {},
           "demos": {}, "demo_dir": ""}
    cfg.update(over)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()


def _get(addr, path, headers=None):
    c = HTTPConnection(*addr, timeout=30)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


@pytest.fixture
def layout(tmp_path):
    """allowed/repo/app/x.py inside the allowlist, plus a secret OUTSIDE it and a symlink pointing out."""
    repo = tmp_path / "allowed" / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "x.py").write_text("print('hi')\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n")
    os.symlink(secret, repo / "link.txt")
    (tmp_path / "other").mkdir()
    return {"repo": str(repo), "secret": str(secret), "allowed": str(tmp_path / "allowed"),
            "other": str(tmp_path / "other"), "tmp": tmp_path}


def test_source_snippet_never_reads_outside_the_repo(layout):
    repo = layout["repo"]
    assert serve.source_snippet(repo, "WORKTREE", "app/x.py", 1)["found"] is True
    for rel in (layout["secret"],            # absolute path — os.path.join would drop the repo
                "../../secret.txt",          # traversal
                "link.txt"):                 # symlink resolving outside the repo
        out = serve.source_snippet(repo, "WORKTREE", rel, 1)
        assert out["found"] is False and "TOP-SECRET" not in json.dumps(out), rel


def test_token_gate(layout):
    with _serve(token="s3cret") as addr:
        assert _get(addr, "/api/config")[0] == 401
        assert _get(addr, "/api/config?token=nope")[0] == 401
        assert _get(addr, "/api/config?token=s3cret")[0] == 200
        assert _get(addr, "/api/config", {"X-Lenscheck-Token": "s3cret"})[0] == 200


def test_option_like_refs_are_refused_before_git(layout):
    target = layout["tmp"] / "pwned.txt"
    repo = quote(layout["repo"])
    with _serve(repo=layout["repo"]) as addr:
        for q in (f"/api/source?repo={repo}&path=app/x.py&ref=--output={quote(str(target))}",
                  f"/api/review?repo={repo}&base=--output={quote(str(target))}&head=HEAD",
                  f"/api/crossrepo?repos={repo},--upload-pack=touch%20{quote(str(target))}.git"):
            status, body = _get(addr, q)
            assert status == 400 and "invalid" in body["error"], q
    assert not list(layout["tmp"].glob("pwned*"))              # nothing written via a git option (old code
                                                                # made `pwned.txt:app/x.py`)


def test_allowlist_resolves_traversal_before_matching(layout):
    sneaky = quote(os.path.join(layout["allowed"], "..", "other"))   # contains the allowed substring
    with _serve(allowed=[layout["allowed"]]) as addr:
        assert _get(addr, f"/api/source?repo={sneaky}&path=x.py")[0] == 403
        ok = _get(addr, f"/api/source?repo={quote(layout['repo'])}&path=app/x.py&ref=WORKTREE")
        assert ok[0] == 200 and ok[1]["found"] is True


def test_crossrepo_schema_must_be_json_inside_the_allowlist(layout):
    a, b = os.path.join(layout["allowed"], "a"), os.path.join(layout["allowed"], "b")
    os.makedirs(a)
    os.makedirs(b)
    repos = quote(f"{a},{b}")
    with _serve(allowed=[layout["allowed"]]) as addr:
        for bad in (layout["secret"], os.path.join(layout["allowed"], "s.txt"), "/etc/passwd"):
            assert _get(addr, f"/api/crossrepo?repos={repos}&schema={quote(bad)}")[0] == 403, bad


def test_git_clone_never_parses_the_repo_as_an_option(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "fail")
    monkeypatch.setattr(gitutil, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("LENSCHECK_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    evil = "--upload-pack=touch pwned;.git"                  # passes is_url() via the .git suffix
    with pytest.raises(Exception):
        gitutil.ensure_local(evil)
    argv = seen[0]
    assert argv[argv.index(evil) - 1] == "--"


def test_export_refuses_option_like_refs(tmp_path):
    # `git archive --remote=<url>` would be SSRF, `--output=<file>` a file write — refused before git runs
    for ref in ("--remote=http://169.254.169.254/", "--output=" + str(tmp_path / "x")):
        with pytest.raises(RuntimeError, match="option-like"):
            gitutil.export(str(tmp_path), ref, str(tmp_path))
    assert not (tmp_path / "x").exists()


def test_schema_dump_uniques_from_show_index():
    cols = ["Table", "Non_unique", "Key_name", "Seq_in_index", "Column_name"]
    rows = [("t", 0, "PRIMARY", 1, "id"),                   # primary key — excluded
            ("t", 0, "uniq_lead", 2, "email"),               # out of order on purpose
            ("t", 0, "uniq_lead", 1, "form_id"),
            ("t", 1, "idx_status", 1, "status")]             # non-unique — excluded
    assert schema_dump.uniques_from_show_index(rows, cols) == [("form_id", "email")]


def test_schema_dump_load(tmp_path):
    assert schema_dump.load("") == {}
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"leads": [["form_id", "email"]]}))
    assert schema_dump.load(str(p)) == {"leads": [("form_id", "email")]}
