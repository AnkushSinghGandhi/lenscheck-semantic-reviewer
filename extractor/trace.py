"""Ordered, guarded trace of one entrypoint.

`trace(ep, index)` re-walks a handler with a `SeqCollector` — a `FactCollector` that also remembers the
`if`/`for`/`try` conditions in force and the order facts were found — then expands the analyzer's own
callee targets into a call tree. Facts still come only from `FactCollector`; call resolution still comes
only from `_callee_targets`. Pure stdlib, deterministic, no LLM.
"""
from __future__ import annotations

import ast

try:                                            # packaged: `from extractor.trace import trace`
    from . import analyzer as A
    from .analyzer import (FactCollector, _frame_collector, _callee_targets, DEPTH_CEILING,
                           FOLLOW_BUDGET, _TASK_DECOS, _deco_names)
except (ImportError, ValueError):               # top-level: extractor dir on sys.path
    import analyzer as A
    from analyzer import (FactCollector, _frame_collector, _callee_targets, DEPTH_CEILING,
                          FOLLOW_BUDGET, _TASK_DECOS, _deco_names)

_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith,
             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
if hasattr(ast, "Match"):
    _COMPOUND += (ast.Match,)
if hasattr(ast, "TryStar"):
    _COMPOUND += (ast.TryStar,)

_FACT_LISTS = ("db", "external", "async_", "cache", "self_calls", "func_calls",
               "typed_calls", "manager_calls")


def _short(text, limit):
    return text if len(text) <= limit else text[:limit] + "…"


def _const_value(node):
    """A literal column value; a list/tuple of literals; else "?"."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in node.elts):
        return [e.value for e in node.elts]
    return "?"


class SeqCollector(FactCollector):
    """FactCollector + ordered events + the guard stack in force at each statement."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guards = []
        self.events = []

    # ---- statement dispatch ----------------------------------------------------------
    def visit(self, node):
        if isinstance(node, ast.stmt) and not isinstance(node, _COMPOUND):
            self._visit_simple(node)
        elif isinstance(node, ast.If):
            self._visit_if(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._visit_for(node)
        elif isinstance(node, ast.While):
            self._visit_header(node.test)
            self._under(f"while {ast.unparse(node.test)}", node.body)
            for s in node.orelse:
                self.visit(s)
        elif isinstance(node, ast.Try) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
            self._visit_try(node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            self._visit_with(node)
        elif hasattr(ast, "Match") and isinstance(node, ast.Match):
            self._visit_header(node.subject)
            for case in node.cases:
                for s in case.body:
                    self.visit(s)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            super().visit(node)             # root fn param typing must still run
        else:
            super().visit(node)

    def _visit_simple(self, node):
        before = tuple(len(getattr(self, name)) for name in _FACT_LISTS)
        super().visit(node)
        self._emit_since(before, node)

    def _visit_header(self, expr):
        """Facts read by a compound statement's own condition/iterable, under the current guards."""
        if expr is None:
            return
        before = tuple(len(getattr(self, name)) for name in _FACT_LISTS)
        self.visit(expr)
        self._emit_since(before, expr)

    def _under(self, guard, body):
        self.guards.append(_short(guard, 80))
        try:
            for s in body:
                self.visit(s)
        finally:
            self.guards.pop()

    def _visit_if(self, node):
        self._visit_header(node.test)
        test = ast.unparse(node.test)
        self._under(f"if {test}", node.body)
        if node.orelse:
            self._under(f"else (not: {test})", node.orelse)

    def _visit_for(self, node):
        if isinstance(node, ast.For):
            self._bind_loop_var(node)
        self._visit_header(node.iter)
        self._under(f"for each {ast.unparse(node.target)} in {ast.unparse(node.iter)}", node.body)
        for s in node.orelse:
            self.visit(s)

    def _visit_try(self, node):
        for s in node.body:
            self.visit(s)
        for h in node.handlers:
            guard = "on any error" if h.type is None else f"on {ast.unparse(h.type)}"
            self._under(guard, h.body)
        for s in node.orelse:
            self.visit(s)
        self._under("finally", node.finalbody)

    def _visit_with(self, node):
        atomic = False
        for item in node.items:
            self._visit_header(item.context_expr)
            try:
                atomic = atomic or "atomic" in ast.unparse(item.context_expr)
            except Exception:
                pass
        if atomic:
            self._under("atomic", node.body)
        else:
            for s in node.body:
                self.visit(s)

    # ---- events ----------------------------------------------------------------------
    def _emit_since(self, before, node):
        guards = list(self.guards)
        loc = f"{self.file}:{getattr(node, 'lineno', 0)}"
        lits = self._literals(node)
        idx = dict(zip(_FACT_LISTS, before))

        seen_db = set()
        for model, kind, f, line in self.db[idx["db"]:]:
            kind = kind.rstrip("*")
            kind = "write" if kind == "write" else "read"
            if (model, kind, f, line) in seen_db:      # a chained queryset names the same read twice
                continue
            seen_db.add((model, kind, f, line))
            ev = {"kind": kind, "model": model, "text": model,
                  "loc": f"{f}:{line}", "guards": guards}
            lit = lits.get(line)
            if lit:
                if lit.get("where"):
                    ev["where"] = lit["where"]
                if lit.get("where_not"):
                    ev["where_not"] = lit["where_not"]
                if lit.get("set"):
                    ev["set"] = lit["set"]
            self.events.append(ev)

        for root, dest, f, line in self.external[idx["external"]:]:
            self.events.append({"kind": "external", "text": f"{root} -> {dest}", "target": dest,
                                "loc": f"{f}:{line}", "guards": guards})

        for mech, target, f, line in self.async_[idx["async_"]:]:
            self.events.append({"kind": "async", "text": f"{mech} {target}", "target": target,
                                "mech": mech, "loc": f"{f}:{line}", "guards": guards})

        for k, detail, f, line in self.cache[idx["cache"]:]:
            self.events.append({"kind": "cache", "text": f"{k} {detail}",
                                "loc": f"{f}:{line}", "guards": guards})

        keys = set()
        keys.update(("self", m) for m in self.self_calls[idx["self_calls"]:])
        keys.update(("func", m) for m in self.func_calls[idx["func_calls"]:])
        keys.update(("typed", c, m) for c, m in self.typed_calls[idx["typed_calls"]:])
        keys.update(("manager", c, m) for c, m in self.manager_calls[idx["manager_calls"]:])
        if keys:
            self.events.append({"kind": "call", "_keys": keys, "loc": loc, "guards": guards})

        if isinstance(node, ast.Return):
            text = "return" + (" " + ast.unparse(node.value) if node.value is not None else "")
        elif isinstance(node, ast.Raise):
            text = "raise" + (" " + ast.unparse(node.exc) if node.exc is not None else "")
        elif isinstance(node, ast.Continue):
            text = "skip item"
        elif isinstance(node, ast.Break):
            text = "break"
        else:
            return
        self.events.append({"kind": "exit", "text": _short(text, 60), "loc": loc, "guards": guards})

    def _literals(self, node):
        """{line: {"where": {...}, "where_not": {...}, "set": {...}}} for literal ORM kwargs on that line."""
        out = {}
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            meth = call.func.attr
            if meth not in {"filter", "exclude", "get", "create", "update",
                            "get_or_create", "update_or_create", "bulk_create"}:
                continue
            slot = out.setdefault(getattr(call, "lineno", 0),
                                  {"where": {}, "where_not": {}, "set": {}})
            if meth == "bulk_create":
                arg = call.args[0] if call.args else None
                if arg is not None:
                    for inner in ast.walk(arg):
                        if not isinstance(inner, ast.Call):
                            continue
                        fn = inner.func
                        name = fn.id if isinstance(fn, ast.Name) else \
                            (fn.attr if isinstance(fn, ast.Attribute) else "")
                        if not name or not name[0].isupper():
                            continue
                        for kw in inner.keywords:
                            if kw.arg is None:
                                continue
                            slot["set"][kw.arg] = _const_value(kw.value)
                continue
            for kw in call.keywords:
                if kw.arg is None:                     # `**spread` — that key is simply absent
                    continue
                if meth in {"filter", "get"}:
                    slot["where"][kw.arg] = _const_value(kw.value)
                elif meth == "exclude":
                    slot["where_not"][kw.arg] = _const_value(kw.value)
                elif meth in {"create", "update"}:
                    slot["set"][kw.arg] = _const_value(kw.value)
                elif kw.arg == "defaults":
                    if isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                slot["set"][k.value] = _const_value(v)
                else:
                    slot["where"][kw.arg] = _const_value(kw.value)
        return out


# ---- trace --------------------------------------------------------------------------

def _display(key):
    base = key.split("|", 1)[0]
    if base.startswith("self:"):
        return f"self.{base.rsplit(':', 1)[-1]}()"
    if base.startswith("func:"):
        return f"{base[len('func:'):]}()"
    if base.startswith("manager:"):
        c, _, m = base[len("manager:"):].rpartition(".")
        return f"{c}.objects.{m}()"
    c, _, m = base.rpartition(".")
    return f"{c}.{m}()" if c else f"{base}()"


def _celery_task(index, ev, owner_file):
    mech, target = ev.get("mech", ""), ev.get("target", "") or ""
    if not mech.startswith("celery."):
        return None
    name = target.split(".")[-1]
    if not name:
        return None
    node, file = index.find_func(name, near=owner_file, toplevel_only=True)
    if node is None or not (_deco_names(node) & _TASK_DECOS):
        return None
    return node, file


def trace(ep, index, keep_all=False):
    """Ordered steps one entrypoint runs, with guards, DB reads/writes, calls and early exits."""
    walk = ep.lc_walk
    ctx = {"next": 1, "expanded": {}, "cut": set(), "stats": {"calls_followed": 0, "truncated": False}}

    def capped(depth):
        if depth + 1 > DEPTH_CEILING:
            ctx["stats"]["truncated"] = True
            return True
        if ctx["stats"]["calls_followed"] >= FOLLOW_BUDGET:
            ctx["stats"]["truncated"] = True
            return True
        return False

    def new_step(step):
        step["id"] = f"s{ctx['next']}"
        ctx["next"] += 1
        return step

    def expand(step, key, depth, run):
        """First sighting is expanded, later ones ref it — unless that first one was cut by a cap,
        so a call reached deep first and shallow later still shows its body."""
        first = ctx["expanded"].get(key)
        if first is not None and first not in ctx["cut"]:
            step["ref"] = first
        elif capped(depth):
            step["truncated"] = True
            ctx["cut"].add(step["id"])
            ctx["expanded"].setdefault(key, step["id"])
        else:
            ctx["expanded"][key] = step["id"]        # before descending, so recursion becomes a ref
            ctx["stats"]["calls_followed"] += 1
            step["children"] = run()

    def trace_fn(fn, owner_file, classmethods, self_type, bindings, seed_types, manager_model, depth):
        col = _frame_collector(index, owner_file, self_type, seed_types, bindings, manager_model,
                               cls=SeqCollector)
        col.visit(fn)
        out = []
        for ev in col.events:
            if ev["kind"] != "call":
                if ev["kind"] == "async":
                    task = _celery_task(index, ev, owner_file)
                    if task is not None:
                        node, file = task
                        step = new_step({**ev, "boundary": "celery", "children": []})
                        expand(step, ("celery", node.name), depth,
                               lambda: trace_fn(node, file, {}, None, None, None, None, depth + 1))
                        out.append(_public(step))
                        continue
                out.append(_public(new_step({**ev, "children": []})))
                continue

            for t in _callee_targets(col, index, owner_file, classmethods, bindings, only=ev["_keys"]):
                step = new_step({"kind": "call", "text": _display(t.key), "target": t.key,
                                 "guards": ev["guards"], "loc": ev["loc"], "children": []})
                expand(step, t.key, depth,
                       lambda t=t: trace_fn(t.fn, t.file, t.classmethods, t.self_type,
                                            t.bindings, t.seed_types, t.manager_model, depth + 1))
                out.append(_public(step))
        return out

    steps = []
    for m in walk["method_nodes"]:
        steps.extend(trace_fn(m, walk["owner_file"], walk["class_methods"], walk["self_type"],
                              walk["bindings"], None, None, 0))
    if not keep_all:
        steps = _prune(steps)
    return {"steps": steps, "stats": ctx["stats"]}


def _public(step):
    """Copy without the internal `_keys`, children normalised."""
    return {k: v for k, v in step.items() if k != "_keys"}


def _iter_all(steps):
    for step in steps:
        yield step
        yield from _iter_all(step.get("children") or [])


def _prune(steps):
    """Keep facts, guarded exits, calls with a kept descendant, and refs to kept steps."""
    all_steps = list(_iter_all(steps))
    keep = {}
    calls = []
    for step in all_steps:
        if step["kind"] in ("read", "write", "external", "async", "cache"):
            keep[step["id"]] = True
        elif step["kind"] == "exit":
            keep[step["id"]] = bool(step.get("guards"))
        elif step["kind"] == "call":
            keep[step["id"]] = bool(step.get("truncated"))     # a cap cut is never hidden
            calls.append(step)
    changed = True
    while changed:                       # fixed point: recursion refs can depend on an ancestor
        changed = False
        for step in calls:
            if step.get("truncated"):
                value = True
            elif "ref" in step:
                value = bool(keep.get(step["ref"], False))
            else:
                value = any(bool(keep.get(c["id"], False)) for c in step.get("children") or [])
            if keep[step["id"]] != value:
                keep[step["id"]] = value
                changed = True

    def prune(items):
        out = []
        for step in items:
            if not keep.get(step["id"], False):
                continue
            if "children" in step:
                step["children"] = prune(step.get("children") or [])
            out.append(step)
        return out

    return prune(steps)
