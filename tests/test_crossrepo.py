"""Cross-repo: background-job write attribution (#1) and HTTP-call coupling (#5)."""
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
