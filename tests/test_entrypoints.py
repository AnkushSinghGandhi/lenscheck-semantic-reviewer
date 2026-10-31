"""Non-HTTP entrypoints in the review + map: celery tasks, management commands and signal receivers are
analyzed like endpoints, but judged as what they are — not reachable over HTTP, so never an
"unauthenticated" finding — and linked to the endpoints that enqueue them."""
import pytest

import map_repo
from diff_pr import CRIT, auth_str, diff, endpoint_risk, is_open
from extractor import analyze_repo
from invariants import eval_auth_before_write


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "svc"
    _write(r, "app/models.py", "from django.db import models\n"
           "class Lead(models.Model):\n    class Meta:\n        db_table = 'leads'\n"
           "class Audit(models.Model):\n    class Meta:\n        db_table = 'audits'\n")
    _write(r, "app/tasks.py", "from celery import shared_task\nfrom .models import Lead\n"
           "@shared_task\ndef process(lead_id):\n    Lead.objects.filter(pk=lead_id).update(status=1)\n")
    _write(r, "app/signals.py",
           "from django.db.models.signals import post_save, post_delete\nfrom django.dispatch import receiver\n"
           "from .models import Lead, Audit\n"
           "@receiver(post_save, sender=Lead)\ndef on_lead_saved(sender, instance, **kw):\n"
           "    Audit.objects.create(note='saved')\n"
           "def on_lead_change(sender, instance, **kw):\n    Audit.objects.create(note='changed')\n"
           "post_save.connect(on_lead_change, sender=Lead)\n"
           "post_delete.connect(on_lead_change, sender=Lead)\n"
           "client.connect(on_lead_change)\n")                       # not a Django signal → must not count
    _write(r, "app/management/commands/backfill.py",
           "from django.core.management.base import BaseCommand\nfrom app.models import Lead\n"
           "class Command(BaseCommand):\n    def handle(self, *a, **k):\n        Lead.objects.create(status=0)\n")
    _write(r, "app/views.py", "from .tasks import process\ndef upload(request):\n    process.delay(1)\n")
    _write(r, "app/urls.py", "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('upload/', views.upload)]\n")
    return str(r)


def test_every_entrypoint_kind_is_discovered_with_its_trigger(repo):
    eps = {e.route: e for e in analyze_repo(repo)}
    assert eps["upload/"].kind == "http"
    assert eps["task:process"].kind == "task" and eps["task:process"].methods == ["celery"]
    assert eps["command:backfill"].kind == "command"
    assert eps["signal:on_lead_saved"].methods == ["post_save:Lead"]
    # one handler wired to two senders is ONE entrypoint listing both — and `client.connect` isn't one
    assert set(eps["signal:on_lead_change"].methods) == {"post_save:Lead", "post_delete:Lead"}


def test_jobs_are_never_unauthenticated_findings(repo):
    eps = analyze_repo(repo)
    for e in eps:
        if e.kind == "http":
            continue
        sev, why = endpoint_risk(e)
        assert sev != CRIT and not is_open(e) and auth_str(e).startswith("n/a"), e.route
        assert "unauthenticated" not in why
    # the auth invariants only judge HTTP entrypoints: writing jobs aren't "auth-before-write" subjects
    subjects, _ok, exc = eval_auth_before_write(eps)["auth-before-write"]
    assert subjects == 0 and not exc


def test_map_shows_kinds_and_what_an_endpoint_enqueues(repo):
    m = map_repo.build_map(repo)
    recs = {r["route"]: r for a in m["apps"] for r in a["endpoints"]}
    assert m["kinds"] == {"http": 1, "task": 1, "command": 1, "signal": 2}
    assert recs["upload/"]["enqueues"] == [{"task": "task:process", "writes": ["Lead"]}]
    assert recs["task:process"]["kind"] == "task" and recs["task:process"]["methods"] == ["celery"]


def test_review_flags_a_new_task_and_who_triggers_it(repo):
    head = analyze_repo(repo)
    base = [e for e in head if e.route != "task:process"]          # the PR adds the task
    ch = next(c for c in diff(base, head) if c["route"] == "task:process")
    assert ch["kind"] == "NEW ENDPOINT" and ch["entry"] == "task" and ch["sev"] != CRIT
    assert "celery task" in ch["why"] and "triggered by `upload/`" in ch["why"]
