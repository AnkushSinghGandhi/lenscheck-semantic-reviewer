"""The QDD bridge: `provenance.build()` turns extracted facts into table→endpoint code provenance and
FK-derived data-consistency rules (in QDD's native source/target/expect format)."""
import os
import tempfile

import provenance


def _repo():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "models.py"), "w") as f:
        f.write("class Lead(models.Model):\n    class Meta:\n        db_table = 'leads'\n"
                "class Payment(models.Model):\n"
                "    lead = models.ForeignKey(Lead, on_delete=models.CASCADE)\n"
                "    class Meta:\n        db_table = 'payments'\n")
    with open(os.path.join(d, "views.py"), "w") as f:
        f.write("from rest_framework.views import APIView\n"
                "class Checkout(APIView):\n"
                "    def post(self, r):\n        Payment.objects.create(amount=1)\n        return None\n")
    with open(os.path.join(d, "urls.py"), "w") as f:
        f.write("from django.urls import path\nfrom .views import Checkout\n"
                "urlpatterns = [path('checkout', Checkout.as_view())]\n")
    return d


def test_code_provenance_maps_table_to_writing_endpoint():
    data = provenance.build(_repo())
    assert "payments" in data["tables"], data["tables"].keys()
    writers = [e["handler"] for e in data["tables"]["payments"]["written_by"]]
    assert "Checkout" in writers, writers


def test_fk_orphan_rule_is_generated_in_qdd_format():
    rules = [r for r in provenance.build(_repo())["consistency_rules"]
             if r["kind"] == "referential_integrity"]
    assert any(r["qdd"]["source"] == {"table": "payments", "key": "lead_id"}
               and r["qdd"]["target"] == {"table": "leads", "key": "id"}
               and r["qdd"]["expect"] == "exists"
               for r in rules), rules
