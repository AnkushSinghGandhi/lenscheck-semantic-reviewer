"""`lenscheck serve` Flow tab: the page in served mode, /api/flow/candidates and /api/flow, and the same trust
boundary as the other endpoints (token, repo allowlist for every path, no option-like values). Real handler on an
ephemeral port."""
import contextlib
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote, urlencode

import pytest

import serve

APPS = "from django.apps import AppConfig\nclass AppCfg(AppConfig):\n    name = 'app'\n"


@contextlib.contextmanager
def _serve(**over):
    cfg = {"repo": "", "invariants": "", "token": "", "allowed": [], "base_path": "", "preload": {},
           "demos": {}, "demo_dir": ""}
    cfg.update(over)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()


def _req(addr, path):
    c = HTTPConnection(*addr, timeout=60)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read().decode("utf-8")


def _api(addr, path, **params):
    status, body = _req(addr, path + "?" + urlencode(params))
    return status, json.loads(body or "{}")


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "allowed" / "svc"
    _write(root, "app/apps.py", APPS)
    _write(root, "app/models.py", "from django.db import models\n"
           "class Lead(models.Model):\n    v = models.IntegerField()\n    class Meta:\n        db_table = 'leads'\n")
    _write(root, "app/views.py", "from .models import Lead\n"
           "def lead_upload(request):\n    Lead.objects.create(v=1)\n"
           "def user_upload(request):\n    Lead.objects.filter(v=1).first()\n")
    _write(root, "app/urls.py", "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('lead/upload/', views.lead_upload), path('user/upload/', views.user_upload)]\n")
    _write(root, ".flows/push.json", json.dumps({"name": "push", "title": "Push",
                                                 "steps": [{"name": "Upload", "entries": ["lead_upload"]},
                                                           {"name": "Check", "entries": ["user_upload"]}]}))
    (tmp_path / "outside").mkdir()
    return {"root": str(root), "allowed": str(tmp_path / "allowed"), "outside": str(tmp_path / "outside"),
            "tmp": tmp_path}


def test_flow_page_is_served_without_baked_data(repo):
    with _serve() as addr:
        status, html = _req(addr, "/flow")
    assert status == 200 and "window.FLOW_DATA = null;" in html and 'id="flowform"' in html
    assert 'src="glossary.js"' in html


def test_candidates_list_entrypoints_and_repo_journeys(repo):
    with _serve() as addr:
        status, body = _api(addr, "/api/flow/candidates", repo=repo["root"])
    assert status == 200
    assert {"lead_upload", "user_upload"} <= {e["handler"] for e in body["entries"]}
    assert [j["name"] for j in body["journeys"]] == ["push"]


def test_flow_returns_page_data_and_ambiguity_then_pick(repo):
    with _serve() as addr:
        status, body = _api(addr, "/api/flow", repo=repo["root"], q="lead_upload")
        assert status == 200 and body["narrative_meta"]["source"] == "facts"
        assert {"flow", "story", "timeline", "sources", "narrative"} <= set(body)
        assert any(t["table"] == "leads" for t in body["timeline"])
        status, body = _api(addr, "/api/flow", repo=repo["root"], q="upload")
        assert status == 409 and len(body["ranked"]) >= 2
        status, body = _api(addr, "/api/flow", repo=repo["root"], q="upload", pick="1")
        assert status == 200 and body["flow"]["entry"]["handler"] in {"lead_upload", "user_upload"}


def test_flow_runs_a_journey_file(repo):
    with _serve() as addr:
        status, body = _api(addr, "/api/flow", repo=repo["root"], journey=repo["root"] + "/.flows/push.json")
    assert status == 200
    assert [p["kind"] for p in body["story"]] == ["step", "step"]


def test_flow_trust_boundary(repo):
    root, outside = repo["root"], repo["outside"]
    bad_journey = repo["tmp"] / "outside" / "j.json"
    bad_journey.write_text("{}")
    with _serve(allowed=[repo["allowed"]]) as addr:
        assert _api(addr, "/api/flow", repo=outside, q="x")[0] == 403
        assert _api(addr, "/api/flow/candidates", repo=root, journeys=outside)[0] == 403
        assert _api(addr, "/api/flow", repo=root, q="lead_upload", crossrepo=outside)[0] == 403
        assert _api(addr, "/api/flow", repo=root, journey=str(bad_journey))[0] == 403
        assert _api(addr, "/api/flow", repo=root, journey=root + "/app/views.py")[0] == 403
        assert _api(addr, "/api/flow", repo=root, q="lead_upload", crossrepo="-x")[0] == 400
        assert _api(addr, "/api/flow", repo=root)[0] == 400                      # neither q nor journey
    with _serve(token="s3cret") as addr:
        assert _api(addr, "/api/flow", repo=root, q="lead_upload")[0] == 401
        assert _api(addr, "/api/flow", repo=root, q="lead_upload", token="s3cret")[0] == 200


def test_one_view_on_two_urls_is_not_ambiguous(tmp_path):
    import flow
    root = tmp_path / "svc"
    _write(root, "app/apps.py", APPS)
    _write(root, "app/models.py", "from django.db import models\nclass Lead(models.Model):\n    v = models.IntegerField()\n")
    _write(root, "app/views.py", "from .models import Lead\ndef target(request):\n    Lead.objects.create(v=1)\n")
    _write(root, "app/urls.py", "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('a/<int:pk>/', views.target), path('b/<int:pk>/target/', views.target)]\n")
    assert flow.resolve(flow.candidates(str(root)), "target")["handler"] == "target"
