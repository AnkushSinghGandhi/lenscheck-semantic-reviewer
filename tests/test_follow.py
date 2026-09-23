"""Interprocedural DB-read tracing: a read reached through a pass-through delegator must be found
(recall), and a call to a nested closure must not bind to a same-named module function elsewhere
(precision). Regression for the cnext `clients/entity-search` case that missed `colleges`."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extractor"))
from analyzer import analyze_repo, to_dict  # noqa: E402


def _models_read(root):
    for ep in analyze_repo(root):
        d = to_dict(ep)
        if "/thing" in (d.get("route") or ""):
            return {i.split(":read")[0].split(":write")[0] for i in (d.get("e3_db_tables") or {}).get("items", [])}
    return set()


def test_delegator_and_closure_collision():
    d = tempfile.mkdtemp()
    # handler → facade (pure delegator, no facts) → worker (reads ModelA) → deep (reads ModelB)
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/thing')\n"
            "def handler():\n"
            "    return facade()\n"
            "def facade():\n"                       # delegator — should be a *free* hop
            "    return worker()\n"
            "def worker():\n"
            "    ModelA.objects.filter(id=1)\n"      # recovered only if the delegator was free
            "    def resolve():\n"                   # a LOCAL closure named like the global below
            "        return 1\n"
            "    resolve()\n"                        # must NOT bind to other.resolve()
            "    return deep()\n"
            "def deep():\n"
            "    ModelB.objects.all()\n"
        )
    # a same-named global in another module that reads a table we must NOT attribute to /thing
    with open(os.path.join(d, "other.py"), "w") as f:
        f.write("def resolve():\n    ModelC.objects.all()\n")

    models = _models_read(d)
    assert "ModelA" in models, f"delegator hop lost the read: {models}"   # recall (A)
    assert "ModelB" in models, f"deep read lost: {models}"                # recall (A, depth)
    assert "ModelC" not in models, f"closure bound to global resolve(): {models}"  # precision (B)


def test_same_name_facade_is_followed():
    """A facade method `Helper.load()` that delegates to a module `load()` of the *same* name must
    still be followed — the root fn's own name is not 'local' (regression: cm-directory went empty)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/thing')\n"
            "def handler():\n"
            "    return Helper.load()\n"
            "class Helper:\n"
            "    @staticmethod\n"
            "    def load():\n"
            "        return load()\n"                  # same-name delegation to the module fn below
            "def load():\n"
            "    ModelD.objects.all()\n"
        )
    models = _models_read(d)
    assert "ModelD" in models, f"same-name facade dropped: {models}"
