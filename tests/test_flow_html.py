"""`lenscheck flow --html`: the story phases, table timeline, screenshots and self-contained page."""
import base64
import json

import flow

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _find(steps, kind, name):
    for s in steps:
        text = s.get("model") or s.get("text") or ""
        if s["kind"] == kind and text == name:
            return s
        found = _find(s.get("children") or [], kind, name)
        if found:
            return found
    return None


def _celery_repo(root):
    _write(root, "app/models.py",
           "from django.db import models\n"
           "class Job(models.Model):\n"
           "    status = models.IntegerField()\n"
           "    class Meta:\n        db_table = 'jobs'\n")
    _write(root, "app/tasks.py",
           "from celery import shared_task\nfrom .models import Job\n"
           "@shared_task\n"
           "def do_job(x):\n    Job.objects.create(status=0)\n")
    _write(root, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('push/', views.push)]\n")
    _write(root, "app/views.py",
           "from .tasks import do_job\n"
           "def push(request):\n"
           "    do_job.delay(1)\n")
    return root


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
           "import requests\nfrom django.conf import settings\nfrom .models import Item\n"
           "def push(request):\n"
           "    Item.objects.bulk_create([Item(job_id=j, flag=0) for j in [1]])\n"
           "    requests.post(f'{settings.B_HOST}/api/v1/score/', json={})\n")
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
           "    class Meta:\n        db_table = 'items'\n"
           "class Score(models.Model):\n    v = models.IntegerField()\n")
    _reader_command(root, "seed", "Item.objects.create(process=0)")
    _reader_command(root, "fresh_cmd", "Item.objects.exclude(job_id__isnull=True).first()")
    _reader_command(root, "legacy_cmd", "Item.objects.filter(process=0).first()")
    _write(root, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('api/v1/score/', views.score)]\n")
    _write(root, "app/views.py",
           "from .models import Score\n"
           "def score(request):\n    Score.objects.create(v=1)\n")
    return root


def _two_repos(tmp_path):
    a, b = tmp_path / "svcA", tmp_path / "svcB"
    _push_repo(a)
    _readers_repo(b)
    return flow.build_flow(str(a), "/push", crossrepo_paths=[str(b)]), a


def test_story_puts_a_celery_task_in_its_own_phase(tmp_path):
    a = _celery_repo(tmp_path / "svcA")
    f = flow.build_flow(str(a), "/push")
    phases, sp = flow.story(f)
    assert [p["kind"] for p in phases] == ["entry", "celery"]
    assert phases[1]["via"] == "celery"
    assert "jobs" in [t["table"] for t in phases[1]["tables"]]
    assert "jobs" not in [t["table"] for t in phases[0]["tables"]]
    dispatch = next(s for s in f["steps"] if s.get("boundary"))
    assert sp[dispatch["id"]] == phases[0]["id"]                 # the .delay() line is shown where it runs
    assert sp[dispatch["children"][0]["id"]] == phases[1]["id"]  # its body belongs to the task phase


def test_story_orders_entry_http_then_table_phase(tmp_path):
    f, _a = _two_repos(tmp_path)
    phases, _sp = flow.story(f)
    assert [p["kind"] for p in phases] == ["entry", "http", "table"]
    assert phases[1]["repo"] == "svcB"
    assert phases[2]["via"] == "reads items"
    assert sum(1 for c in phases[2]["candidates"] if c["expanded"]) == 1


def test_table_timeline_is_write_first_then_the_cross_repo_read(tmp_path):
    f, _a = _two_repos(tmp_path)
    phases, sp = flow.story(f)
    tl = flow.table_timeline(f, phases, sp)
    item = next(t for t in tl if t["table"] == "items")
    assert item["written"] is True
    assert item["events"][0]["op"] == "W"
    assert item["events"][0]["repo"] == "svcA"
    assert item["events"][0].get("set")
    later = next(e for e in item["events"][1:]
                 if e["op"] == "R" and e["repo"] == "svcB")
    assert later["where_not"] == {"job_id__isnull": True}
    assert all(e.get("phase") and e.get("step") for e in item["events"])


def test_write_html_inlines_data_and_glossary_without_breaking_script_tags(tmp_path):
    a = tmp_path / "svcA"
    _write(a, "app/models.py",
           "from django.db import models\n"
           "class Item(models.Model):\n    flag = models.IntegerField()\n")
    _write(a, "app/urls.py",
           "from django.urls import path\nfrom . import views\n"
           "urlpatterns = [path('push/', views.push)]\n")
    _write(a, "app/views.py",
           "from .models import Item\n"
           "def push(request):\n"
           "    Item.objects.create(flag=0); x = \"</script>\"\n")
    f = flow.build_flow(str(a), "/push")
    out = tmp_path / "flow.html"
    flow.write_html(f, str(out))

    html = out.read_text(encoding="utf-8")
    assert "LC_GLOSSARY" in html and 'src="glossary.js"' not in html
    assert "<\\/script>" in html
    assert html.count("</script>") == html.count("<script")

    raw = html.split("window.FLOW_DATA = ", 1)[1].split(";</script>", 1)[0]
    data = json.loads(raw)
    assert set(data) >= {"flow", "story", "step_phase", "timeline", "sources"}


def test_screens_attach_skip_bad_extensions_and_oversized_files(tmp_path, capsys):
    a = tmp_path / "svcA"
    _write(a, "app.py",
           "from fastapi import FastAPI\n"
           "app = FastAPI()\n"
           "class M:\n    objects = None\n"
           "@app.get('/h')\n"
           "def handler():\n    M.objects.create(x=1)\n")
    f = flow.build_flow(str(a), "/h")
    phases, sp = flow.story(f)
    assert sp.get("s1")

    png = tmp_path / "a.png"
    png.write_bytes(PNG)
    notes = tmp_path / "notes.txt"
    notes.write_text("not an image")
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"\x89PNG" + b"\0" * (5 * 1024 * 1024))

    out = tmp_path / "flow.html"
    flow.write_html(f, str(out), screens=[f"s1={png}", str(notes), str(huge)])

    err = capsys.readouterr().err
    assert "notes.txt" in err
    assert "huge.png" in err
    raw = out.read_text(encoding="utf-8").split("window.FLOW_DATA = ", 1)[1].split(";</script>", 1)[0]
    data = json.loads(raw)
    phase = next(p for p in data["story"] if p["id"] == sp["s1"])
    assert len(phase["screens"]) == 1
    assert phase["screens"][0].startswith("data:image/png;base64,")


def test_cli_writes_the_html_page(tmp_path, monkeypatch):
    a = _celery_repo(tmp_path / "svcA")
    out = tmp_path / "flow.html"
    monkeypatch.setattr("sys.argv", ["flow", str(a), "/push", "--html", str(out)])
    assert flow.main() == 0
    assert out.exists()
