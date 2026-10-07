"""The QDD bridge: `provenance.build()` must turn extracted facts into (1) table→endpoint code
provenance and (2) FK-derived consistency rules in QDD's format — a stable, documented JSON contract
tools can shell out to. A small Django fixture with one FK (Payment.lead → Lead) exercises both."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import provenance  # noqa: E402


def _repo():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "models.py"), "w") as f:
        f.write("from django.db import models\n"
                "class Lead(models.Model):\n    class Meta:\n        db_table = 'leads'\n"
                "class Payment(models.Model):\n"
                "    lead = models.ForeignKey(Lead, on_delete=models.CASCADE)\n"
                "    class Meta:\n        db_table = 'payments'\n")
    with open(os.path.join(d, "views.py"), "w") as f:
        f.write("from rest_framework.views import APIView\n"
                "from .models import Payment\n"
                "class CreatePayment(APIView):\n"
                "    def post(self, request):\n"
                "        Payment.objects.create(amount=1)\n"
                "        return None\n")
    with open(os.path.join(d, "urls.py"), "w") as f:
        f.write("from django.urls import path\nfrom .views import CreatePayment\n"
                "urlpatterns = [path('pay', CreatePayment.as_view())]\n")
    return d


def test_contract_shape_is_stable():
    data = provenance.build(_repo())
    assert data["schema"] == provenance.SCHEMA          # consumers pin on this
    for k in ("repo", "summary", "tables", "consistency_rules"):
        assert k in data, f"missing contract key {k!r}"


def test_code_provenance_maps_table_to_writing_endpoint():
    data = provenance.build(_repo())
    pay = data["tables"].get("payments")
    assert pay is not None, f"payments not in {list(data['tables'])}"
    assert any(e["handler"] == "CreatePayment" for e in pay["written_by"]), \
        "payments should be written_by CreatePayment"


def test_foreign_key_becomes_a_qdd_orphan_rule():
    rules = provenance.build(_repo())["consistency_rules"]
    fk = [r for r in rules if r["kind"] == "referential_integrity"]
    rule = next((r for r in fk if r["qdd"]["source"]["table"] == "payments"), None)
    assert rule is not None, "no orphan rule for payments"
    q = rule["qdd"]
    assert q["source"]["key"] == "lead_id"              # Django FK column convention
    assert q["target"] == {"table": "leads", "key": "id"}
    assert q["expect"] == "exists"


def test_main_is_a_clean_subprocess_contract(capsys, tmp_path):
    # exit 0 on success, pure JSON on stdout, human text on stderr; exit 3 on a bad repo
    import json
    code = provenance.main([_repo()])
    out = capsys.readouterr()
    assert code == 0
    assert json.loads(out.out)["schema"] == provenance.SCHEMA      # stdout parses, nothing else on it
    assert "endpoints" in out.err                                   # summary went to stderr

    bad = provenance.main([str(tmp_path / "nope")])
    out = capsys.readouterr()
    assert bad == 3 and out.out == ""                               # nothing on stdout when it fails


def test_out_flag_writes_a_shareable_file(capsys, tmp_path):
    # a dev runs `--out file.json` to hand a facts-only file to someone without repo access
    import json
    f = tmp_path / "prov.json"
    code = provenance.main([_repo(), "--out", str(f)])
    out = capsys.readouterr()
    assert code == 0
    assert out.out == ""                                            # --out suppresses stdout
    assert json.loads(f.read_text())["schema"] == provenance.SCHEMA  # the file is a valid contract


def test_tables_carry_pii_fields():
    """Each table carries the PII-egress signal (empty here — the fixture has no PII flow)."""
    t = provenance.build(_repo())["tables"]["payments"]
    assert "pii_off_platform" in t and "pii_to_client" in t
