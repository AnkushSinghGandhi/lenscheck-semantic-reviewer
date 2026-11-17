"""`lenscheck flow` accuracy: runtime-chosen models shown as "one of" (flow only — provenance facts stay
unchanged), implicit else after an early return/continue, alternatives in the story, and two false
positives (non-ORM `.save()`, `@mock.patch` taken as a route)."""
import provenance
import flow
from extractor import trace as tr
from extractor.analyzer import analyze_repo, RepoIndex

APPS = "from django.apps import AppConfig\nclass AppCfg(AppConfig):\n    name = 'app'\n"


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _app(root, models, views, routes=("push",)):
    _write(root, "app/apps.py", APPS)
    _write(root, "app/models.py", "from django.db import models\n" + models)
    _write(root, "app/urls.py", "from django.urls import path\nfrom . import views\nurlpatterns = ["
           + ", ".join(f"path('{r}/', views.{r})" for r in routes) + "]\n")
    _write(root, "app/views.py", views)
    return root


def _model(name, table, fields="    v = models.IntegerField(null=True)\n"):
    return f"class {name}(models.Model):\n{fields}    class Meta:\n        db_table = '{table}'\n"


def _all(steps):
    for s in steps:
        yield s
        yield from _all(s.get("children") or [])


def _find(steps, kind, name):
    return next((s for s in _all(steps) if s["kind"] == kind
                 and (s.get("model") == name or s.get("text") == name)), None)


def _jobs_repo(root):
    _app(root, _model("Job", "jobs") + _model("NuJob", "nu_jobs"),
         "from .store import sync\ndef push(request):\n    sync(1)\n")
    _write(root, "app/store.py",
           "from .models import Job, NuJob\n"
           "def _job_model(pt):\n    return Job if pt else NuJob\n"
           "def sync(pt):\n    model = _job_model(pt)\n    model.objects.filter(a=1).update(b=2)\n")
    return root


def test_ternary_helper_model_is_shown_as_one_of_without_touching_provenance(tmp_path):
    root = _jobs_repo(tmp_path / "svc")
    f = flow.build_flow(str(root), "push/")
    w = next(s for s in _all(f["steps"]) if s["kind"] == "write" and s.get("one_of"))
    assert w["one_of"] == ["jobs", "nu_jobs"]
    assert w["text"] == "jobs | nu_jobs (one of)"
    assert not [s for s in _all(f["steps"]) if s["kind"] == "write" and s["text"] == "<instance>"
                and s["loc"] == w["loc"]]
    tables = provenance.build(str(root))["tables"]
    assert any(e["route"] == "push/" for e in tables["<unresolved>"]["written_by"])
    assert "jobs" not in tables and "nu_jobs" not in tables


def test_dict_of_models_read_and_write_are_one_of(tmp_path):
    root = _app(tmp_path / "svc", _model("A", "a_tbl") + _model("B", "b_tbl"),
                "from .models import A, B\n_M = {'a': A, 'b': B}\n"
                "def push(request):\n    k = request.GET['k']\n"
                "    _M[k].objects.filter(x=1).count()\n"
                "    m = _M[k]\n    m.objects.create(y=1)\n")
    f = flow.build_flow(str(root), "push/")
    read = next(s for s in _all(f["steps"]) if s["kind"] == "read" and s.get("one_of"))
    write = next(s for s in _all(f["steps"]) if s["kind"] == "write" and s.get("one_of"))
    assert read["one_of"] == ["a_tbl", "b_tbl"] and read["where"] == {"x": 1}
    assert write["one_of"] == ["a_tbl", "b_tbl"]
    tables = provenance.build(str(root))["tables"]
    assert "a_tbl" not in tables and "b_tbl" not in tables


def test_implicit_else_after_return_and_continue_but_not_raise(tmp_path):
    models = "".join(_model(n, n.lower() + "_tbl") for n in "ABCD")
    root = _app(tmp_path / "svc", models,
                "from .models import A, B, C, D\n"
                "def save_a():\n    A.objects.create(v=1)\n"
                "def save_b():\n    B.objects.create(v=1)\n"
                "def push(request):\n    pt = request.GET['pt']\n"
                "    if pt == 'x':\n        return save_a()\n    save_b()\n"
                "def val(request):\n    bad = request.GET.get('bad')\n"
                "    if bad:\n        raise ValueError()\n    C.objects.create(v=1)\n"
                "def loop(request):\n    rows = request.GET.getlist('r')\n"
                "    for r in rows:\n        if r.skip:\n            continue\n        D.objects.create(v=1)\n",
                routes=("push", "val", "loop"))
    push = flow.build_flow(str(root), "push/")["steps"]
    assert _find(push, "call", "save_b()")["guards"] == ["else (not: pt == 'x')"]
    assert _find(flow.build_flow(str(root), "val/")["steps"], "write", "C")["guards"] == []
    assert _find(flow.build_flow(str(root), "loop/")["steps"], "write", "D")["guards"] == \
        ["for each r in rows", "else (not: r.skip)"]


def test_story_groups_mutually_exclusive_table_hops_as_alternatives():
    steps = [{"id": f"s{i}", "kind": "write", "text": f"t{i}", "table": f"t{i}", "guards": [],
              "loc": f"a.py:{i}", "children": []} for i in (1, 2, 3)]
    whens = [["if pt == 'ns'", "for each r in rows"], ["else (not: pt == 'ns')", "for each r in rows"],
             ["if other"]]
    hops = [{"id": f"h{i}", "via": "table", "repo": "b", "table": f"t{i}", "from_step": f"s{i}",
             "when": whens[i - 1], "match": None, "entry": None, "candidates": [], "steps": []}
            for i in (1, 2, 3)]
    f = {"entry": {"kind": "http", "route": "/x", "handler": "x", "loc": "a.py:1"}, "origin": "a",
         "steps": steps, "hops": hops}
    phases, _ = flow.story(f)
    by_table = {p["via"]: p for p in phases if p["kind"] == "table"}
    t1, t2, t3 = by_table["reads t1"], by_table["reads t2"], by_table["reads t3"]
    assert t1["alt"]["group"] == t2["alt"]["group"]
    assert t1["alt"]["branch"] == "if pt == 'ns'" and t2["alt"]["branch"] == "else (not: pt == 'ns')"
    assert "alt" not in t3


def test_non_orm_save_is_not_a_db_write(tmp_path):
    root = _app(tmp_path / "svc", _model("A", "a_tbl"),
                "from openpyxl import Workbook\nfrom io import BytesIO\n"
                "def push(request):\n    wb = Workbook()\n    buf = BytesIO()\n    wb.save(buf)\n    return buf\n")
    f = flow.build_flow(str(root), "push/", keep_all=True)
    assert not [s for s in _all(f["steps"]) if s["kind"] == "write"]
    tables = provenance.build(str(root))["tables"]
    assert "Workbook" not in tables
    assert not any(e["route"] == "push/" for e in tables.get("<unresolved>", {}).get("written_by", []))


def test_mock_patch_decorator_is_not_an_http_route(tmp_path):
    _write(tmp_path, "app/tests_x.py",
           "from unittest import mock\n@mock.patch('app.models.Item.objects')\ndef test_it(m):\n    pass\n")
    _write(tmp_path, "app/api.py",
           "from fastapi import FastAPI\napp = FastAPI()\n@app.patch('/items')\ndef patch_items():\n    pass\n")
    routes = [e.route for e in analyze_repo(str(tmp_path))]
    assert "/items" in routes
    assert not [r for r in routes if "app.models" in r]


def test_timeline_has_a_maybe_event_under_each_candidate_table(tmp_path):
    f = flow.build_flow(str(_jobs_repo(tmp_path / "svc")), "push/")
    phases, sp = flow.story(f)
    items = {t["table"]: t for t in flow.table_timeline(f, phases, sp)}
    for table in ("jobs", "nu_jobs"):
        assert any(e["op"] == "W" and e.get("maybe") is True for e in items[table]["events"])


def test_exclusive_survives_the_80_char_guard_cut():
    a = [tr._short("if " + "c" * 100, 80)]
    b = [tr._short("else (not: " + "c" * 100 + ")", 80)]
    assert flow._exclusive(a, b) is True


def test_nested_def_early_return_does_not_eat_the_outer_guard(tmp_path):
    # a closure's `if x: return` inside `with atomic():` used to pop the "atomic" guard (IndexError)
    root = _app(tmp_path / "svc", _model("A", "a_tbl") + _model("B", "b_tbl"),
                "from django.db import transaction\nfrom .models import A, B\n"
                "def push(request):\n    with transaction.atomic():\n"
                "        def pick(x):\n            if not x:\n                return None\n            return x\n"
                "        A.objects.create(v=pick(1))\n    B.objects.create(v=2)\n")
    steps = flow.build_flow(str(root), "push/")["steps"]
    assert _find(steps, "write", "A")["guards"] == ["atomic"]
    assert _find(steps, "write", "B")["guards"] == []
