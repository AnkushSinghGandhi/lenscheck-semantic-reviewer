"""`lenscheck flow`: ordered steps, guards, literal column values, call dedupe, celery boundaries and
entrypoint resolution. Each fixture is a tiny FastAPI/Django repo, like `test_follow.py`."""
import os
import tempfile

import pytest

import flow
from extractor.analyzer import RepoIndex, analyze_handler, to_dict


def _write(root, rel, src):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(src)


def _repo(src, rel="app.py"):
    root = tempfile.mkdtemp()
    _write(root, rel, src)
    return root


def _flat(steps):
    out = []
    for s in steps:
        out.append((s["kind"], s.get("model") or s.get("text")))
        out += _flat(s.get("children") or [])
    return out


def _find(steps, kind, name, startswith=False):
    for s in steps:
        text = s.get("model") or s.get("text") or ""
        if s["kind"] == kind and (text.startswith(name) if startswith else text == name):
            return s
        found = _find(s.get("children") or [], kind, name, startswith)
        if found:
            return found
    return None


def test_nested_helper_call_appears_between_the_handlers_own_facts():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelA:
    objects = None
class ModelB:
    objects = None
class ModelC:
    objects = None
@app.get('/thing')
def handler():
    ModelA.objects.all()
    helper()
    ModelC.objects.create(x=1)
def helper():
    ModelB.objects.create(y=2)
""")
    steps = flow.build_flow(root, "/thing")["steps"]
    assert _flat(steps) == [("read", "ModelA"), ("call", "helper()"),
                            ("write", "ModelB"), ("write", "ModelC")]


def test_guards_on_if_else_loops_and_early_exits():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelA:
    objects = None
class ModelB:
    objects = None
class ModelC:
    objects = None
class ModelD:
    objects = None
@app.get('/thing')
def handler(x):
    if x:
        raise ValueError("bad")
    for i in ModelA.objects.all():
        ModelB.objects.create(a=i)
    if x > 1:
        ModelC.objects.create()
    else:
        ModelD.objects.create()
""")
    steps = flow.build_flow(root, "/thing")["steps"]
    exit_step = _find(steps, "exit", "raise ValueError", startswith=True)
    assert exit_step and exit_step["text"].startswith("raise ValueError")
    assert exit_step["guards"] == ["if x"]
    assert _find(steps, "read", "ModelA")["guards"] == []
    assert _find(steps, "write", "ModelB")["guards"] == ["for each i in ModelA.objects.all()"]
    assert _find(steps, "write", "ModelC")["guards"] == ["if x > 1"]
    assert _find(steps, "write", "ModelD")["guards"] == ["else (not: x > 1)"]


def test_literal_where_and_set_values():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class Q:
    objects = None
@app.get('/q')
def handler(x):
    Q.objects.filter(process=0).update(process=1)
    Q.objects.create(flag=x)
""")
    steps = flow.build_flow(root, "/q")["steps"]
    writes = [s for s in steps if s["kind"] == "write"]
    assert writes[0]["where"] == {"process": 0} and writes[0]["set"] == {"process": 1}
    assert writes[1]["set"] == {"flag": "?"}


def test_repeated_helper_is_a_ref_to_the_first_call():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelB:
    objects = None
@app.get('/h')
def handler():
    helper()
    helper()
def helper():
    ModelB.objects.create(y=2)
""")
    steps = flow.build_flow(root, "/h")["steps"]
    calls = [s for s in steps if s["kind"] == "call"]
    assert len(calls) == 2
    assert calls[1]["ref"] == calls[0]["id"] and calls[1]["children"] == []


def test_factless_helper_is_pruned_unless_keep_all():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelB:
    objects = None
@app.get('/h')
def handler():
    noop()
    helper()
def noop():
    x = 1
def helper():
    ModelB.objects.create(y=2)
""")
    assert _find(flow.build_flow(root, "/h")["steps"], "call", "noop()") is None
    assert _find(flow.build_flow(root, "/h", keep_all=True)["steps"], "call", "noop()") is not None


def test_same_repo_celery_task_is_a_guarded_boundary():
    root = _repo("""from fastapi import FastAPI
from celery import shared_task
app = FastAPI()
class Job:
    objects = None
@shared_task
def do_job(x):
    Job.objects.create(status=0)
@app.get('/h')
def handler():
    do_job.delay(1)
""")
    steps = flow.build_flow(root, "/h")["steps"]
    async_step = next(s for s in steps if s["kind"] == "async")
    assert async_step["target"] == "do_job" and async_step["boundary"] == "celery"
    child = _find(async_step["children"], "write", "Job")
    assert child["set"] == {"status": 0}


def test_thin_command_reads_through_its_base_before_the_external_call():
    root = tempfile.mkdtemp()
    _write(root, "app/models.py", "class Lead:\n    objects = None\n")
    _write(root, "app/engine.py", """from django.core.management.base import BaseCommand
from app.models import Lead
import requests
class DeliverBaseCommand(BaseCommand):
    def handle(self, *a, **k):
        Lead.objects.filter(process=0).first()
        self.push()
    def push(self):
        requests.post("https://x.example/api")
""")
    _write(root, "app/management/commands/deliver_fresh.py",
           "from app.engine import DeliverBaseCommand\nclass Command(DeliverBaseCommand):\n    spec = 1\n")
    steps = flow.build_flow(root, "command:deliver_fresh")["steps"]
    read = _find(steps, "read", "Lead")
    assert read["where"] == {"process": 0}
    call = _find(steps, "call", "self.push()")
    assert call is not None and _find(call["children"], "external", "requests.post -> https://x.example/api")
    assert _flat(steps).index(("read", "Lead")) < _flat(steps).index(("call", "self.push()"))


def test_query_resolution_exact_keyword_ambiguity_and_pick():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
@app.get('/lead/upload')
def lead_upload():
    return 1
@app.get('/user/upload')
def user_upload():
    return 1
""")
    cands = flow.candidates(root)
    assert flow.resolve(cands, "lead upload")["handler"] == "lead_upload"
    with pytest.raises(flow.Ambiguous) as err:
        flow.resolve(cands, "upload")
    assert len(err.value.ranked) >= 2
    assert flow.resolve(cands, "user_upload")["handler"] == "user_upload"
    assert flow.resolve(cands, "upload", pick=2) == err.value.ranked[1]


def test_to_dict_never_leaks_the_walk_state():
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
@app.get('/x')
def handler():
    pass
""")
    ep = analyze_handler("handler", RepoIndex(root))
    assert hasattr(ep, "lc_walk")
    assert "lc_walk" not in to_dict(ep)


def test_call_cut_by_the_depth_cap_stays_visible(monkeypatch):
    import extractor.trace as tr
    monkeypatch.setattr(tr, "DEPTH_CEILING", 1)
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelB:
    objects = None
@app.get('/deep')
def handler():
    a()
def a():
    b()
def b():
    ModelB.objects.create(y=2)
""")
    out = flow.build_flow(root, "/deep")
    cut = _find(out["steps"], "call", "b()")
    assert cut is not None and cut["truncated"] is True and cut["children"] == []
    assert out["stats"]["truncated"] is True
    assert "✂" in flow.render_ascii(out)


def test_call_cut_deep_first_is_still_expanded_when_reached_shallow(monkeypatch):
    import extractor.trace as tr
    monkeypatch.setattr(tr, "DEPTH_CEILING", 2)
    root = _repo("""from fastapi import FastAPI
app = FastAPI()
class ModelB:
    objects = None
@app.get('/both')
def handler():
    a()
    helper()
def a():
    b()
def b():
    helper()
def helper():
    ModelB.objects.create(y=2)
""")
    steps = flow.build_flow(root, "/both")["steps"]
    assert _find(steps, "call", "helper()")["truncated"] is True          # the deep one, under a → b
    top = [s for s in steps if s["text"] == "helper()"]
    assert len(top) == 1 and "ref" not in top[0]
    assert _find(top[0]["children"], "write", "ModelB") is not None


def test_db_steps_show_the_table_name_not_the_model():
    root = tempfile.mkdtemp()
    _write(root, "shop/models.py",
           "from django.db import models\n"
           "class FreshLead(models.Model):\n"
           "    class Meta:\n        db_table = 'adm_campaign_lead_temp'\n"
           "class Order(models.Model):\n    pass\n")
    _write(root, "shop/apps.py", "from django.apps import AppConfig\nclass ShopConfig(AppConfig):\n    name = 'shop'\n")
    _write(root, "shop/urls.py",
           "from django.urls import path\nfrom . import views\nurlpatterns = [path('push/', views.push)]\n")
    _write(root, "shop/views.py",
           "from .models import FreshLead, Order\n"
           "def push(request):\n"
           "    FreshLead.objects.create(process=0)\n"
           "    Order.objects.filter(id=1).first()\n")
    out = flow.build_flow(root, "push/")
    lead, order = _find(out["steps"], "write", "FreshLead"), _find(out["steps"], "read", "Order")
    assert (lead["text"], lead["table"]) == ("adm_campaign_lead_temp", "adm_campaign_lead_temp")
    assert (order["text"], order["table"]) == ("shop_order", "shop_order")     # Django's default name
    text = flow.render_ascii(out)
    assert "W adm_campaign_lead_temp" in text and "R shop_order" in text and "FreshLead" not in text
