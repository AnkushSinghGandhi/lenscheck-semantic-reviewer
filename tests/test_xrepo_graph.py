"""Cross-repo dependency graph + change impact (xrepo_graph), and its wiring into
`lenscheck review --crossrepo` and `lenscheck crossrepo --changed`."""
import json
import subprocess

import crossrepo
import xrepo_graph


def _ep(route, handler, loc):
    return {"route": route, "handler": handler, "loc": loc}


A_TASK = _ep("task:sync_orders", "sync_orders", "a/tasks.py:3")
B_VIEW = _ep("report/", "report", "b/views.py:7")
C_VIEW = _ep("api/notify/", "notify", "c/views.py:2")
D_CMD = _ep("command:fix", "Command", "d/cmd.py:4")


def _data(match="exact", co_writer=False):
    by_repo = {"svcA": {"written_by": [A_TASK], "read_by": []},
               "svcB": {"written_by": [], "read_by": [B_VIEW]}}
    if co_writer:
        by_repo["svcD"] = {"written_by": [D_CMD], "read_by": []}
    return {
        "repos": sorted(by_repo) + ["svcC"],
        "shared_tables": [{"table": "orders", "database": "", "by_repo": by_repo}],
        "api_links": [{"from_repo": "svcB", "call": "/api/notify/", "match": match, "from": B_VIEW,
                       "to_repo": "svcC", "to": C_VIEW}],
        "task_links": [],
    }


def _impact(data, repo, files):
    g = xrepo_graph.build(data)
    return xrepo_graph.impact(g, xrepo_graph.seeds(g, repo, files))


def test_a_write_reaches_the_reader_and_what_the_reader_calls():
    imp = _impact(_data(), "svcA", {"a/tasks.py"})
    got = [(a["repo"], a["handler"], a["depth"], a["kind"], a["confidence"]) for a in imp["affected"]]
    assert got == [("svcB", "report", 1, "data", "exact"), ("svcC", "notify", 2, "data", "exact")]
    assert imp["repos"] == {"svcB": 1, "svcC": 1}
    assert "writes orders" in imp["affected"][1]["path"] and "calls /api/notify/" in imp["affected"][1]["path"]


def test_a_tail_link_makes_the_whole_path_verify():
    imp = _impact(_data(match="tail"), "svcA", {"a/tasks.py"})
    assert {a["repo"]: a["confidence"] for a in imp["affected"]} == {"svcB": "exact", "svcC": "verify"}


def test_other_writers_are_flagged_but_not_walked():
    imp = _impact(_data(co_writer=True), "svcA", {"a/tasks.py"})
    kinds = {a["repo"]: a["kind"] for a in imp["affected"]}
    assert kinds == {"svcB": "data", "svcC": "data", "svcD": "co-writer"}


def test_changing_a_route_flags_its_callers_only():
    imp = _impact(_data(), "svcC", {"c/views.py"})
    assert [(a["repo"], a["kind"], a["depth"]) for a in imp["affected"]] == [("svcB", "contract", 1)]


def test_unrelated_file_and_own_repo_report_nothing():
    assert _impact(_data(), "svcA", {"a/other.py"})["affected"] == []
    imp = _impact(_data(), "svcB", {"b/views.py"})        # svcB reads (no write) and calls svcC
    assert [(a["repo"], a["kind"]) for a in imp["affected"]] == [("svcC", "data")]
    assert xrepo_graph.render_markdown({"affected": []}) == ""


def test_celery_dispatch_edge_is_walked():
    data = _data()
    data["task_links"] = [{"from_repo": "svcC", "dispatch": "celery.send_task -> b.rebuild", "match": "exact",
                           "from": dict(C_VIEW, dispatch_loc="c/views.py:3"), "to_repo": "svcB",
                           "to": {"task": "rebuild", "names": ["rebuild"], "loc": "b/tasks.py:9"}}]
    imp = _impact(data, "svcB", {"b/views.py"})               # svcB → calls svcC → svcC dispatches to svcB
    assert [(a["repo"], a["handler"], a["depth"]) for a in imp["affected"]] == [("svcC", "notify", 1)]


# ---- end to end: real extraction, real git -------------------------------------------------------

def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _two_services(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _write(a, "app/models.py", "from django.db import models\n"
                               "class Order(models.Model):\n    class Meta:\n        db_table = 'orders'\n")
    _write(a, "app/tasks.py", "from celery import shared_task\nfrom .models import Order\n"
                              "@shared_task\ndef sync_orders():\n    Order.objects.create()\n")
    _write(b, "app/models.py", "from django.db import models\n"
                               "class Order(models.Model):\n    class Meta:\n        db_table = 'orders'\n")
    _write(b, "app/urls.py", "from django.urls import path\nfrom . import views\n"
                             "urlpatterns = [path('report/', views.report)]\n")
    _write(b, "app/views.py", "from .models import Order\n"
                              "def report(request):\n    return list(Order.objects.all())\n")
    _git(a, "init", "-q")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "base")
    return a, b


def test_review_crossrepo_lists_the_other_repos_reader(tmp_path):
    from diff_pr import run_review
    a, b = _two_services(tmp_path)
    base = _git(a, "rev-parse", "HEAD")
    _write(a, "app/tasks.py", "from celery import shared_task\nfrom .models import Order\n"
                              "@shared_task\ndef sync_orders():\n    Order.objects.create(status='new')\n")
    _git(a, "commit", "-qam", "head")
    res = run_review(str(a), base=base, head=_git(a, "rev-parse", "HEAD"), crossrepo=[str(b)])
    imp = res["review"]["crossrepo_impact"]
    assert [(x["repo"], x["handler"], x["kind"]) for x in imp["affected"]] == [("svcB", "report", "data")]
    assert imp["repo_meta"]["svcB"]["commit"] is None                    # not a git repo
    assert "Cross-repo impact" in res["report"]
    assert "crossrepo_impact" not in run_review(str(a), base=base, head="HEAD")["review"]


def test_crossrepo_changed_adds_an_impact_block(tmp_path):
    a, b = _two_services(tmp_path)
    _write(a, "app/tasks.py", "from celery import shared_task\nfrom .models import Order\n"
                              "@shared_task\ndef sync_orders():\n    Order.objects.create(status='x')\n")
    out = tmp_path / "o.json"
    assert crossrepo.main([str(a), str(b), "--changed", str(a), "--no-cache", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert [x["repo"] for x in data["impact"]["affected"]] == ["svcB"]
    assert data["repo_meta"]["svcA"]["commit"] and data["repo_meta"]["svcA"]["dirty"] is True


def test_contract_hits_are_not_walked_further():
    # svcB calls svcC AND writes `reports` that svcE reads. Changing svcC's route affects its caller
    # svcB (contract) — but svcB's own output didn't change, so svcE must NOT be reported.
    data = _data()
    data["shared_tables"].append({"table": "reports", "database": "", "by_repo": {
        "svcB": {"written_by": [B_VIEW], "read_by": []},
        "svcE": {"written_by": [], "read_by": [_ep("dash/", "dash", "e/views.py:1")]}}})
    imp = _impact(data, "svcC", {"c/views.py"})
    assert [(a["repo"], a["kind"]) for a in imp["affected"]] == [("svcB", "contract")]


def test_review_seeds_endpoints_changed_through_a_helper(tmp_path):
    # the PR only edits app/helper.py; the task that calls it gains a new table write → the semantic
    # diff flags the task's route, and that route seeds the cross-repo walk
    from diff_pr import run_review
    a, b = _two_services(tmp_path)
    _write(a, "app/helper.py", "from .models import Order\ndef save():\n    return Order.objects.first()\n")
    _write(a, "app/tasks.py", "from celery import shared_task\nfrom .models import Order\nfrom .helper import save\n"
                              "@shared_task\ndef sync_orders():\n    Order.objects.create()\n    save()\n")
    _git(a, "add", "-A")
    _git(a, "commit", "-qm", "helper")
    base = _git(a, "rev-parse", "HEAD")
    _write(a, "app/helper.py", "from .models import Order\nfrom django.db import models\n"
                               "class Audit(models.Model):\n    class Meta:\n        db_table = 'audit'\n"
                               "def save():\n    Audit.objects.create()\n    return Order.objects.first()\n")
    _git(a, "commit", "-qam", "helper writes audit")
    res = run_review(str(a), base=base, head=_git(a, "rev-parse", "HEAD"), crossrepo=[str(b)])
    assert [x["repo"] for x in res["review"]["crossrepo_impact"]["affected"]] == ["svcB"]
