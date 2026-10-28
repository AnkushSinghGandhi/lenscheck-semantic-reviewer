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
