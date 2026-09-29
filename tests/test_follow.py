"""Interprocedural DB-read tracing: a read reached through a pass-through delegator must be found
(recall), and a call to a nested closure must not bind to a same-named module function elsewhere
(precision). Regression for the example `clients/entity-search` case that missed `colleges`."""

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


def _thing_ep(root):
    for ep in analyze_repo(root):
        if "/thing" in (ep.route or ""):
            return ep
    return None


def _deep_chain_repo(hops_read):
    """A handler → l1 → l2 → … chain where hop i runs `hops_read[i]`. Returns the temp repo dir."""
    d = tempfile.mkdtemp()
    body = ["from fastapi import FastAPI", "app = FastAPI()", "@app.get('/thing')",
            "def handler():", "    return l0()"]
    for i, stmt in enumerate(hops_read):
        nxt = f"    return l{i+1}()" if i + 1 < len(hops_read) else "    return 1"
        body += [f"def l{i}():", f"    {stmt}", nxt]
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write("\n".join(body) + "\n")
    return d


def test_adaptive_depth_reaches_deep_new_facts():
    """5 fact-bearing hops, each reading a DISTINCT model. The old flat depth-3 cap stopped at the
    third — findings-driven follow keeps descending while every hop still reveals something new."""
    root = _deep_chain_repo([f"Model{c}.objects.all()" for c in "ABCDE"])
    models = _models_read(root)
    assert {"ModelA", "ModelB", "ModelC", "ModelD", "ModelE"} <= models, models  # D,E are depth 4,5
    ep = _thing_ep(root)
    assert ep.follow_depth >= 5                    # observability: reports how deep data actually went
    assert ep.follow_stop == "dried_out"           # natural, complete stop (never hit a cap)


def test_adaptive_depth_dries_out_and_still_reports_the_fact():
    """A chain that keeps re-reading the SAME model teaches nothing new past hop 1 → the branch
    brakes (dried_out) instead of walking to the ceiling; the one real read is still reported."""
    root = _deep_chain_repo(["Same.objects.all()"] * 6)
    models = _models_read(root)
    assert "Same" in models
    assert _thing_ep(root).follow_stop == "dried_out"


def test_adaptive_depth_ceiling_is_labelled_not_silently_complete():
    """A genuinely-productive chain deeper than DEPTH_CEILING is cut at the cap and MUST report
    'ceiling' (possibly-incomplete) — never a silent 'dried_out' that would imply completeness."""
    root = _deep_chain_repo([f"M{i}.objects.all()" for i in range(12)])   # 12 distinct models
    ep = _thing_ep(root)
    from analyzer import DEPTH_CEILING
    assert ep.follow_stop == "ceiling"                 # honest blindspot, not a false 'complete'
    assert ep.follow_depth == DEPTH_CEILING            # reached the cap while still learning


def test_adaptive_depth_terminates_on_recursion():
    """Mutual recursion (a→b→a) and self-recursion must not hang or double-count — the `followed`
    set stops a callee being re-entered. Both real reads are still found exactly once."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write("from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/thing')\ndef handler():\n    return a()\n"
                "def a():\n    Ay.objects.all()\n    return b()\n"
                "def b():\n    Bee.objects.all()\n    return a()\n")   # b → a cycle
    models = _models_read(d)
    assert models == {"Ay", "Bee"}                     # both found, walk terminated (no hang)


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


def test_get_object_or_404_reads_real_model():
    """get_object_or_404(Model, …) is a genuine read (Django shortcut runs Model.objects.get)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/thing')\n"
            "def handler(pk):\n"
            "    obj = get_object_or_404(RealModel, pk=pk)\n"
            "    return obj\n"
        )
    assert "RealModel" in _models_read(d)


def test_get_object_or_404_skips_local_model_alias():
    """A runtime model *alias* (`M = apps.get_model(...)` / `M = RealModel`) passed to
    get_object_or_404 must NOT be credited as a table literally named `M` — no such table exists.
    Regression for the example `Alias` phantom found by the gain audit."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/thing')\n"
            "def handler(pk):\n"
            "    Alias = apps.get_model('app', 'Thing')\n"     # a Capitalized *variable*, not a model class
            "    obj = get_object_or_404(Alias, pk=pk)\n"
            "    return obj\n"
        )
    assert "Alias" not in _models_read(d), "credited a phantom table named after a local alias var"
