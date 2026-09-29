"""M6 PoC — runtime observations sharpen static facts, and NEVER downgrade them."""
from runtime_merge import merge, VERIFIED, POTENTIAL


def _facts():
    return [{
        "route": "api/orders", "handler": "CreateOrder", "methods": ["POST"],
        "e3_db_tables": {"status": POTENTIAL,
                         "items": ["OrderItem:write (via call) @ helper.py:12"],
                         "note": "write hidden in manager/service"},
        "e4_external": {"status": POTENTIAL, "items": ["requests.post -> ? @ pay.py:5"], "note": "dest unresolved"},
        "e6_pii": {"status": "?", "items": ["email @ v.py:3"], "note": ""},
        "tables": {"OrderItem": {"table": "order_items", "explicit": True}},   # model→table bridge
    }]


def test_runtime_confirms_upgrades_potential_to_verified():
    rt = [{"route": "api/orders", "method": "POST", "kind": "db",
           "target": "order_items", "op": "write", "n": 142}]
    out = merge(_facts(), rt)
    e3 = out[0]["e3_db_tables"]
    assert e3["status"] == VERIFIED                       # ⚠ → ✓ once runtime proves the write
    assert "runtime-confirmed (142 traces)" in e3["note"]  # provenance preserved


def test_runtime_reveals_edge_static_missed():
    rt = [{"route": "api/orders", "method": "POST", "kind": "external",
           "target": "api.stripe.com", "n": 88}]
    out = merge(_facts(), rt)
    e4 = out[0]["e4_external"]
    assert any("api.stripe.com" in it and "runtime-only" in it for it in e4["items"])


def test_pii_egress_is_traced():
    rt = [{"route": "api/orders", "method": "POST", "kind": "pii-egress",
           "target": "api.stripe.com", "field": "email", "n": 88}]
    out = merge(_facts(), rt)
    e6 = out[0]["e6_pii"]
    assert any("email -> api.stripe.com" in it for it in e6["items"])


def test_runtime_never_downgrades_or_drops_a_static_edge():
    # runtime is SILENT on api/orders (no obs) → the ⚠ write must survive untouched, not become "safe"
    rt = [{"route": "api/other", "method": "GET", "kind": "db", "target": "x", "n": 5}]
    before = _facts()
    out = merge(before, rt)
    assert out[0]["e3_db_tables"] == before[0]["e3_db_tables"]   # unexercised path: unchanged, kept


def test_min_confirm_floor_filters_noise():
    rt = [{"route": "api/orders", "method": "POST", "kind": "db",
           "target": "order_items", "op": "write", "n": 1}]
    out = merge(_facts(), rt, min_confirm=10)          # 1 trace < floor → ignored
    assert out[0]["e3_db_tables"]["status"] == POTENTIAL   # stayed ⚠, not upgraded on noise
