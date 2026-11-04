"""Column-level write tracking: the extractor records the columns a write names, provenance reports
them per table, and crossrepo intersects them across the writing repos."""
import crossrepo
import provenance
from extractor.analyzer import analyze_repo


def _write(root, rel, src):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)


MODELS = ("from django.db import models\n"
          "class Order(models.Model):\n    class Meta:\n        db_table = 'orders'\n")


def _repo(root, views):
    """A tiny Django repo: Order model + one url per `def name(request)` body in `views`."""
    _write(root, "app/models.py", MODELS)
    urls = "from django.urls import path\nfrom . import views\nurlpatterns = [\n"
    src = "from .models import Order\n"
    for name, body in views.items():
        urls += f"    path('{name}/', views.{name}),\n"
        src += f"def {name}(request):\n" + "".join(f"    {ln}\n" for ln in body.splitlines()) + "\n"
    _write(root, "app/urls.py", urls + "]\n")
    _write(root, "app/views.py", src)
    return str(root)


def _items(root, model="Order"):
    return [w for e in analyze_repo(root) for w in e.writes_cols if w["model"] == model]


def test_create_records_kwarg_columns(tmp_path):
    root = _repo(tmp_path / "svc", {"make": 'Order.objects.create(total=1, status="new")'})
    items = _items(root)
    assert len(items) == 1 and items[0]["cols"] == ["status", "total"]


def test_save_update_fields_records_those_columns(tmp_path):
    root = _repo(tmp_path / "svc", {"ship": 'o = Order.objects.get(pk=1)\no.save(update_fields=["delivery_status"])'})
    items = _items(root)
    assert len(items) == 1 and items[0]["cols"] == ["delivery_status"]


def test_plain_save_is_unknown_columns(tmp_path):
    root = _repo(tmp_path / "svc", {"touch": "o = Order.objects.get(pk=1)\no.save()"})
    items = _items(root)
    assert len(items) == 1 and items[0]["cols"] is None


def test_provenance_written_columns_union_and_unknown(tmp_path):
    views = {"a": "Order.objects.create(a=1)",
             "b": 'o = Order.objects.get(pk=1)\no.save(update_fields=["b"])'}
    root = _repo(tmp_path / "svc", views)
    t = provenance.build(root)["tables"]["orders"]
    assert t["written_columns"] == ["a", "b"] and t["write_cols_complete"] is True

    views["c"] = "o = Order.objects.get(pk=1)\no.save()"      # a whole-row write → columns unknown
    root = _repo(tmp_path / "svc2", views)
    t = provenance.build(root)["tables"]["orders"]
    assert t["written_columns"] == ["a", "b"]                 # the known union survives
    assert t["write_cols_complete"] is False                  # ...but is flagged incomplete


def test_crossrepo_disjoint_columns_have_empty_intersection(tmp_path):
    a = _repo(tmp_path / "svcA", {"wx": "Order.objects.create(x=1)"})
    b = _repo(tmp_path / "svcB", {"wy": "Order.objects.create(y=2)"})
    t = {s["table"]: s for s in crossrepo.build([a, b], use_cache=False)["shared_tables"]}["orders"]
    assert t["kind"] == "multi_writer"
    assert t["shared_write_columns"] == []
    assert t["write_columns"] == {"svcA": ["x"], "svcB": ["y"]}
    assert t["distinct_write_columns"] == {"svcA": ["x"], "svcB": ["y"]}
    assert t["write_cols_complete"] == {"svcA": True, "svcB": True}


def test_known_column_survives_an_unknown_write(tmp_path):
    a = _repo(tmp_path / "svcA", {"ship": 'o = Order.objects.get(pk=1)\no.save(update_fields=["delivery_status"])',
                                  "touch": "o = Order.objects.get(pk=1)\no.save()"})
    b = _repo(tmp_path / "svcB", {"look": "Order.objects.get(pk=1)"})    # a reader, so the table is shared
    t = {s["table"]: s for s in crossrepo.build([a, b], use_cache=False)["shared_tables"]}["orders"]
    assert "delivery_status" in t["write_columns"]["svcA"]
    assert t["write_cols_complete"]["svcA"] is False
