"""Injected models: a model that reaches `.objects` through an attribute, not its name. T0 — a class
attribute (`model = Job` + `self.model.objects`); T1 — a config object built with the model (a dataclass
`SPEC = QueueSpec(model=M)` or a service whose `__init__` stores it); T2 — that object passed on as an
argument. Bindings are made only on a fully static chain to a repo model, else the fact stays `<instance>`."""
import provenance
from extractor import analyze_repo


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


MODELS = ("from django.db import models\n"
          "class Job(models.Model):\n    class Meta:\n        db_table = 'jobs'\n"
          "class FreshQueue(models.Model):\n    class Meta:\n        db_table = 'fresh_q'\n"
          "class RetryQueue(models.Model):\n    class Meta:\n        db_table = 'retry_q'\n"
          "class NotAModel:\n    pass\n")


def _eps(tmp_path, files):
    r = tmp_path / "svc"
    _write(r, "app/models.py", MODELS)
    for rel, src in files.items():
        _write(r, rel, src)
    return {e.route: e for e in analyze_repo(str(r))}, str(r)


def _facts(ep):
    """{(model, kind)} with the via-call / location noise stripped."""
    return {tuple(i.split(" ")[0].split(":")[:2]) for i in ep.e3_db_tables.items}


URLS = "from django.urls import path\nfrom . import views\nurlpatterns = [path('jobs/', views.JobList.as_view())]\n"


def test_t0_class_attribute_model_on_the_view_and_its_base(tmp_path):
    eps, _ = _eps(tmp_path, {"app/urls.py": URLS, "app/views.py":
        "from django.views import View\nfrom . import models\nfrom .models import Job\n"
        "class Base(View):\n    model = Job\n"
        "class JobList(Base):\n"
        "    def get(self, request):\n        return list(self.model.objects.filter(active=1))\n"
        "    def post(self, request):\n        self.model.objects.filter(pk=1).update(active=0)\n"
        "        models.Job.objects.create(active=1)\n"})        # module-qualified model, also missed before
    assert _facts(eps["jobs/"]) == {("Job", "read"), ("Job", "write")}


ENGINE = ("from dataclasses import dataclass\nfrom typing import Any\n"
          "from django.core.management.base import BaseCommand\nfrom app.models import FreshQueue, RetryQueue\n"
          "@dataclass(frozen=True)\nclass QueueSpec:\n    label: str\n    model: Any\n"
          "    def pending(self):\n        return self.model.objects.filter(status=0)\n"
          "    def rows(self):\n        return list(self.pending())\n"
          "    def mark_done(self, row):\n        self.model.objects.filter(pk=row.pk).update(status=1)\n"
          "SPEC_FRESH = QueueSpec(label='fresh', model=FreshQueue)\n"
          "SPEC_RETRY = QueueSpec(label='retry', model=RetryQueue)\n"
          "def deliver(spec, row):\n    spec.mark_done(row)\n"
          "class BasePushCommand(BaseCommand):\n"
          "    def handle(self, *a, **k):\n        spec = self.spec\n"
          "        for row in spec.rows():\n            deliver(spec, row)\n"
          "        self.audit(spec)\n"
          "    def audit(self, spec):\n        spec.pending()\n")


def test_t1_t2_config_object_reads_and_writes_only_its_own_queue(tmp_path):
    eps, _ = _eps(tmp_path, {
        "app/engine.py": ENGINE,
        "app/management/commands/push_fresh.py":
            "from app.engine import BasePushCommand, SPEC_FRESH\nclass Command(BasePushCommand):\n    spec = SPEC_FRESH\n",
        "app/management/commands/push_retry.py":
            "from app.engine import BasePushCommand, SPEC_RETRY\nclass Command(BasePushCommand):\n    spec = SPEC_RETRY\n"})
    # T1 read (spec.rows → self.pending) + T2 write (deliver(spec, row) → spec.mark_done), per command
    assert _facts(eps["command:push_fresh"]) == {("FreshQueue", "read"), ("FreshQueue", "write")}
    assert _facts(eps["command:push_retry"]) == {("RetryQueue", "read"), ("RetryQueue", "write")}


def test_t1_service_storing_ctor_models_and_two_specs_in_one_walk(tmp_path):
    eps, _ = _eps(tmp_path, {"app/urls.py":
        "from django.urls import path\nfrom . import views\n"
        "urlpatterns = [path('svc/', views.svc_view), path('both/', views.both)]\n",
        "app/engine.py": ENGINE,
        "app/views.py":
        "from .models import Job\nfrom .engine import SPEC_FRESH, SPEC_RETRY, deliver\n"
        "class JobService:\n    def __init__(self, job_model):\n        self.Job = job_model\n"
        "    def close(self):\n        self.Job.objects.filter(pk=1).delete()\n"
        "def svc_view(request):\n    svc = JobService(Job)\n    svc.close()\n"
        "def both(request):\n    deliver(SPEC_FRESH, 1)\n    deliver(SPEC_RETRY, 2)\n"})
    assert _facts(eps["svc/"]) == {("Job", "write")}
    # the same helper reached with two different specs → both get analyzed (binding is in the follow key)
    assert _facts(eps["both/"]) == {("FreshQueue", "write"), ("RetryQueue", "write")}


def test_t2_through_a_repo_module_imported_absolutely(tmp_path):
    # `from app import engine` + `engine.deliver(spec, …)` — the real legacy path (`push_queue.deliver_row`)
    # was invisible: only relative `from . import mod` modules were followed.
    eps, _ = _eps(tmp_path, {"app/engine.py": ENGINE, "app/urls.py":
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('push/', views.push)]\n",
        "app/views.py": "from app import engine\n"
                        "def push(request):\n    engine.deliver(engine.SPEC_RETRY, 1)\n"})
    assert _facts(eps["push/"]) == {("RetryQueue", "write")}


def test_no_static_chain_means_no_guess_and_unresolved_reads_are_surfaced(tmp_path):
    eps, root = _eps(tmp_path, {"app/urls.py":
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('x/', views.X.as_view())]\n",
        "app/views.py":
        "from django.views import View\nfrom .models import NotAModel\n"
        "class X(View):\n    model = NotAModel\n"                    # not a model → never bound
        "    def get(self, request):\n        return self.model.objects.all()\n"})
    assert _facts(eps["x/"]) == {("<instance>", "read")}
    tables = provenance.build(root)["tables"]
    assert any(e["route"] == "x/" for e in tables["<unresolved>"]["read_by"])
