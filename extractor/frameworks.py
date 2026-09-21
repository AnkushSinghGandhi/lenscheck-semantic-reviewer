"""
frameworks.py — Flask & FastAPI endpoint discovery for the Lenscheck extractor.

Django puts routes in urls.py; Flask and FastAPI put the route on a decorator directly above
the handler function:

    @app.get("/users/{id}")            # FastAPI method shortcut
    @app.route("/users", methods=[…])  # Flask
    @router.post("/pay")               # FastAPI/Flask router or blueprint

This module finds those decorated handlers and hands each function to the SAME behaviour
analysis the Django path uses (`analyzer.build_edges`) — so external-call, DB, cache, async and
PII-taint tracing all come along unchanged, refactor-stable and never running the code. Only
route discovery and auth detection differ per framework, and that is all this file adds.

Honest by construction: a route whose prefix/path we can't fully resolve is still surfaced with
what we have; auth we can't see is reported "?" (unknown), never silently "safe".
"""
from __future__ import annotations
import ast

try:                                    # packaged: `from extractor import frameworks`
    from . import analyzer as A
except (ImportError, ValueError):       # top-level: extractor dir on sys.path
    import analyzer as A

# decorator attributes that mount a route
METHOD_DECOS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
ROUTE_DECOS = {"route", "api_route"}                     # Flask .route / FastAPI .api_route (methods= kwarg)
MOUNT_CALLS = {"include_router", "register_blueprint"}   # carry a prefix / url_prefix

# Flask-style auth decorators (name-based). Extend freely — an unknown one just falls through to "?".
AUTH_DECORATORS = {
    "login_required", "jwt_required", "requires_auth", "auth_required", "token_required",
    "permission_required", "roles_required", "role_required", "requires_scope", "requires",
    "authenticated", "protected", "admin_required", "staff_member_required",
    "fresh_jwt_required", "jwt_required_optional", "login_exempt",
}
DEP_MARKERS = {"Depends", "Security"}                    # FastAPI dependency markers that imply auth


def discover(index):
    """Every Flask/FastAPI route in the repo, as fully-formed Endpoint objects."""
    out = []
    for path in index._iter_py():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        prefixes = _prefix_map(tree)
        consts = index.consts_for(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for methods, route in _routes_of(node, prefixes, consts):
                    out.append(_make_endpoint(node, route, methods, path, index))
    return out


def _routes_of(fn, prefixes, consts):
    """Every (methods, route) a function's decorators declare. Empty for non-handlers."""
    found = []
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        attr = dec.func.attr
        recv = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
        if attr in METHOD_DECOS:
            methods = [attr.upper()]
        elif attr in ROUTE_DECOS:
            methods = _methods_kw(dec) or ["GET"]
        else:
            continue
        if not dec.args:
            continue
        path = A._resolve_route(dec.args[0], consts)
        if not path:
            continue
        found.append((methods, _join(prefixes.get(recv, ""), path)))
    return found


def _make_endpoint(fn, route, methods, file, index):
    rel = A._rel(index, file)
    ep = A.Endpoint(route=route, handler=fn.name, file=rel, line=fn.lineno,
                    methods=[m.upper() for m in methods])
    ep.e1_route_handler = A.Edge(A.VERIFIED, [f"{fn.name} @ {rel}:{fn.lineno}"])
    ep.e2_auth = _auth_edge(fn)
    A.build_edges(ep, [fn], index, file, self_type=None, class_methods={})
    return ep


def _auth_edge(fn):
    """E2 auth for a decorator-routed handler: Flask auth decorators + FastAPI Depends/Security
    (in the route's dependencies=[…] or as a parameter default / Annotated[...])."""
    hits = []
    for dec in fn.decorator_list:
        if _deco_name(dec) in AUTH_DECORATORS:
            hits.append(_deco_name(dec))
        if isinstance(dec, ast.Call):
            deps = _kw_value(dec, "dependencies")
            if isinstance(deps, (ast.List, ast.Tuple)):
                for e in deps.elts:
                    d = _dep_name(e)
                    if d:
                        hits.append(f"Depends({d})")
    hits += _param_deps(fn.args)
    if hits:
        return A.Edge(A.VERIFIED, _dedupe(hits), note="auth decorator / dependency detected")
    return A.Edge(A.UNKNOWN, note="no auth decorator or dependency detected — verify")


# ---- route prefixes (routers / blueprints) -------------------------------------------

def _prefix_map(tree):
    """var -> route prefix, from `x = APIRouter(prefix='..')` / `x = Blueprint(.., url_prefix='..')`
    and `app.include_router(x, prefix='..')` / `app.register_blueprint(x, url_prefix='..')`."""
    pm = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call):
            if _callee_name(n.value.func) in {"APIRouter", "Blueprint"}:
                p = _kw_str(n.value, "prefix") or _kw_str(n.value, "url_prefix")
                if p:
                    pm[n.targets[0].id] = p
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in MOUNT_CALLS and n.args and isinstance(n.args[0], ast.Name):
            p = _kw_str(n, "prefix") or _kw_str(n, "url_prefix")
            if p:
                v = n.args[0].id
                pm[v] = _join(p, pm.get(v, ""))
    return pm


# ---- small ast helpers ----------------------------------------------------------------

def _methods_kw(dec):
    v = _kw_value(dec, "methods")
    if isinstance(v, (ast.List, ast.Tuple, ast.Set)):
        return [A._const_str(e).upper() for e in v.elts if A._const_str(e)]
    return None


def _param_deps(args):
    """FastAPI: `x = Depends(f)` / `x: Annotated[T, Depends(f)]` in the signature → auth."""
    hits = []
    all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    tail = positional[len(positional) - len(defaults):] if defaults else []
    for a, d in zip(tail, defaults):
        n = _dep_name(d)
        if n:
            hits.append(f"{a.arg}=Depends({n})")
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        n = _dep_name(d) if d is not None else None
        if n:
            hits.append(f"{a.arg}=Depends({n})")
    for a in all_args:
        n = _annotated_dep(a.annotation)
        if n:
            hits.append(f"{a.arg}: Depends({n})")
    return hits


def _annotated_dep(ann):
    if isinstance(ann, ast.Subscript) and _callee_name(ann.value) == "Annotated":
        sl = ann.slice
        for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl]):
            n = _dep_name(e)
            if n:
                return n
    return None


def _dep_name(node):
    """The dependency callable inside Depends(x) / Security(x); None if not a dep marker."""
    if isinstance(node, ast.Call) and _callee_name(node.func) in DEP_MARKERS:
        return (_callee_name(node.args[0]) or "?") if node.args else "?"
    return None


def _deco_name(dec):
    return _callee_name(dec.func) if isinstance(dec, ast.Call) else _callee_name(dec)


def _callee_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kw_value(call, name):
    for kw in getattr(call, "keywords", []) or []:
        if kw.arg == name:
            return kw.value
    return None


def _kw_str(call, name):
    return A._const_str(_kw_value(call, name))


def _join(prefix, path):
    prefix, path = (prefix or "").strip(), (path or "").strip()
    if not prefix:
        return path or "/"
    if not path or path == "/":
        return "/" + prefix.strip("/")
    return "/" + prefix.strip("/") + "/" + path.strip("/")


def _dedupe(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
