import os
import sys

import pytest

# make the top-level modules (extractor, diff_pr, enforce, invariants) importable in tests
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _isolate_fact_cache(tmp_path, monkeypatch):
    # keep factindex's cache out of the real ~/.cache during tests (crossrepo now writes there too)
    monkeypatch.setenv("LENSCHECK_INDEX_DIR", str(tmp_path / "lenscheck-index"))

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BLOG = os.path.join(FIXTURES, "blog")


def facts(ep, edge):
    """Fact identities for an edge (strip the `@ file:line` and `(via call)` markers)."""
    e = getattr(ep, edge)
    return {x.split(" @ ")[0].replace(" (via call)", "").strip() for x in (e.items or [])}


def by_route(eps, substr):
    for e in eps:
        if substr in e.route:
            return e
    raise AssertionError(f"no endpoint whose route contains {substr!r}; got {[e.route for e in eps]}")
