"""`lenscheck review --runtime`: runtime observations (lenscheck-contract record output) confirm or add
edges on the endpoints a PR changed, and never touch the others."""
import json
import subprocess

import runtime_merge


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_runtime_adds_the_call_static_analysis_could_not_name(tmp_path):
    from diff_pr import run_review
    r = tmp_path / "svc"
    _write(r, "app/urls.py", "from django.urls import path\nfrom . import views\n"
                             "urlpatterns = [path('pay/', views.pay), path('other/', views.other)]\n")
    _write(r, "app/views.py", "def pay(request):\n    return None\ndef other(request):\n    return None\n")
    _git(r, "init", "-q"); _git(r, "add", "-A"); _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD")
    _write(r, "app/views.py", "import requests\nfrom .gateway import url_for\n"
                              "def pay(request):\n    requests.post(url_for('charge'), json={})\n"
                              "def other(request):\n    return None\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "head")
    obs = tmp_path / "observed.json"
    obs.write_text(json.dumps({"app.views.pay": ["net:api.stripe.com"], "app.views.other": ["net:metrics.io"]}))

    res = run_review(str(r), base=base, head=_git(r, "rev-parse", "HEAD"), runtime=str(obs))
    rt = res["review"]["runtime"]
    assert rt["observations"] == 2
    assert [(e["route"], e["edge"]) for e in rt["evidence"]] == [("pay/", "e4_external")]   # other/ unchanged
    assert any("api.stripe.com" in i for i in rt["evidence"][0]["runtime_only"])
    assert "Runtime evidence" in res["report"] and "metrics.io" not in res["report"]


def test_load_observations_accepts_both_shapes():
    assert runtime_merge.load_observations({"m.v.f": ["net:x.io"]}) == \
        [{"handler": "m.v.f", "kind": "external", "target": "x.io", "n": 1}]
    rows = [{"route": "a/", "kind": "db", "target": "orders", "n": 3}, {"route": "b/", "kind": "nope"}]
    assert runtime_merge.load_observations(rows) == rows[:1]
