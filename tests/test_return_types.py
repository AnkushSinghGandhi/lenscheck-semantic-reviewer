"""Return-type inference + typed-var chaining: a write on a var that came from a repo helper's return
(`obj = self.get_object(pk); obj.save()`) resolves to the helper's model instead of `<unresolved>`."""
import provenance


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


def _fixture(tmp_path):
    r = tmp_path / "svc"
    _write(r, "app/models.py",
           "from django.db import models\n"
           + "".join(f"class {m}(models.Model):\n    class Meta:\n        db_table = '{t}'\n"
                     for m, t in [("Campaign", "campaigns"), ("ConfigRow", "config_rows"), ("Group", "groups"),
                                  ("Lead", "leads"), ("Other", "others")]))
    _write(r, "app/helpers.py",
           "from .models import ConfigRow, Group, Lead, Other\n"
           "def get_config_row(cid):\n"
           "    qs = ConfigRow.objects.using('default')\n"
           "    return qs.filter(campaign_id=cid).order_by('-id').first()\n"
           "def group_or_404(gid):\n"
           "    group = Group.objects.filter(pk=gid).first()\n"
           "    if group is None:\n        raise ValueError('missing')\n"
           "    return group\n"
           "def either(flag):\n"
           "    if flag:\n        return Lead.objects.first()\n"
           "    return Other.objects.first()\n"          # two different models → ambiguous
           "def payload(x):\n    return {'x': x}\n"     # not a model
           "def loop(n):\n    x = loop(n - 1)\n    return x\n")   # self-recursive — must not hang
    _write(r, "app/views.py",
           "from rest_framework.views import APIView\nfrom rest_framework.response import Response\n"
           "from .models import Campaign, Lead\n"
           "from .helpers import get_config_row, group_or_404, either, payload, loop\n"
           "class CampaignDetail(APIView):\n"
           "    def get_object(self, pk):\n"
           "        try:\n            return Campaign.objects.get(pk=pk)\n"
           "        except Campaign.DoesNotExist:\n            return Response(status=404)\n"
           "    def put(self, request, pk):\n        obj = self.get_object(pk)\n        obj.save()\n"
           "    def delete(self, request, pk):\n        self.get_object(pk).delete()\n"
           "def touch_config(request):\n    row = get_config_row(1)\n    row.save()\n"
           "def rename_group(request):\n    g = group_or_404(2)\n    g.save()\n"
           "def ambiguous(request):\n    x = either(True)\n    x.save()\n"
           "def not_model(request):\n    p = payload(1)\n    p.update({'y': 2})\n"
           "def recurse(request):\n    v = loop(3)\n    v.save()\n"
           "def chained(request):\n    qs = Lead.objects.all()\n    lead = qs.first()\n    lead.save()\n")
    _write(r, "app/urls.py",
           "from django.urls import path\nfrom . import views\nurlpatterns = [\n"
           "    path('campaign/<int:pk>/', views.CampaignDetail.as_view()),\n"
           "    path('config/', views.touch_config), path('group/', views.rename_group),\n"
           "    path('amb/', views.ambiguous), path('nm/', views.not_model),\n"
           "    path('rec/', views.recurse), path('chain/', views.chained),\n]\n")
    return provenance.build(str(r))["tables"]


def _writers(tables, table):
    return {e["route"] for e in tables.get(table, {}).get("written_by", [])}


def test_helper_return_types_resolve_the_write(tmp_path):
    t = _fixture(tmp_path)
    # self.get_object(): Campaign.objects.get(...) — the Response(404) error path doesn't make it ambiguous;
    # covers both `obj = self.get_object(pk); obj.save()` and `self.get_object(pk).delete()`
    assert "campaign/<int:pk>/" in _writers(t, "campaigns")
    assert "config/" in _writers(t, "config_rows")       # local qs → qs.filter().order_by().first()
    assert "group/" in _writers(t, "groups")             # `return group` (a local typed var)
    assert "chain/" in _writers(t, "leads")              # typed-var chain: lead = qs.first()


def test_return_inference_stays_conservative(tmp_path):
    t = _fixture(tmp_path)
    unresolved = _writers(t, "<unresolved>")
    assert "amb/" in unresolved                          # Lead-or-Other → ambiguous → not guessed
    assert "amb/" not in _writers(t, "leads") | _writers(t, "others")
    assert "rec/" in unresolved                          # recursion terminates, nothing invented
    every_writer = {r for tbl in t.values() for r in (e["route"] for e in tbl["written_by"])}
    assert "nm/" not in every_writer                     # a dict's .update() from payload() is not a DB write
