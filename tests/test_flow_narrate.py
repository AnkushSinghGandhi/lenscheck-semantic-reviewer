"""`lenscheck flow --explain`: the digest Claude reads, grounding (refs must exist), the facts-only fallback,
and the `claude -p` provider (mocked — no real LLM call in tests)."""
import json
import subprocess

import pytest

import flow
import narrate

APPS = "from django.apps import AppConfig\nclass AppCfg(AppConfig):\n    name = 'app'\n"


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _repo(root):
    _write(root, "app/apps.py", APPS)
    _write(root, "app/models.py", "from django.db import models\n"
           "class A(models.Model):\n    v = models.IntegerField()\n    class Meta:\n        db_table = 'a_tbl'\n")
    _write(root, "app/urls.py", "from django.urls import path\nfrom . import views\nurlpatterns = [path('push/', views.push)]\n")
    _write(root, "app/views.py",
           "from .models import A\n"
           "def helper():\n    \"\"\"Save the thing for later.\"\"\"\n    A.objects.create(v=1)\n"
           "def push(request):\n    if request.GET.get('x'):\n        helper()\n        helper()\n")
    return root


def _parts(root):
    f = flow.build_flow(str(root), "push/")
    phases, sp = flow.story(f)
    return f, phases, sp, flow.collect_sources(f), flow.table_timeline(f, phases, sp)


def _ids(f):
    return {s["id"] for s in narrate._walk(f["steps"])}


def test_digest_carries_ids_guards_docs_and_code_and_is_deterministic(tmp_path):
    f, phases, sp, src, tl = _parts(_repo(tmp_path / "svc"))
    d = narrate.digest(f, phases, sp, src, tl)
    assert d == narrate.digest(f, phases, sp, src, tl)
    assert "? if request.GET.get('x')" in d
    assert '"Save the thing for later."' in d
    assert any(ln.strip().startswith("code:") and "A.objects.create(v=1)" in ln for ln in d.splitlines())
    assert "a_tbl" in d
    assert all(f"{i} [" in d for i in _ids(f))


def test_validate_drops_unknown_refs_files_tables_and_bad_icons(tmp_path):
    f, phases, sp, src, tl = _parts(_repo(tmp_path / "svc"))
    real = sorted(_ids(f))[0]
    narr = {"title": "t", "one_liner": [], "sections": [{"id": "S1", "title": "x", "subtitle": "", "repo": "svc",
            "phases": ["p1"], "stages": [{"n": 1, "title": "s", "icon": "rocket", "refs": [real, "s999"],
                                          "items": [{"text": "i", "icon": "database", "refs": ["nope"]}],
                                          "branches": [], "next_label": ""}]}],
            "modules": [{"name": "m", "file": "ghost.py", "purpose": "", "refs": [real]}],
            "notes": [{"text": "n", "refs": []}],
            "key_files": [{"file": "app/views.py", "purpose": ""}, {"file": "ghost.py", "purpose": ""}],
            "tables": [{"table": "a_tbl", "story": "", "refs": [real]}, {"table": "ghost", "story": "", "refs": [real]}]}
    out, stats = narrate.validate(narr, f, tl)
    stage = out["sections"][0]["stages"][0]
    assert stage["refs"] == [real] and stage["icon"] == "gear" and not stage.get("unverified")
    assert stage["items"][0]["unverified"] is True and stage["items"][0]["refs"] == []
    assert out["modules"] == [] and out["notes"][0]["unverified"] is True
    assert [k["file"] for k in out["key_files"]] == ["app/views.py"]
    assert [t["table"] for t in out["tables"]] == ["a_tbl"]
    assert stats["refs_dropped"] == 2 and narr["sections"][0]["stages"][0]["icon"] == "rocket"   # input untouched


def test_fallback_is_a_grounded_narrative_from_facts(tmp_path):
    f, phases, sp, src, tl = _parts(_repo(tmp_path / "svc"))
    fb = narrate.fallback(f, phases, sp, tl)
    assert fb["sections"] and fb["sections"][0]["stages"][0]["n"] == 1
    assert any("a_tbl" in it["text"] for it in fb["sections"][0]["stages"][0]["items"])
    ids = _ids(f)
    for st in fb["sections"][0]["stages"]:
        assert set(st["refs"]) <= ids and all(set(it["refs"]) <= ids for it in st["items"])
    assert fb["tables"][0]["table"] == "a_tbl"


class _Run:
    def __init__(self, stdout, code=0):
        self.calls, self.stdout, self.code = [], stdout, code

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.code, stdout=self.stdout, stderr="boom")


def _answer(f):
    real = sorted(_ids(f))[0]
    return {"title": "Push", "one_liner": ["a", "b"], "sections": [{"id": "S1", "title": "SVC", "subtitle": "push/",
            "repo": "svc", "phases": ["p1"], "stages": [{"n": 1, "title": "Save", "icon": "database", "refs": [real],
                                                       "items": [], "branches": [], "next_label": ""}]}],
            "modules": [], "notes": [], "key_files": [], "tables": []}


@pytest.mark.parametrize("wrap", ["structured_output", "result"])
def test_explain_parses_the_cli_answer_and_caches_it(tmp_path, monkeypatch, wrap):
    f, phases, sp, src, tl = _parts(_repo(tmp_path / "svc"))
    ans = _answer(f)
    out = {"type": "result", wrap: ans if wrap == "structured_output" else json.dumps(ans)}
    run = _Run(json.dumps(out))
    monkeypatch.setattr(narrate.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(narrate.subprocess, "run", run)
    narr, meta = narrate.explain(f, phases, sp, src, tl)
    assert narr["title"] == "Push" and meta["source"] == "claude:claude-opus-5-5" and meta["cached"] is False
    cmd, kw = run.calls[0]
    assert cmd[:2] == ["/usr/bin/claude", "-p"] and "--json-schema" in cmd and "--disallowedTools" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert "TRACE:" in kw["input"]
    _, meta2 = narrate.explain(f, phases, sp, src, tl)
    assert meta2["cached"] is True and len(run.calls) == 1
    narrate.explain(f, phases, sp, src, tl, refresh=True)
    assert len(run.calls) == 2


@pytest.mark.parametrize("which,stdout,code", [(None, "", 0), ("/x/claude", "", 1), ("/x/claude", "not json", 0)])
def test_explain_failures_fall_back_to_facts_without_failing(tmp_path, monkeypatch, capsys, which, stdout, code):
    root = _repo(tmp_path / "svc")
    f = flow.build_flow(str(root), "push/")
    monkeypatch.setattr(narrate.shutil, "which", lambda _: which)
    monkeypatch.setattr(narrate.subprocess, "run", _Run(stdout, code))
    out = tmp_path / "f.html"
    meta = flow.write_html(f, str(out), explain=True)
    assert meta["source"] == "facts"
    assert "--explain failed" in capsys.readouterr().err
    html = out.read_text(encoding="utf-8")
    data = json.loads(html.split("window.FLOW_DATA = ", 1)[1].split(";</script>", 1)[0])
    assert data["narrative"]["sections"] and data["narrative_meta"]["source"] == "facts"


def test_a_journey_step_id_alone_is_not_evidence():
    f = {"steps": [{"id": "j1", "kind": "call", "loc": "", "children": [
        {"id": "j1e1", "kind": "call", "loc": "app/views.py:3", "children": [
            {"id": "j1e1.s1", "kind": "write", "loc": "app/views.py:4", "children": []}]}]}], "hops": []}
    narr = {"sections": [{"stages": [{"n": 1, "title": "s", "icon": "gear", "refs": ["j1"], "branches": [],
                                      "items": [{"text": "a", "icon": "gear", "refs": ["j1"]},
                                                {"text": "b", "icon": "gear", "refs": ["j1", "j1e1.s1"]},
                                                {"text": "c", "icon": "gear", "refs": ["j1e1"]}]}]}]}
    out, stats = narrate.validate(narr, f, [])
    st = out["sections"][0]["stages"][0]
    assert st["unverified"] is True and st["refs"] == ["j1"]          # kept for navigation, not evidence
    assert [bool(i.get("unverified")) for i in st["items"]] == [True, False, False]
