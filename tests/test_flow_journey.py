"""`lenscheck flow` journeys (several entrypoints in user order traced as one flow) and the catalog
(every entrypoint grouped by app, with summaries and writes)."""
import json

import pytest

import flow
import narrate

APPS = "from django.apps import AppConfig\nclass AppCfg(AppConfig):\n    name = 'app'\n"
MODELS = ("from django.db import models\n"
          "class Campaign(models.Model):\n    name = models.CharField(max_length=9)\n"
          "    class Meta:\n        db_table = 'campaigns'\n"
          "class Targeting(models.Model):\n    campaign_id = models.IntegerField()\n"
          "    class Meta:\n        db_table = 'targeting'\n")
VIEWS = ("from rest_framework.views import APIView\nfrom drf_spectacular.utils import extend_schema\n"
         "from .models import Campaign, Targeting\nfrom .tasks import notify\n"
         "class CampaignView(APIView):\n"
         "    @extend_schema(summary='List campaigns')\n    def get(self, request):\n        return Campaign.objects.all()\n"
         "    @extend_schema(summary='Create campaign')\n    def post(self, request):\n"
         "        c = Campaign.objects.create(name='x')\n        notify.delay(c.id)\n        return c\n"
         "class TargetingView(APIView):\n    def put(self, request, pk):\n"
         "        \"\"\"Set state targeting.\"\"\"\n"
         "        Campaign.objects.get(pk=pk)\n        Targeting.objects.create(campaign_id=pk)\n")


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _repo(root, twin=False):
    _write(root, "app/apps.py", APPS)
    _write(root, "app/models.py", MODELS)
    _write(root, "app/views.py", VIEWS)
    _write(root, "app/tasks.py", "from celery import shared_task\nfrom .models import Campaign\n"
           "@shared_task\ndef notify(cid):\n    Campaign.objects.filter(id=cid).update(name='y')\n")
    _write(root, "app/management/commands/sync_x.py",
           "from django.core.management.base import BaseCommand\nfrom app.models import Targeting\n"
           "class Command(BaseCommand):\n    help = 'Sync targeting rows'\n"
           "    def handle(self, *a, **k):\n        Targeting.objects.all()\n")
    urls = ("from django.urls import path\nfrom . import views\n"
            "urlpatterns = [path('campaigns/', views.CampaignView.as_view()),\n"
            "    path('campaigns/<int:pk>/targeting/', views.TargetingView.as_view())")
    if twin:                                        # two commands: both handlers are `Command`, files differ
        _write(root, "app/management/commands/sync_y.py",
               "from django.core.management.base import BaseCommand\nfrom app.models import Campaign\n"
               "class Command(BaseCommand):\n    def handle(self, *a, **k):\n        Campaign.objects.all()\n")
    _write(root, "app/urls.py", urls + "]\n")
    return root


def _all(steps):
    for s in steps:
        yield s
        yield from _all(s.get("children") or [])


def test_resolve_entry_by_handler_method_command_and_file(tmp_path):
    cands = flow.candidates(str(_repo(tmp_path / "svc", twin=True)))
    assert flow.resolve_entry(cands, "TargetingView")[0]["handler"] == "TargetingView"
    cand, method = flow.resolve_entry(cands, "TargetingView.put")
    assert method == "put" and cand["handler"] == "TargetingView"
    assert flow.resolve_entry(cands, "command:sync_x")[0]["route"] == "command:sync_x"
    assert flow.resolve_entry(cands, "CampaignView.post@views.py")[0]["loc"].startswith("app/views.py")
    assert flow.resolve_entry(cands, "Command@sync_y")[0]["route"] == "command:sync_y"
    with pytest.raises(LookupError, match="add @<file>"):
        flow.resolve_entry(cands, "Command")
    with pytest.raises(LookupError, match="no entrypoint for 'Nope'"):
        flow.resolve_entry(cands, "Nope")


def test_method_filter_traces_only_that_http_method(tmp_path):
    root = str(_repo(tmp_path / "svc"))
    cand, _ = flow.resolve_entry(flow.candidates(root), "CampaignView")
    get = flow.build_flow(root, cand=cand, method="get")
    post = flow.build_flow(root, cand=cand, method="post")
    assert get["entry"]["methods"] == ["GET"]
    assert not [s for s in _all(get["steps"]) if s["kind"] == "write"]
    assert [s for s in _all(post["steps"]) if s["kind"] == "write" and s.get("table") == "campaigns"]
    with pytest.raises(LookupError, match="no `delete` method"):
        flow.build_flow(root, cand=cand, method="delete")


SPEC = {"name": "campaign-setup", "title": "Campaign setup", "source": "test",
        "steps": [{"name": "Create", "entries": ["CampaignView.post"]},
                  {"name": "Target", "entries": ["TargetingView.put"]}]}


def test_journey_is_one_flow_with_a_phase_per_step_in_order(tmp_path):
    f = flow.build_journey(str(_repo(tmp_path / "svc")), SPEC)
    assert f["entry"]["kind"] == "journey" and f["journey"]["title"] == "Campaign setup"
    ids = [s["id"] for s in _all(f["steps"])]
    assert ids[:2] == ["j1", "j1e1"] and any(i.startswith("j2e1.") for i in ids)
    phases, sp = flow.story(f)
    assert [p["kind"] for p in phases] == ["step", "celery", "step"]          # the task runs inside step 1
    assert phases[0]["title"] == "Step 1 · Create" and phases[2]["title"] == "Step 2 · Target"
    camp = next(t for t in flow.table_timeline(f, phases, sp) if t["table"] == "campaigns")
    assert camp["events"][0]["op"] == "W" and camp["events"][0]["step"].startswith("j1e1.")
    assert any(e["op"] == "R" and e["step"].startswith("j2e1.") for e in camp["events"])
    assert "JOURNEY: Campaign setup — 2 steps in user order" in narrate.digest(f, phases, sp, flow.collect_sources(f),
                                                                               flow.table_timeline(f, phases, sp))
    fb = narrate.fallback(f, phases, sp, flow.table_timeline(f, phases, sp))
    assert [s["title"] for s in fb["sections"]] == ["STEP 1 · CREATE", "STEP 2 · TARGET"]


def test_load_journey_yaml_json_and_errors(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text("title: A\nsteps:\n  - name: one\n    entries: [X.post]\n")
    j = flow.load_journey(str(y))
    assert j["name"] == "a" and j["steps"][0]["entries"] == ["X.post"]
    js = tmp_path / "b.json"
    js.write_text(json.dumps(SPEC))
    assert flow.load_journey(str(js))["name"] == "campaign-setup"
    bad = tmp_path / "c.yaml"
    bad.write_text("title: C\nsteps:\n  - name: one\n")
    with pytest.raises(ValueError, match="c.yaml: step 1 needs `entries`"):
        flow.load_journey(str(bad))


def test_catalog_groups_summarises_and_lists_journeys(tmp_path):
    root = str(_repo(tmp_path / "svc"))
    jdir = tmp_path / "journeys"
    jdir.mkdir()
    (jdir / "campaign-setup.json").write_text(json.dumps(SPEC))
    cat = flow.catalog(root, str(jdir))
    rows = {r["handler"] + r["route"]: r for a in cat["apps"] for r in a["entries"]}
    camp = next(r for k, r in rows.items() if k.startswith("CampaignView"))
    assert camp["summary"] == "GET: List campaigns · POST: Create campaign" and "campaigns" in camp["writes"]
    assert next(r for k, r in rows.items() if k.startswith("TargetingView"))["summary"] == "PUT: Set state targeting."
    assert next(r for r in rows.values() if r["route"] == "command:sync_x")["summary"] == "Sync targeting rows"
    assert {a["app"] for a in cat["apps"]} == {"app"}
    assert cat["journeys"][0]["title"] == "Campaign setup" and cat["journeys"][0]["steps"] == 2
    out = tmp_path / "index.html"
    flow.write_catalog_html(cat, str(out))
    html = out.read_text(encoding="utf-8")
    assert "LC_GLOSSARY" in html and 'id="q"' in html and "Campaign setup" in html


def test_html_dir_target_names_the_page_by_slug(tmp_path):
    f = flow.build_journey(str(_repo(tmp_path / "svc")), SPEC)
    out_dir = tmp_path / "atlas"
    assert flow._html_target(str(out_dir) + "/", f) == str(out_dir / "campaign-setup.html")
    assert flow._html_target(str(tmp_path / "x.html"), f) == str(tmp_path / "x.html")


def test_journey_keeps_one_table_hop_per_table_and_takes_follow_from_the_file(tmp_path, monkeypatch):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/apps.py", APPS)
    _write(a, "app/models.py", MODELS)
    _write(a, "app/views.py", "from .models import Campaign\n"
           "def make(request):\n    Campaign.objects.create(name='x')\n"
           "def rename(request):\n    Campaign.objects.filter(id=1).update(name='y')\n")
    _write(a, "app/urls.py", "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('make/', views.make), path('rename/', views.rename)]\n")
    _write(b, "app/apps.py", APPS)
    _write(b, "app/models.py", MODELS)
    for cmd in ("reader_one", "reader_two"):
        _write(b, f"app/management/commands/{cmd}.py",
               "from django.core.management.base import BaseCommand\nfrom app.models import Campaign\n"
               "class Command(BaseCommand):\n    def handle(self, *a, **k):\n        Campaign.objects.all()\n")
    spec = {"name": "j", "title": "J", "steps": [{"name": "make", "entries": ["make"]},
                                                 {"name": "rename", "entries": ["rename"]}]}
    f = flow.build_journey(str(a), spec, crossrepo_paths=[str(b)])
    assert [h["table"] for h in f["hops"] if h["via"] == "table"] == ["campaigns"]
    jf = tmp_path / "j.json"
    jf.write_text(json.dumps(dict(spec, follow=["svcB:command:reader_two"])))
    out = tmp_path / "out.json"
    monkeypatch.setattr("sys.argv", ["flow", str(a), "--journey", str(jf), "--crossrepo", str(b), "--out", str(out)])
    assert flow.main() == 0
    hop = json.loads(out.read_text())["hops"][0]
    assert hop["match"] == "forced" and hop["entry"]["route"] == "command:reader_two"


def test_one_reader_of_two_written_tables_is_expanded_once(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    for root in (a, b):
        _write(root, "app/apps.py", APPS)
        _write(root, "app/models.py", MODELS)
    _write(a, "app/views.py", "from .models import Campaign, Targeting\n"
           "def make(request):\n    Campaign.objects.create(name='x')\n    Targeting.objects.create(campaign_id=1)\n")
    _write(a, "app/urls.py", "from django.urls import path\nfrom . import views\nurlpatterns = [path('make/', views.make)]\n")
    _write(b, "app/management/commands/reader.py",
           "from django.core.management.base import BaseCommand\nfrom app.models import Campaign, Targeting\n"
           "class Command(BaseCommand):\n    def handle(self, *a, **k):\n"
           "        Campaign.objects.all()\n        Targeting.objects.all()\n")
    f = flow.build_flow(str(a), "make/", crossrepo_paths=[str(b)], follow=["svcB:command:reader"])
    tables = [h for h in f["hops"] if h["via"] == "table"]
    assert len(tables) == 1 and tables[0]["table"] == "campaigns" and tables[0]["also_tables"] == ["targeting"]
    phases, _ = flow.story(f)
    assert [p["via"] for p in phases if p["kind"] == "table"] == ["reads campaigns + 1 more"]


def test_a_core_table_with_too_many_readers_gets_no_hop_unless_followed(tmp_path, monkeypatch):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    for root in (a, b):
        _write(root, "app/apps.py", APPS)
        _write(root, "app/models.py", MODELS)
    _write(a, "app/views.py", "from .models import Campaign\ndef make(request):\n    Campaign.objects.create(name='x')\n")
    _write(a, "app/urls.py", "from django.urls import path\nfrom . import views\nurlpatterns = [path('make/', views.make)]\n")
    for cmd in ("r1", "r2", "r3"):
        _write(b, f"app/management/commands/{cmd}.py",
               "from django.core.management.base import BaseCommand\nfrom app.models import Campaign\n"
               "class Command(BaseCommand):\n    def handle(self, *a, **k):\n        Campaign.objects.all()\n")
    monkeypatch.setattr(flow, "MAX_TABLE_READERS", 2)
    assert flow.build_flow(str(a), "make/", crossrepo_paths=[str(b)])["hops"] == []
    hops = flow.build_flow(str(a), "make/", crossrepo_paths=[str(b)], follow=["svcB:command:r2"])["hops"]
    assert len(hops) == 1 and hops[0]["entry"]["route"] == "command:r2"
