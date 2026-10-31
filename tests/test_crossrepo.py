"""Cross-repo: background-job write attribution (#1), HTTP-call coupling (#5),
provenance caching (#8), and PR blast-radius (#10)."""
import subprocess

import crossrepo


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def test_background_job_writes_make_a_shared_table_multi_writer(tmp_path):
    # Two services whose ONLY write to the shared table is from a non-route path: a celery task in
    # one, a management command in the other. Route-only discovery would miss both → table absent.
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/models.py",
           "from django.db import models\n"
           "class FreshLead(models.Model):\n"
           "    class Meta:\n        db_table = 'campaign_lead_staging'\n")
    _write(a, "app/tasks.py",
           "from celery import shared_task\nfrom .models import FreshLead\n"
           "@shared_task\ndef process_upload(rows):\n"
           "    FreshLead.objects.bulk_create([FreshLead() for _ in rows])\n")
    _write(b, "app/models.py",
           "from django.db import models\n"
           "class LeadTemp(models.Model):\n"
           "    class Meta:\n        db_table = 'campaign_lead_staging'\n")
    _write(b, "app/management/commands/run_delivery.py",
           "from django.core.management.base import BaseCommand\nfrom app.models import LeadTemp\n"
           "class Command(BaseCommand):\n    def handle(self, *a, **k):\n        LeadTemp.objects.create()\n")

    data = crossrepo.build([str(a), str(b)])
    shared = {s["table"]: s for s in data["shared_tables"]}
    assert "campaign_lead_staging" in shared, "job writes must surface the shared table"
    t = shared["campaign_lead_staging"]
    assert t["kind"] == "multi_writer" and set(t["writers"]) == {"svcA", "svcB"}
    routes = {e["route"] for r in t["by_repo"].values() for e in r["written_by"]}
    assert any(r.startswith("task:") for r in routes) and any(r.startswith("command:") for r in routes)


def test_cross_repo_api_call_is_linked_to_the_serving_route(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('sync/', views.sync_lead)]\n")
    _write(a, "app/views.py",
           "import requests\nfrom django.conf import settings\n"
           "def sync_lead(request):\n"
           "    requests.post(f'{settings.LEGACY_HOST}/api/leads/', json={})\n")
    _write(b, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('leads/', views.lead_create)]\n")
    _write(b, "app/views.py", "def lead_create(request):\n    return None\n")

    links = crossrepo.build([str(a), str(b)])["api_links"]
    assert len(links) == 1
    l = links[0]
    assert (l["from_repo"], l["from"]["handler"], l["call"], l["to_repo"], l["to"]["handler"]) == \
        ("svcA", "sync_lead", "/api/leads/", "svcB", "lead_create")


def test_route_match_is_suffix_and_needs_a_literal():
    assert crossrepo._route_match(["api", "v1", "leads"], ["leads"])          # tail literal
    assert crossrepo._route_match(["api", "leads", "5"], ["leads", "*"])      # param wildcard
    assert not crossrepo._route_match(["api", "leads"], ["orders"])           # no literal match
    assert not crossrepo._route_match(["leads"], ["*"])                       # all-wildcard → no link


def test_classmethod_cls_call_chain_is_followed_to_the_write(tmp_path):
    # task → Helper.save_uploaded() → cls._save_fresh() → bulk_create. The `cls.method()` hop was
    # invisible before (only `self.method()` was followed), hiding the write two levels down — the
    # real campaign_lead_staging miss.
    r = tmp_path / "svc"
    _write(r, "app/models.py",
           "from django.db import models\nclass FreshLead(models.Model):\n"
           "    class Meta:\n        db_table = 't_fresh'\n")
    _write(r, "app/helper.py",
           "from .models import FreshLead\nclass Helper:\n"
           "    @classmethod\n    def save_uploaded(cls, rows):\n        return cls._save_fresh(rows)\n"
           "    @classmethod\n    def _save_fresh(cls, rows):\n        FreshLead.objects.bulk_create([FreshLead()])\n")
    _write(r, "app/tasks.py",
           "from celery import shared_task\nfrom .helper import Helper\n"
           "@shared_task\ndef process_upload(rows):\n    Helper.save_uploaded(rows)\n")
    t = crossrepo.provenance.build(str(r))["tables"].get("t_fresh")
    assert t and any(e["route"] == "task:process_upload" for e in t["written_by"])


def test_blast_keeps_only_tables_and_links_a_changed_file_touches():
    data = {
        "repos": ["cms", "legacy"],
        "shared_tables": [
            {"table": "leads", "kind": "multi_writer", "writers": ["cms", "legacy"], "by_repo": {
                "cms": {"written_by": [{"route": "upload/", "handler": "u", "loc": "cms/views.py:10"}], "read_by": []},
                "legacy": {"written_by": [{"route": "command:run", "handler": "C", "loc": "legacy/cmd.py:5"}], "read_by": []}}},
            {"table": "other", "kind": "multi_writer", "writers": ["cms", "legacy"], "by_repo": {
                "cms": {"written_by": [{"route": "x/", "handler": "x", "loc": "cms/elsewhere.py:1"}], "read_by": []},
                "legacy": {"written_by": [{"route": "y/", "handler": "y", "loc": "legacy/z.py:1"}], "read_by": []}}},
        ],
        "api_links": [
            {"from_repo": "cms", "call": "/api/x", "from": {"loc": "cms/views.py:10", "route": "upload/", "handler": "u"},
             "to_repo": "legacy", "to": {"loc": "legacy/urls.py:2", "route": "x/", "handler": "xv"}},
        ],
    }
    tables, links = crossrepo._blast(data, "cms", {"cms/views.py"})
    assert [t["table"] for t in tables] == ["leads"]           # 'other' dropped (its cms write isn't in the diff)
    assert tables[0]["blast"]["changed_write"] is True         # a WRITE changed → bigger blast
    assert len(links) == 1                                     # link whose from.loc is in the changed file


def test_provenance_cache_hits_on_second_call_for_a_clean_repo(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "models.py").write_text(
        "from django.db import models\nclass M(models.Model):\n    class Meta:\n        db_table = 't'\n")

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "x")

    calls, real = [], crossrepo.provenance.build
    monkeypatch.setattr(crossrepo.provenance, "build",
                        lambda p, with_calls=False: (calls.append(1), real(p, with_calls=with_calls))[1])
    crossrepo._provenance(str(repo), True, True)      # miss → extracts
    crossrepo._provenance(str(repo), True, True)      # clean HEAD unchanged → cache hit
    assert len(calls) == 1

    git("rm", "-q", "app/models.py")                  # dirty tree → never cached, always fresh
    crossrepo._provenance(str(repo), True, True)
    assert len(calls) == 2


def test_cache_invalidates_when_engine_fingerprint_changes(tmp_path, monkeypatch):
    # C1: a tool upgrade that changes extraction (not the JSON shape) must bust a clean-repo cache.
    repo = tmp_path / "r"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "models.py").write_text(
        "from django.db import models\nclass M(models.Model):\n    class Meta:\n        db_table = 't'\n")

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "x")

    calls, real = [], crossrepo.provenance.build
    monkeypatch.setattr(crossrepo.provenance, "build",
                        lambda p, with_calls=False: (calls.append(1), real(p, with_calls=with_calls))[1])
    monkeypatch.setattr(crossrepo, "_engine_fingerprint", lambda: "aaaaaaaaaaaa")
    crossrepo._provenance(str(repo), True, True)      # miss
    crossrepo._provenance(str(repo), True, True)      # hit (same fingerprint)
    assert len(calls) == 1
    monkeypatch.setattr(crossrepo, "_engine_fingerprint", lambda: "bbbbbbbbbbbb")   # tool "upgraded"
    crossrepo._provenance(str(repo), True, True)      # miss again — key changed
    assert len(calls) == 2


def test_unresolved_write_is_surfaced_not_dropped(tmp_path):
    # C2: a write on a var whose type we can't resolve (`<instance>`) must surface under a visible
    # `<unresolved>` table, not vanish — else a real writer (and a multi-writer) hides silently.
    r = tmp_path / "svc"
    _write(r, "app/urls.py",
           "from django.urls import path\nfrom . import views\nurlpatterns = [path('go/', views.go)]\n")
    _write(r, "app/views.py", "def go(request):\n    h = make_thing()\n    h.save()\n")
    tables = crossrepo.provenance.build(str(r))["tables"]
    assert "<unresolved>" in tables
    assert any(e["route"] == "go/" for e in tables["<unresolved>"]["written_by"])


def test_unresolved_is_not_a_cross_repo_multi_writer(tmp_path):
    # C2 guard: the per-repo `<unresolved>` bucket must NOT merge into a false cross-repo multi-writer.
    def mk(name):
        r = tmp_path / name
        _write(r, "app/urls.py",
               "from django.urls import path\nfrom . import views\nurlpatterns = [path('go/', views.go)]\n")
        _write(r, "app/views.py", "def go(request):\n    h = make_thing()\n    h.save()\n")
        return str(r)
    data = crossrepo.build([mk("a"), mk("b")])
    assert all(s["table"] != "<unresolved>" for s in data["shared_tables"])


def test_typed_queryset_var_update_is_a_write_but_dict_update_is_not(tmp_path):
    # C3: `qs = Model.objects.filter(...); qs.update(...)` is a real bulk write the `.objects` matcher can't
    # see (the chain is gone by the call); a bare `c.update({...})` on a dict must NOT become a write.
    r = tmp_path / "svc"
    _write(r, "app/models.py",
           "from django.db import models\nclass Lead(models.Model):\n"
           "    class Meta:\n        db_table = 'leads'\n")
    _write(r, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('bump/', views.bump), path('ctx/', views.ctx)]\n")
    _write(r, "app/views.py",
           "from .models import Lead\n"
           "def bump(request):\n    qs = Lead.objects.filter(status=1)\n    qs.update(status=2)\n"
           "def ctx(request):\n    c = {}\n    c.update({'a': 1})\n")
    tables = crossrepo.provenance.build(str(r))["tables"]
    assert any(e["route"] == "bump/" for e in tables["leads"]["written_by"])
    assert "<unresolved>" not in tables      # the dict update must not leak in as an unresolved write


def test_list_wrapped_queryset_keeps_its_model_for_the_loop_var(tmp_path):
    # `dus = list(M.objects.select_for_update().filter(...))`; `for du in dus: du.save()` — the list()
    # wrapper used to strip the type, so every du.save() fell into <unresolved> (the real cms pattern in
    # apply_campaign_action). A set() wrapper must NOT propagate: set.update() is a real method.
    r = tmp_path / "svc"
    _write(r, "app/models.py",
           "from django.db import models\nclass DU(models.Model):\n"
           "    class Meta:\n        db_table = 'dus'\n")
    _write(r, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('cascade/', views.cascade), path('tags/', views.tags)]\n")
    _write(r, "app/views.py",
           "from .models import DU\n"
           "def cascade(request):\n"
           "    dus = list(DU.objects.select_for_update().filter(campaign_id=1))\n"
           "    for du in dus:\n        du.save()\n"
           "def tags(request):\n"
           "    s = set(DU.objects.all())\n    s.update({1})\n")
    tables = crossrepo.provenance.build(str(r))["tables"]
    assert any(e["route"] == "cascade/" for e in tables["dus"]["written_by"])
    assert not any(e["route"] == "tags/" for e in tables["dus"]["written_by"])
    assert "<unresolved>" not in tables
