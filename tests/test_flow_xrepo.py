"""`lenscheck flow --crossrepo`: table / http / celery hops into another repo."""
import flow
from extractor.analyzer import RepoIndex


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


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


def _push_repo(root):
    _write(root, "app/models.py",
           "from django.db import models\n"
           "class Item(models.Model):\n"
           "    job_id = models.IntegerField()\n"
           "    flag = models.IntegerField(db_column='process')\n"
           "    class Meta:\n        db_table = 'items'\n")
    _write(root, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('push/', views.push)]\n")
    _write(root, "app/views.py",
           "from .models import Item\n"
           "def push(request):\n"
           "    Item.objects.bulk_create([Item(job_id=j, flag=0) for j in [1]])\n")
    return root


def _reader_command(root, name, body):
    _write(root, f"app/management/commands/{name}.py",
           "from django.core.management.base import BaseCommand\n"
           "from app.models import Item\n"
           "class Command(BaseCommand):\n"
           "    def handle(self, *a, **k):\n"
           f"        {body}\n")


def _readers_repo(root):
    _write(root, "app/models.py",
           "from django.db import models\n"
           "class Item(models.Model):\n"
           "    job_id = models.IntegerField(null=True)\n"
           "    process = models.IntegerField()\n"
           "    class Meta:\n        db_table = 'items'\n")
    _reader_command(root, "seed", "Item.objects.create(process=0)")
    _reader_command(root, "fresh_cmd", "Item.objects.exclude(job_id__isnull=True).first()")
    _reader_command(root, "legacy_cmd", "Item.objects.filter(process=0).first()")
    _reader_command(root, "done_cmd", "Item.objects.filter(process=1).first()")
    return root


def test_table_hop_ranks_the_distinctive_reader(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _push_repo(a)
    _readers_repo(b)

    f = flow.build_flow(str(a), "/push", crossrepo_paths=[str(b)])
    assert len(f["hops"]) == 1
    hop = f["hops"][0]
    assert (hop["via"], hop["table"], hop["repo"], hop["match"]) == \
        ("table", "items", "svcB", "ranked")
    assert hop["entry"]["route"] == "command:fresh_cmd"
    cands = hop["candidates"]
    assert cands[0]["route"] == "command:fresh_cmd"
    assert any("job_id" in r for r in cands[0]["reasons"])
    assert cands[1]["route"] == "command:legacy_cmd"
    assert cands[-1]["route"] == "command:done_cmd" and cands[-1]["contradicted"] is True
    read = _find(hop["steps"], "read", "Item")
    assert read and read["where_not"] == {"job_id__isnull": True}


def test_table_hop_ambiguous_until_follow_forces_one(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _push_repo(a)
    _write(b, "app/models.py",
           "from django.db import models\n"
           "class Item(models.Model):\n"
           "    job_id = models.IntegerField(null=True)\n"
           "    process = models.IntegerField()\n"
           "    class Meta:\n        db_table = 'items'\n")
    _reader_command(b, "legacy_cmd", "Item.objects.filter(process=0).first()")
    _reader_command(b, "other_cmd", "Item.objects.filter(process=0).last()")

    hop = flow.build_flow(str(a), "/push", crossrepo_paths=[str(b)])["hops"][0]
    assert hop["match"] is None and "steps" not in hop

    hop = flow.build_flow(str(a), "/push", crossrepo_paths=[str(b)],
                          follow=["svcB:command:legacy_cmd"])["hops"][0]
    assert hop["match"] == "forced"
    assert hop["entry"]["route"] == "command:legacy_cmd"


def test_http_hop_expands_the_serving_route(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('score/', views.score_it)]\n")
    _write(a, "app/views.py",
           "import requests\nfrom django.conf import settings\n"
           "def score_it(request):\n"
           "    requests.post(f'{settings.B_HOST}/api/v1/score/', json={})\n")
    _write(b, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('api/v1/score/', views.score)]\n")
    _write(b, "app/views.py",
           "from .models import Score\n"
           "def score(request):\n"
           "    Score.objects.create(v=1)\n")
    _write(b, "app/models.py",
           "from django.db import models\nclass Score(models.Model):\n    v = models.IntegerField()\n")

    f = flow.build_flow(str(a), "/score/", crossrepo_paths=[str(b)])
    hop = next(h for h in f["hops"] if h["via"] == "http")
    assert (hop["repo"], hop["match"]) == ("svcB", "exact")
    external = _find(f["steps"], "external", "requests.post", startswith=True)
    assert hop["from_step"] == external["id"]
    assert _find(hop["steps"], "write", "Score") is not None


def test_celery_hop_expands_the_task(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('pay/', views.pay)]\n")
    _write(a, "app/views.py",
           "from celery import current_app\n"
           "def pay(request):\n"
           "    current_app.send_task('b.score', args=[1])\n")
    _write(b, "app/tasks.py",
           "from celery import shared_task\nfrom .models import Score\n"
           "@shared_task(name='b.score')\n"
           "def score(x):\n    Score.objects.create(v=x)\n")
    _write(b, "app/models.py",
           "from django.db import models\nclass Score(models.Model):\n    v = models.IntegerField()\n")

    f = flow.build_flow(str(a), "/pay/", crossrepo_paths=[str(b)])
    hop = next(h for h in f["hops"] if h["via"] == "celery")
    assert hop["match"] == "exact"
    assert _find(hop["steps"], "write", "Score") is not None


def test_hop_steps_are_namespaced(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('score/', views.score_it)]\n")
    _write(a, "app/views.py",
           "import requests\nfrom django.conf import settings\n"
           "def score_it(request):\n"
           "    requests.post(f'{settings.B_HOST}/api/v1/score/', json={})\n")
    _write(b, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('api/v1/score/', views.score)]\n")
    _write(b, "app/views.py",
           "from .models import Score\n"
           "def score(request):\n    helper()\n    helper()\n"
           "def helper():\n    Score.objects.create(v=1)\n")
    _write(b, "app/models.py",
           "from django.db import models\nclass Score(models.Model):\n    v = models.IntegerField()\n")

    hop = flow.build_flow(str(a), "/score/", crossrepo_paths=[str(b)])["hops"][0]
    seen = {"ids": 0, "refs": 0}

    def walk(steps):
        for s in steps:
            seen["ids"] += 1
            assert s["id"].startswith("h1.")
            if "ref" in s:
                seen["refs"] += 1
                assert s["ref"].startswith("h1.")
            walk(s.get("children") or [])
    walk(hop["steps"])
    assert seen["ids"] >= 3 and seen["refs"] == 1       # call, its write, the second call as a ref


def test_column_map_resolves_db_column_and_fk(tmp_path):
    b = tmp_path / "svcB"
    _write(b, "app/models.py",
           "from django.db import models\n"
           "class Item(models.Model):\n"
           "    job_id = models.IntegerField()\n"
           "    process = models.IntegerField()\n"
           "    class Meta:\n        db_table = 'items'\n"
           "class Other(models.Model):\n"
           "    owner = models.ForeignKey(User, on_delete=models.CASCADE)\n"
           "    flag = models.IntegerField(db_column='process')\n")
    index = RepoIndex(str(b))
    assert index.column_map("Item") == {"job_id": "job_id", "process": "process"}
    assert index.column_map("Other") == {"owner": "owner_id", "flag": "process"}


def test_tracer_reads_exclude_and_bulk_create_kwargs(tmp_path):
    a = tmp_path / "svcA"
    _write(a, "app.py",
           "from fastapi import FastAPI\n"
           "app = FastAPI()\n"
           "class M:\n    objects = None\n"
           "@app.get('/thing')\n"
           "def handler(v):\n"
           "    M.objects.exclude(a=1).first()\n"
           "    M.objects.bulk_create([M(x=1, y=v)])\n")
    steps = flow.build_flow(str(a), "/thing")["steps"]
    read = _find(steps, "read", "M")
    assert read["where_not"] == {"a": 1} and "a" not in (read.get("where") or {})
    write = _find(steps, "write", "M")
    assert write["set"] == {"x": 1, "y": "?"}


def test_no_crossrepo_means_no_hops(tmp_path):
    a = tmp_path / "svcA"
    _push_repo(a)
    assert flow.build_flow(str(a), "/push")["hops"] == []


def test_table_hop_starts_at_the_first_write_in_source_order(tmp_path):
    a, b = _push_repo(tmp_path / "svcA"), _readers_repo(tmp_path / "svcB")
    _write(a, "app/views.py",
           "from .models import Item\n"
           "def push(request):\n"
           "    stage()\n"
           "    Item.objects.create(job_id=2, flag=0)\n"
           "def stage():\n"
           "    Item.objects.create(job_id=1, flag=0)\n")
    f = flow.build_flow(str(a), "/push", crossrepo_paths=[str(b)])
    nested = _find(f["steps"], "call", "stage()")["children"][0]
    assert nested["kind"] == "write"
    assert f["hops"][0]["from_step"] == nested["id"]


def test_an_expanded_hop_with_no_steps_says_so_instead_of_asking_to_pick():
    f = {"entry": {"kind": "http", "route": "/a", "handler": "h", "loc": "x:1"},
         "steps": [{"id": "s1", "kind": "external", "text": "requests.get -> /b", "guards": [],
                    "loc": "x:2", "children": []}],
         "hops": [{"id": "h1", "via": "http", "from_step": "s1", "repo": "B", "match": "exact",
                   "entry": {"route": "b/"}, "steps": []},
                  {"id": "h2", "via": "table", "table": "t", "from_step": "s1", "when": [], "repo": "B",
                   "match": "ranked", "entry": {"route": "command:x"}, "steps": [],
                   "candidates": [{"route": "command:x", "handler": "C", "reasons": [], "expanded": True}]}]}
    out = flow.render_ascii(f)
    assert "═══ http → B b/ (exact) ═══" in out
    assert out.count("(no reads, writes, calls or exits found there)") == 2 and "pick one" not in out
