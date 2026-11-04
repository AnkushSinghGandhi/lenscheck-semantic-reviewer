"""
Milestone 1 extractor — a crude static pass over a Django repo for the lens:

    HTTP endpoint -> auth -> DB tables -> external calls -> async -> PII egress

Pure stdlib `ast`. No imports of the target app (safe on any checkout). Emits per-endpoint
facts with an explicit resolution status per edge:  ✓ resolved / ⚠ potential / ? unknown.

The design principle baked in here: never render "not observed" as "doesn't exist".
Anything we cannot resolve is surfaced as ⚠ or ? and linked to source, never dropped.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# ---- config --------------------------------------------------------------------------

SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", "migrations",
             "site-packages", "tests", "test"}

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# outbound-call roots we recognise; value = how to label the destination
EXTERNAL_ROOTS = {"requests", "httpx", "urllib", "urllib2", "aiohttp",
                  "stripe", "razorpay", "boto3", "sendgrid", "smtplib"}
EXTERNAL_VERBS = {"get", "post", "put", "patch", "delete", "request", "send", "post_async"}

# Django field lookups — the token after `field__` when it's a WHERE operator, not an FK traversal.
# `status__in` / `target_year__gt` / `removed_at__isnull` filter the LOCAL column (no join, no related
# read); only `fk__relatedcol` joins a table. Used to stop the FK-traversal heuristic hallucinating a
# read of a model that merely shares a name with a filtered field's stem (Status/EntityType/…).
_DJANGO_LOOKUPS = {
    "exact", "iexact", "contains", "icontains", "in", "gt", "gte", "lt", "lte", "startswith",
    "istartswith", "endswith", "iendswith", "range", "date", "year", "iso_year", "month", "day",
    "week", "week_day", "iso_week_day", "quarter", "time", "hour", "minute", "second", "isnull",
    "regex", "iregex", "search", "overlap", "contained_by", "has_key", "has_keys", "has_any_keys",
    "len", "trigram_similar", "unaccent", "isempty",
    "id", "pk",   # `fk__id`/`fk__pk` resolve to the LOCAL fk column — no join to the related table
}

# ORM read vs write method names
ORM_READ = {"get", "filter", "all", "values", "values_list", "count", "exists",
            "first", "last", "aggregate", "annotate", "none", "get_or_none"}
ORM_WRITE = {"create", "update", "save", "delete", "bulk_create", "bulk_update",
             "get_or_create", "update_or_create", "insert", "add", "set", "remove"}

# receivers where a bare .save()/.delete() is NOT a DB write — cache, session, file/blob storage.
# Excluding these stops `cache.delete(key)` etc. from being mislabeled a table write (`<instance>`).
NON_ORM_RECEIVERS = {"cache", "caches", "session", "storage", "default_storage", "fs"}

# class-name suffixes that are NOT ORM tables (a `serializer.save()` writes to its Meta.model, not a
# table named "FooSerializer"). We resolve these to Meta.model when known, else fall back to unknown.
NON_MODEL_SUFFIXES = ("Serializer", "Form")

# Django cache ops — surfaced as the E7 cache lens (read vs mutate/invalidate), not DB writes.
CACHE_READ = {"get", "get_many", "has_key", "keys", "ttl"}
CACHE_WRITE = {"set", "set_many", "add", "delete", "delete_many", "clear",
               "incr", "decr", "touch", "get_or_set"}

# sensitive attribute names for the PII edge
SENSITIVE = {"email", "phone", "mobile", "password", "ssn", "aadhaar", "aadhar",
             "pan", "card", "card_number", "address", "dob", "date_of_birth",
             "contact", "contact_number", "user_email", "billing_email"}

# taint sinks (feature #9): where a tainted value "leaves". Egress to a third party / log / client.
LOG_ROOTS = {"logger", "logging", "log", "LOG", "logfire"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
RESPONSE_CALLS = {"Response", "JsonResponse", "HttpResponse", "HttpResponseBadRequest"}
# calls we propagate taint *through* (they serialize/wrap, they don't sanitize)
TAINT_WRAPPERS = {"dumps", "dump", "str", "format", "dict", "list", "tuple", "join"}

VERIFIED, POTENTIAL, UNKNOWN, NA = "✓", "⚠", "?", "n/a"


# ---- fact model ----------------------------------------------------------------------

@dataclass
class Edge:
    status: str                       # ✓ / ⚠ / ? / n/a
    items: list = field(default_factory=list)   # human-readable resolved facts
    note: str = ""


@dataclass
class Endpoint:
    route: str
    handler: str
    file: str = ""
    line: int = 0
    methods: list = field(default_factory=list)
    e1_route_handler: Edge = None
    e2_auth: Edge = None
    e3_db_tables: Edge = None
    e4_external: Edge = None
    e5_async: Edge = None
    e6_pii: Edge = None
    e7_cache: Edge = None
    tables: dict = field(default_factory=dict)   # model -> {"table": name, "explicit": bool}
    follow_depth: int = 0     # deepest fact-bearing hop the adaptive follow actually reached
    follow_stop: str = ""     # why it stopped: 'dried_out' (complete) / 'ceiling' / 'budget' (⚠ maybe more)
    kind: str = "http"        # what triggers it: http | task (celery) | command (manage.py) | signal (Django)
    writes_cols: list = field(default_factory=list)   # [{model, cols (list|None=unknown), loc}] per ORM write


# ---- repo index ----------------------------------------------------------------------

class RepoIndex:
    """Parse every .py once; index class + function defs by name, and url path() calls."""

    def __init__(self, root: str):
        self.root = root
        self.classes: dict[str, list] = {}     # name -> [(node, file)]
        self.funcs: dict[str, list] = {}       # name -> [(node, file, toplevel)]
        self.url_files: list[str] = []
        self.drf_present = False               # REST_FRAMEWORK settings seen
        self.default_permissions = None        # DEFAULT_PERMISSION_CLASSES (list) or None
        self.module_consts: dict[str, dict] = {}   # file -> {NAME: folded string}
        self.global_consts: dict[str, str] = {}    # repo-wide URL constants (unambiguous only)
        self.class_models: dict[str, str] = {}     # Serializer/Form/etc. name -> its Meta.model table
        self.perm_consts: dict[str, list] = {}     # PERMISSION_CONSTANT -> [class names] (unambiguous)
        self.model_tables: dict[str, str] = {}     # Model -> explicit Meta.db_table (SQL table name)
        self.model_managers: dict[str, str] = {}   # Model -> its custom Manager class (from `objects = X()`)
        self.module_imports: dict[str, dict] = {}  # file -> {alias: stem} for relative module imports
        self.signal_connects: list = []            # (handler, signal, sender) from `post_save.connect(h, sender=M)`
        self.instance_consts: dict[str, list] = {}  # NAME -> [(Call, file)] for a module-level `NAME = Cls(...)`
        self._build()

    def _iter_py(self):
        # sorted traversal (dirs + files) so the index is filesystem-order-independent: on a same-name
        # helper/class collision `_pick` breaks proximity ties on the first-indexed candidate, so an
        # unsorted os.walk would resolve the name differently across machines/CI checkouts.
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)

    def _build(self):
        seen, ambiguous = {}, set()        # for the repo-wide string-constant table
        pseen, pamb = {}, set()            # for the repo-wide permission-list constant table
        for path in self._iter_py():
            try:
                src = open(path, encoding="utf-8").read()
                tree = ast.parse(src)
            except (SyntaxError, UnicodeDecodeError):
                continue
            if os.path.basename(path) == "urls.py" or os.sep + "urls" + os.sep in path:
                self.url_files.append(path)
            if "settings" in path:
                self._scan_drf(tree)
            mc = _module_str_consts(tree)              # this module's top-level string constants
            self.module_consts[path] = mc
            for name, val in mc.items():               # merge into the repo-wide table
                if name in seen and seen[name] != val:
                    ambiguous.add(name)                # same name, different value across files → drop
                seen[name] = val
            for name, lst in _module_perm_consts(tree).items():   # permission-list constants
                if name in pseen and pseen[name] != lst:
                    pamb.add(name)
                pseen[name] = lst
            self._index_defs(tree, path)
            for n in tree.body:                        # config singletons: `SPEC_X = QueueSpec(model=M, …)`
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                        and _ctor_name(n.value):
                    self.instance_consts.setdefault(n.targets[0].id, []).append((n.value, path))
            # collect relative module imports: `from . import rows` / `from .rows import fn`
            # so `rows.fn()` typed calls can be resolved as module-level functions in rows.py
            file_dir = os.path.dirname(path)
            mod_imports = {}
            # `ast.walk`, not `iter_child_nodes`: the import can be *function-local* (a view that does
            # `from . import interactions` inside its `get()` to dodge a cycle), and that method-body
            # import is exactly the one whose `interactions.build_response(...)` we need to follow.
            for node in ast.walk(tree):
                sig = _signal_connect(node)                 # `post_save.connect(handler, sender=Order)`
                if sig:
                    self.signal_connects.append(sig)
                    continue
                if not isinstance(node, ast.ImportFrom):
                    continue
                if not node.level:
                    # `from pkg.sub import mod` — a repo module imported absolutely; `mod.fn()` follows like a
                    # relative one. shortcut: resolved against the repo root only (not a src/ layout or sys.path)
                    for alias in node.names:
                        f = os.path.join(self.root, *(node.module or "").split("."), alias.name + ".py")
                        if node.module and os.path.isfile(f):
                            mod_imports[alias.asname or alias.name] = f
                    continue
                if node.module is None:
                    # `from . import rows, tiles` — each alias IS the module stem
                    for alias in node.names:
                        stem = alias.asname or alias.name
                        mod_imports[stem] = os.path.join(file_dir, alias.name + ".py")
                # `from .rows import list_entities` — module is the stem, names are members (not modules)
                # we don't need to handle that: it imports names, not the module itself
            if mod_imports:
                self.module_imports[path] = mod_imports
        self.global_consts = {n: v for n, v in seen.items() if n not in ambiguous}
        self.perm_consts = {n: v for n, v in pseen.items() if n not in pamb}

    def _index_defs(self, node, path, toplevel=True):
        """Index ClassDefs and FunctionDefs, recursing through the module, class bodies, and
        control-flow (if/try/with) — but NOT into function bodies. Handlers and the helpers we
        follow are module-level or class methods, never nested inside a function, so pruning
        function bodies (the bulk of the AST) skips ~all the cost with no loss.

        Each `self.funcs` entry carries a `toplevel` flag (module-level def vs class method). Both
        kinds are indexed — a classmethod can be a view (`path(..., Helper.search)` resolves by
        name) — but the *bare-call follow* path filters to toplevel only: a bare `foo()` never
        resolves to a class method in Python (needs `self.`/`Cls.`), so a same-named method must
        not shadow the real module helper (e.g. `Helper.load()` -> module `load()` was resolving
        back to itself and losing the module read)."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                self.classes.setdefault(child.name, []).append((child, path))
                m = _class_meta_model(child)                  # DRF/Django `class Meta: model = X`
                if m:
                    self.class_models[child.name] = m
                t = _class_db_table(child) or _class_tablename(child)   # Django Meta.db_table / SQLAlchemy __tablename__
                if t:
                    self.model_tables[child.name] = t
                mgr = _class_manager(child)                   # `objects = OrderManager()` → custom manager
                if mgr:
                    self.model_managers[child.name] = mgr
                self._index_defs(child, path, toplevel=False)  # methods live under the class, not the module
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(child.name, []).append((child, path, toplevel))
                # prune: a function body holds no module-level handlers/helpers to index
            else:
                self._index_defs(child, path, toplevel=toplevel)   # if/try/with/… wrap defs, same level

    def consts_for(self, file: str) -> dict:
        """String constants visible in `file`: the repo-wide table, with this module's own on top."""
        return {**self.global_consts, **self.module_consts.get(file, {})}

    def table_for(self, model: str):
        """(table, explicit) for a model: its declared `Meta.db_table` if present (explicit=True),
        else Django's default `{app_label}_{model_lower}` from where the class lives (explicit=False),
        or (None, False) if the model isn't in the repo."""
        if model in self.model_tables:
            return self.model_tables[model], True
        defs = self.classes.get(model)
        if defs:
            app = _app_label(defs[0][1])
            if app:
                return f"{app}_{model.lower()}", False
        return None, False

    def reverse_relation(self, model, accessor):
        """(source model, kind) for a reverse accessor on `model`: `Credential.user = FK(User,
        related_name="credentials")` makes `user.credentials` a manager of Credential; without a `related_name`
        a ForeignKey/ManyToMany gives `<source lower>_set` and a OneToOne gives `<source lower>`.
        kind is "fk", "m2m" or "o2o"."""
        rev = self.__dict__.get("_reverse_rels")
        if rev is None:
            rev = self.__dict__["_reverse_rels"] = {}
            kinds = {"ForeignKey": "fk", "OneToOneField": "o2o", "ManyToManyField": "m2m"}
            for src, defs in self.classes.items():
                for node, _path in defs[:1]:
                    for st in getattr(node, "body", []):
                        if not (isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and st.value.args):
                            continue
                        kind = kinds.get(_dotted(st.value.func).split(".")[-1])
                        target = _class_ref(st.value.args[0]) if kind else None
                        if not target:
                            continue
                        target = src if target == "self" else target
                        name = next((k.value.value for k in st.value.keywords if k.arg == "related_name"
                                     and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)), None)
                        if name is None:
                            name = src.lower() if kind == "o2o" else f"{src.lower()}_set"
                        rev.setdefault((target, name), (src, kind))   # related_name="+" registers a name nothing can match
        return rev.get((model, accessor))

    def property_def(self, cls_name, attr):
        """The `@property` / `@cached_property` def `attr` on repo class `cls_name` (own or inherited), else None."""
        cache = self.__dict__.setdefault("_prop_defs", {})
        key = (cls_name, attr)
        if key not in cache:
            fn = _inherited_methods(self, cls_name).get(attr)
            cache[key] = fn if fn is not None and _deco_names(fn) & {"property", "cached_property"} else None
        return cache[key]

    def class_attr_models(self, cls_name):
        """{attr: model} for class-body annotations of a repo class (`profile: Profile`): a request or
        context class that declares which models hang off it."""
        cache = self.__dict__.setdefault("_attr_models", {})
        if cls_name not in cache:
            out = {}
            node, _ = self.find_class(cls_name)
            for st in getattr(node, "body", []):
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    am = _anno_model(st.annotation)
                    if am and _is_model(self, am):
                        out[st.target.id] = am
            cache[cls_name] = out
        return cache[cls_name]

    def model_field(self, model, field):
        """(kind, target, through) for a relation field declared on a repo model: kind "fk"
        (ForeignKey / OneToOneField) or "m2m" (ManyToManyField), target = the related class name
        ("self" resolved to `model`), through = an explicit M2M `through=` model or None. None when
        `field` isn't a relation field this can read."""
        return self.relation_fields(model).get(field)

    def relation_fields(self, model):
        """{field: (kind, target, through)} for the relation fields declared on `model` (see model_field)."""
        cache = self.__dict__.setdefault("_rel_fields", {})
        if model not in cache:
            out = {}
            for node, _path in (self.classes.get(model) or [])[:1]:
                for st in getattr(node, "body", []):
                    if not (isinstance(st, ast.Assign) and len(st.targets) == 1
                            and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call)):
                        continue
                    kind = {"ForeignKey": "fk", "OneToOneField": "fk", "ManyToManyField": "m2m"}.get(
                        _dotted(st.value.func).split(".")[-1])
                    if not kind or not st.value.args:
                        continue
                    tgt = _class_ref(st.value.args[0])
                    thr = next((_class_ref(k.value) for k in st.value.keywords if k.arg == "through"), None)
                    if tgt:
                        out[st.targets[0].id] = (kind, model if tgt == "self" else tgt, thr)
            cache[model] = out
        return cache[model]

    def _scan_drf(self, tree):
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "REST_FRAMEWORK" for t in n.targets) \
                    and isinstance(n.value, ast.Dict):
                self.drf_present = True
                for k, v in zip(n.value.keys, n.value.values):
                    if _const_str(k) == "DEFAULT_PERMISSION_CLASSES" and isinstance(v, (ast.List, ast.Tuple)):
                        self.default_permissions = [
                            (_const_str(e) or _dotted(e) or "").split(".")[-1] for e in v.elts]

    def find_class(self, name: str, near: str = ""):
        return _pick(self.classes.get(name), near)

    def find_func(self, name: str, near: str = "", toplevel_only: bool = False):
        """Resolve a function by name. `toplevel_only=True` (the bare-call follow path) drops class
        methods so a bare `foo()` binds only to a module-level `foo`, never a same-named method."""
        cands = self.funcs.get(name)
        if cands:
            cands = [(n, p) for (n, p, tl) in cands if tl or not toplevel_only]
        return _pick(cands, near)

    def imports_for(self, path: str) -> dict:
        """Return {alias: module_file} for relative module imports in `path` (e.g. `from . import rows`).
        Used to resolve `module.fn()` typed calls where the callee is a sibling module, not a class."""
        return self.module_imports.get(path, {})

    def instance_const(self, name: str, near: str = ""):
        """(Cls, {field: model}) of a module-level `NAME = Cls(...)` config object, else None."""
        call, _ = _pick(self.instance_consts.get(name), near)
        if call is None:
            return None
        memo = self.__dict__.setdefault("_instance_const_memo", {})
        if id(call) not in memo:
            memo[id(call)] = _ctor_binding(self, call)
        return memo[id(call)]


def _pick(candidates, near: str):
    """Disambiguate a name collision by preferring the def defined nearest `near`."""
    if not candidates:
        return (None, None)
    if len(candidates) == 1 or not near:
        return candidates[0]
    near_parts = near.split(os.sep)

    def common(path):
        p = path.split(os.sep)
        n = 0
        for a, b in zip(near_parts, p):
            if a != b:
                break
            n += 1
        return n
    return max(candidates, key=lambda c: common(c[1]))


# ---- url parsing ---------------------------------------------------------------------

def _const_str(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_route(node, str_consts: dict) -> str:
    """Handle plain strings and f-strings like f'{prefix}/submit'."""
    s = _const_str(node)
    if s is not None:
        return s
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                out.append(str_consts.get(part.value.id, "{" + part.value.id + "}"))
            else:
                out.append("{?}")
        return "".join(out)
    return "?"


def _handler_name(node) -> Optional[str]:
    """From `X.as_view()` or `mod.Cls.as_view()` or bare `view` return the class/func name."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "as_view":
            base = f.value
            if isinstance(base, ast.Attribute):
                return base.attr
            if isinstance(base, ast.Name):
                return base.id
    if isinstance(node, ast.Name):          # bare function view
        return node.id
    if isinstance(node, ast.Attribute):     # mod.view
        return node.attr
    return None


def parse_endpoints(index: RepoIndex) -> list:
    endpoints = []
    for path in index.url_files:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        # module-level string consts (for f-string route prefixes)
        str_consts = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                s = _const_str(n.value)
                if s is not None:
                    str_consts[n.targets[0].id] = s
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                fname = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if fname not in {"path", "re_path", "url"} or len(node.args) < 2:
                    continue
                route = _resolve_route(node.args[0], str_consts)
                handler = _handler_name(node.args[1])
                if handler:
                    endpoints.append((route, handler, path))
    # dedupe, preserve order; the urls file rides along to disambiguate same-named views
    seen, out = set(), []
    for r, h, u in endpoints:
        if (r, h) not in seen:
            seen.add((r, h))
            out.append((r, h, u))
    return out


def _strip_anchors(p):
    """`^api/` / `leads/$` → `api/` / `leads/`. Regex anchors only belong at a pattern's edges, so they must
    go BEFORE pieces are concatenated — else `api/^leads/` turns into a literal `^leads` segment."""
    return (p or "").lstrip("^").rstrip("$")


def _unresolved(p):
    """A route piece `_resolve_route` couldn't fold: the whole thing ("?", e.g. a
    `'{}'.format(settings.URL_PREFIX)` mount) or an f-string placeholder (`{NAME}` / `{?}`). A regex
    quantifier (`{4}`, `{2,3}`) is not one."""
    return p == "?" or bool(re.search(r"\{(?![\d,]+\})[^}]*\}", p or ""))


def full_routes(index: RepoIndex) -> dict:
    """Each URLconf endpoint's FULL route, built by chaining `path(prefix, include('dotted.urls'))` from the
    root down to the leaf. `ep.route` deliberately stays the local leaf pattern — it's an identity key
    across the tool (baselines, runtime joins) — so this is a separate map, used only where a full path
    must match (cross-repo API links). Returns {(leaf_route, handler): [full_route, ...]}: a module
    included under two prefixes yields two. Dotted-string includes only; router / inline-list / tuple
    includes are skipped, and a prefix we can't fold (a settings-driven mount) yields an EMPTY list — in
    both cases the endpoint simply keeps its leaf route."""
    def dotted(p):
        rel = os.path.relpath(p, index.root)[:-3]                      # drop ".py"
        if rel.endswith(os.sep + "__init__"):                          # `urls/` package
            rel = rel[: -len(os.sep + "__init__")]
        return rel.replace(os.sep, ".")
    mods = {p: dotted(p) for p in index.url_files}

    def resolve(mod):
        hits = [p for p, d in mods.items() if d == mod or d.endswith("." + mod)]
        return hits[0] if len(hits) == 1 else None                      # unknown / ambiguous → unresolved

    leaves, parents = {}, {}
    for path in index.url_files:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        str_consts = {}                                                 # mirrors parse_endpoints → keys match
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                s = _const_str(n.value)
                if s is not None:
                    str_consts[n.targets[0].id] = s
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))):
                continue
            fname = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if fname not in {"path", "re_path", "url"} or len(node.args) < 2:
                continue
            route, tgt = _resolve_route(node.args[0], str_consts), node.args[1]
            inc = tgt.func if isinstance(tgt, ast.Call) else None
            if inc is not None and (getattr(inc, "id", None) or getattr(inc, "attr", None)) == "include":
                mod = _const_str(tgt.args[0]) if tgt.args else None
                child = resolve(mod) if mod else None
                if child and child != path:
                    parents.setdefault(child, []).append((path, route))
                continue
            handler = _handler_name(tgt)
            if handler:
                leaves.setdefault(path, []).append((route, handler))

    memo = {}

    def prefixes(f, stack=()):
        """Every root→f prefix; None marks a chain with a piece we couldn't fold."""
        if f in memo:
            return memo[f]
        if f in stack:                                                  # include cycle (invalid Django)
            return [None]
        out = [None if (pp is None or _unresolved(p)) else pp + _strip_anchors(p)
               for parent, p in parents.get(f, []) for pp in prefixes(parent, stack + (f,))]
        memo[f] = out or [""]                                           # included by nobody → a root
        return memo[f]

    result, unknown = {}, set()
    for f, items in leaves.items():
        pre = prefixes(f)
        for route, handler in items:
            if None in pre or _unresolved(route):
                unknown.add((route, handler))
                continue
            bucket = result.setdefault((route, handler), [])
            for pp in pre:
                full = pp + _strip_anchors(route)
                if full not in bucket:
                    bucket.append(full)
    # Never claim a full path we can't see: concatenating an unfolded "?" yields a route that matches
    # nothing and silently drops REAL links. No full route → the caller falls back to the leaf ("tail").
    for key in unknown:
        result[key] = []
    return result


# ---- view analysis -------------------------------------------------------------------

def _written_cols(method, node):
    """Columns a write names, or None when not statically knowable. node is the ast.Call."""
    def _str_list(v):
        if isinstance(v, (ast.List, ast.Tuple)):
            names = [e.value for e in v.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            return set(names) if len(names) == len(v.elts) else None
        return None
    if method in ("create", "update", "get_or_create", "update_or_create", "acreate", "aupdate"):
        if any(k.arg is None for k in node.keywords):      # `**spread` → the column set is incomplete
            return None
        cols = {k.arg for k in node.keywords if k.arg and k.arg != "defaults"}
        for k in node.keywords:
            if k.arg == "defaults" and isinstance(k.value, ast.Dict):
                dk = [c.value for c in k.value.keys if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                if len(dk) == len(k.value.keys):
                    cols |= set(dk)
                else:
                    return None
            elif k.arg == "defaults":      # dynamic `defaults=` (not a literal dict) → can't know
                return None
        return cols or None
    if method == "save":
        for k in node.keywords:
            if k.arg == "update_fields":
                return _str_list(k.value)
        return None        # save() with no update_fields → whole row, columns unknown
    if method in ("bulk_update", "abulk_update"):
        if len(node.args) >= 2:
            return _str_list(node.args[1])
        for k in node.keywords:
            if k.arg == "fields":
                return _str_list(k.value)
        return None
    return None


class FactCollector(ast.NodeVisitor):
    """Walk a function body; collect lens facts. Records callees for 1-level follow."""

    def __init__(self, file: str = "", consts=None, class_models=None, self_type=None, known_models=None,
                 manager_model=None, index=None, ret_depth=0):
        self.file = file
        self.class_models = class_models or {}   # Serializer/Form name -> its Meta.model table
        self.known_models = known_models or set()  # all repo model/class names — for FK traversal detection
        self.self_type = self_type               # enclosing class, so `self.objects.x()` names its table
        self.manager_model = manager_model       # set when walking a Manager method: `self.create()` → this model
        self.db = []           # (model, kind, file, line)
        self.write_cols = {}   # (model, file, line) -> set of column names, or None = written, cols unknown
        self.where_only = set()  # ids of filter()/exclude() Call nodes that are only an UPDATE/DELETE
                                 # WHERE clause (no SELECT) — their read must not be recorded
        self.external = []     # (root, dest, file, line)
        self.async_ = []       # (mechanism, target, file, line)
        self.pii = []          # (attr, file, line)
        self.cache = []        # (kind, detail, file, line): cache read / mutate (E7)
        self.self_calls = []   # method names called as self.x(...)
        self.func_calls = []   # bare names called x(...)
        self.typed_calls = []  # (ClassName, method) called as Model.method(...)
        self.manager_calls = []  # (Model, method) for Model.objects.<custom>() — follow into its Manager
        self.manager_fallback = []  # (Model, method, file, line) — a read added only if the Manager is unresolved
        self.call_arg_models = {}  # call-key -> [model|None per positional arg]; binds a helper's param
                                   # to the model actually passed (`fetch_all(Product)` → param=Product)
        self.call_arg_insts = {}   # call-key -> [[binding|None per positional arg], …] — one list per distinct
                                   # call site, so `deliver(SPEC_A, …)` + `deliver(SPEC_B, …)` follow both
        self.inst_fields = {}      # var key ("spec", "self.spec") -> (Cls, {field: model}) — a config object
        self.var_types = {}    # local var name -> Model (so `order.save()` → Order:write)
        self.instance_types = {}  # local var name -> repo class it holds (`h = mod.Helper(req)` → Helper),
                                  # so a later `h.method()` follows into Helper.method (the write hides there)
        # local var name -> resolved string (so `requests.post(url)` names the dest); seeded with
        # module/repo-level URL constants so an imported `requests.post(LEARN_API)` resolves too.
        self.str_vars = dict(consts or {})
        self.tainted = {}      # local var name -> {sensitive field labels it carries}
        self.leaks = []        # (field, sink, file, line): a tainted field that reaches an egress sink
        self.local_names = set()  # names bound *inside* this fn (nested defs/closures, params) —
                                  # a call to one of these must never resolve to a module-level helper
        self.assigned_names = set()  # names bound by a local `x = …` assignment. A Capitalized one is
                                     # often a runtime model *alias* (`Alias = apps.get_model(…)` /
                                     # `Alias = RealModel`), NOT an imported model class — so a
                                     # get_object_or_404(<that name>, …) must not be credited as a
                                     # read of a table literally called `Alias`.
        self._fn_depth = 0        # nesting level while walking, so we don't treat the root fn's own
                                  # name as "local" (a facade `X()` that delegates to a module `X` must
                                  # still be followed — only genuinely nested defs are closures)
        self.index = index        # attached while walking a followed frame → enables return-type inference
        self._ret_depth = ret_depth

    def _loc(self, node):
        return (self.file, getattr(node, "lineno", 0))

    def _receiver_model(self, expr):
        """The repo model `expr` evaluates to, at any depth: a typed name, a dotted key typed from an annotation
        (`request.profile`), an FK field (`channel.project`) or a `@property` annotated `-> Model`
        (`self.owner_profile`). None when it can't be pinned down."""
        if isinstance(expr, ast.Name):
            return self._typed_model(expr)
        if not isinstance(expr, ast.Attribute) or self.index is None:
            return None
        typed = self.var_types.get(_receiver_key(expr))
        if typed and _is_model(self.index, typed):
            return typed
        owner = self._receiver_model(expr.value)
        if not owner:
            return None
        rel = self.index.model_field(owner, expr.attr)
        if rel and rel[0] == "fk":
            return rel[1]
        rev = self.index.reverse_relation(owner, expr.attr)
        if rev and rev[1] == "o2o":
            return rev[0]
        prop = self.index.property_def(owner, expr.attr)
        m = _anno_model(prop.returns) if prop is not None else None
        return m if m and _is_model(self.index, m) else None

    def _typed_model(self, expr):
        """The repo model a receiver holds: `self` in a model method, or a local typed by an ORM fetch."""
        if isinstance(expr, ast.Name):
            if expr.id in ("self", "cls"):
                return self.self_type
            t = self.var_types.get(expr.id)
            return t if t and t[:1].isupper() else None
        return None

    def _relation(self, f):
        """For `<recv>.<field>.<method>(...)`: (owner model, field, (kind, target, through)) or None."""
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and self.index is not None):
            return None
        owner = self._typed_model(f.value.value)
        rel = self.index.model_field(owner, f.value.attr) if owner else None
        return (owner, f.value.attr, rel) if rel else None

    def _reverse_m2m(self, f):
        """`check.channel_set.set(...)` where `Channel.checks = ManyToManyField(Check)`: the reverse side of an
        M2M on a typed receiver. Returns (model holding the field, field, through) or None."""
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)
                and f.value.attr.endswith("_set") and self.index is not None):
            return None
        owner = self._typed_model(f.value.value)
        stem = f.value.attr[:-4].replace("_", "").lower()
        hits = [m for m in self.known_models if m.lower() == stem]
        if not owner or len(hits) != 1:
            return None
        for field, (kind, target, through) in self.index.relation_fields(hits[0]).items():
            if kind == "m2m" and target == owner:
                return hits[0], field, through
        return None

    def _property_manager(self, f):
        """`check.visible_pings.order_by(...)` where Check has `@property def visible_pings(self) ->
        QuerySet[Ping]`: the declared return type names the model the property's queryset reads."""
        if not isinstance(f, ast.Attribute) or self.index is None \
                or f.attr not in (ORM_READ | ORM_WRITE | _SAME_MODEL_METHODS):
            return None, None
        cur = f.value
        while isinstance(cur, ast.Call):
            cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
        if not isinstance(cur, ast.Attribute):
            return None, None
        owner = self._receiver_model(cur.value)
        prop = self.index.property_def(owner, cur.attr) if owner else None
        m = _anno_model(prop.returns) if prop is not None else None
        if m and _is_model(self.index, m):
            return m, (f.attr if f.attr in (ORM_READ | ORM_WRITE) else "all")
        return None, None

    def _reverse_manager(self, f):
        """`check.flip_set.order_by(...)` — Django's default reverse-FK manager is `<model name lowercased>_set`.
        Resolve the stem against the repo's class names; no unique match records nothing (never invent a
        table). Queryset-building methods count as a read, like `Model.objects.filter(...)`."""
        if not isinstance(f, ast.Attribute) or f.attr not in (ORM_READ | ORM_WRITE | _SAME_MODEL_METHODS):
            return None, None
        if f.attr in {"add", "remove", "set", "clear"} and self._reverse_m2m(f):
            return None, None                    # an M2M join-table write, recorded by the m2m block
        cur = f.value
        while isinstance(cur, ast.Call):
            cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
        if isinstance(cur, ast.Attribute) and self.index is not None:       # `user.credentials.filter(...)`
            owner = self._receiver_model(cur.value)
            rev = self.index.reverse_relation(owner, cur.attr) if owner else None
            if rev and rev[1] in {"fk", "m2m"}:
                return rev[0], (f.attr if f.attr in (ORM_READ | ORM_WRITE) else "all")
        if not (isinstance(cur, ast.Attribute) and cur.attr.endswith("_set") and len(cur.attr) > 4):
            return None, None
        stem = cur.attr[:-4].replace("_", "").lower()
        hits = [m for m in self.known_models if m.lower() == stem]
        if len(hits) != 1:
            return None, None
        return hits[0], (f.attr if f.attr in (ORM_READ | ORM_WRITE) else "all")

    def _note_write_cols(self, model, method, node, fl, ln):
        if not model or model == "<instance>":
            return
        cols = _written_cols(method, node)
        key = (model, fl, ln)
        if key in self.write_cols:
            cur = self.write_cols[key]
            self.write_cols[key] = None if (cur is None or cols is None) else (cur | cols)
        else:
            self.write_cols[key] = cols

    def visit_Call(self, node: ast.Call):
        f = node.func
        fl, ln = self._loc(node)
        # inside a followed custom-Manager method: `self.create()/save()/filter()/…` acts on the model
        # the manager MANAGES (set via manager_model), so resolve it to that model's table — this is
        # what makes `Order.objects.import_batch()`'s `self.create()` a real write, not a fallback read.
        if self.manager_model and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id == "self" and f.attr in (ORM_WRITE | ORM_READ):
            self.db.append((self.manager_model, "write" if f.attr in ORM_WRITE else "read", fl, ln))
        # many-to-many: `channel.checks.add(*xs)` writes the join table (an explicit `through=` model, or
        # Django's default `<owner table>_<field>`); `channel.checks.all()/filter()` reads the target model
        rel = self._relation(f)
        m2m = None
        if rel and rel[2][0] == "m2m":
            owner, field, (_k, target, through) = rel
            m2m = (owner, field, target, through)
        elif isinstance(f, ast.Attribute) and f.attr in {"add", "remove", "set", "clear"} and self._reverse_m2m(f):
            holder, field, through = self._reverse_m2m(f)
            m2m = (holder, field, holder, through)     # the join table belongs to the model holding the field
        if m2m:
            owner, field, target, through = m2m
            if f.attr in {"add", "remove", "set", "clear"}:
                tbl = through or (self.index.table_for(owner)[0] and f"{self.index.table_for(owner)[0]}_{field}")
                if tbl:
                    self.db.append((tbl, "write", fl, ln))
            elif f.attr in (ORM_READ | _SAME_MODEL_METHODS):
                self.db.append((target, "read", fl, ln))
        # ORM:  <Model>.objects.<method>(...)  and chained querysets
        model, method = _orm_model_method(f, dotted=True)
        if not model:
            model, method = self._reverse_manager(f)
        if not model:
            model, method = self._property_manager(f)
        if model and method:
            if model in ("self", "cls"):                 # `self.objects.x()` in a model method →
                model = self.self_type or "<instance>"   # the enclosing class, not a table named "self"
            elif "." in model:                           # `self.model.objects` / `spec.model.objects` /
                model = self._attr_model(model) or "<instance>"   # `models.Job.objects` (see _attr_model)
            elif model[:1].islower():
                # a lowercase receiver before `.objects` is a *variable* (a param, a local, a runtime
                # `apps.get_model(...)` result), never a model class — Django/SQLAlchemy models are
                # PascalCase. Resolve it to the concrete model if we tracked its binding (e.g.
                # `link_model = SynonymLink`), else record an honest `<instance>` rather than
                # inventing a table literally named `model`/`link_model`/`target_model`.
                model = self.var_types.get(model) or "<instance>"
            if method in {"annotate", "aggregate"} and self.index is not None and model[:1].isupper():
                for arg in list(node.args) + [k.value for k in node.keywords]:    # `Count("checks")` joins checks
                    if isinstance(arg, ast.Call) and _dotted(arg.func).split(".")[-1] in _AGG_FUNCS \
                            and arg.args and isinstance(arg.args[0], ast.Constant) \
                            and isinstance(arg.args[0].value, str):
                        rel = self.index.model_field(model, arg.args[0].value.split("__")[0])
                        if rel:
                            self.db.append((rel[1], "read", fl, ln))
            if method == "raw":
                pass   # `<Model>.objects.raw(sql)` — the raw SQL text (parsed below into FROM/JOIN
                       # tables) names the real table; a model-level read here would double-count it.
            elif method not in ORM_READ and method not in ORM_WRITE and model[:1].isupper():
                # a non-standard method on `Model.objects` is a CUSTOM MANAGER method (`Order.objects.
                # import_batch(...)`). Follow into the Manager to get its real reads/writes; the read
                # is only a FALLBACK, added in `_walk_follow` iff the Manager can't be resolved — so a
                # resolved write (`self.create` inside the method) isn't shadowed by a spurious read.
                self.manager_calls.append((model, method))
                self.manager_fallback.append((model, method, fl, ln))
            else:
                kind = "write" if method in ORM_WRITE else "read"
                # `Model.objects.filter(...).update()/.delete()` — the filtered queryset feeding a
                # terminal write is a WHERE clause, not a SELECT; mark its read-method Call nodes so the
                # phantom read (and the phantom person-read PII it triggers) is never recorded.
                if method in {"update", "delete"} and isinstance(f, ast.Attribute) \
                        and isinstance(f.value, ast.Call):
                    cur = f.value
                    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
                        if cur.func.attr in ORM_READ:
                            self.where_only.add(id(cur))
                        cur = cur.func.value
                if not (kind == "read" and id(node) in self.where_only):
                    self.db.append((model, kind, fl, ln))
                if kind == "write":
                    self._note_write_cols(model, method, node, fl, ln)
            # Django FK traversal in filter/exclude/get kwargs: `fk_field__col__gte=val`
            # causes a JOIN on the related table. The kwarg stem is the FK field name (snake_case);
            # convert to CamelCase and check the repo index — if it's a known model, record a read.
            if method in {"filter", "exclude", "get", "get_or_create", "update_or_create"}:
                for kw in node.keywords:
                    # only a REAL traversal `fk__relatedcol[...]` joins a table. `field__lookup`
                    # (status__in, target_year__gt, removed_at__isnull, fk__in) is a plain WHERE on the
                    # local column — the token after the stem being a Django lookup means NO join.
                    if kw.arg and "__" in kw.arg and kw.arg.split("__")[1] not in _DJANGO_LOOKUPS:
                        stem = kw.arg.split("__")[0]
                        related = "".join(w.capitalize() for w in stem.split("_"))
                        if related in self.known_models:
                            # point at the FK-kwarg line (`fk__col=…`), NOT the whole `.filter(` call:
                            # the parent model already owns the call line, so sharing it made the two
                            # models resolve to the same source — clicking the FK'd table showed the
                            # parent's query. `ast.keyword` carries `lineno` on 3.9+ (else fall back).
                            kln = getattr(kw, "lineno", ln)
                            self.db.append((related, "read", fl, kln))
        # get_object_or_404(Model, …) / get_list_or_404(Model, …) — a Django shortcut that runs a
        # real query (Model.objects.get/filter under the hood). A common in-body read the ORM
        # matcher above misses because there is no literal `.objects`.
        if isinstance(f, ast.Name) and f.id in {"get_object_or_404", "get_list_or_404"} \
                and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id[:1].isupper() \
                and node.args[0].id not in self.assigned_names:      # skip a local model *alias* (see __init__)
            self.db.append((node.args[0].id, "read", fl, ln))
        # instance .save()/.delete() — resolve `var.save()` to its model when we know the type,
        # but skip non-ORM receivers (cache/session/storage) so they aren't mislabeled DB writes.
        if isinstance(f, ast.Attribute) and f.attr in {"save", "delete"} and not model \
                and not _is_non_orm_receiver(f.value):
            who = self._value_model(f.value)                 # a typed var, or `self.get_object(pk).save()`
            who = self.class_models.get(who, who)            # FooSerializer.save() → its Meta.model
            if who and who.endswith(NON_MODEL_SUFFIXES):     # a serializer/form we couldn't map → unknown
                who = None
            self.db.append((who or "<instance>", "write", fl, ln))
            if f.attr == "save":
                self._note_write_cols(who or None, "save", node, fl, ln)
        # queryset write via a typed var: `qs = Model.objects.filter(...); qs.update(...)` — the `.objects`
        # chain is gone by the time we reach `qs.update`, so only the var's resolved type names the table.
        # Unlike save/delete, require a KNOWN model: a bare `x.update(...)` is almost always a dict/context.
        # shortcut: `.extra(tables=,where=)` and dynamic `apps.get_model(...)` writes still miss — rare/deprecated.
        if isinstance(f, ast.Attribute) and f.attr == "update" and not model \
                and not _is_non_orm_receiver(f.value):
            who = self._value_model(f.value)                 # a typed var, or `self.get_object(pk).save()`
            who = self.class_models.get(who, who)
            if who and not who.endswith(NON_MODEL_SUFFIXES):
                self.db.append((who, "write", fl, ln))
                self._note_write_cols(who, "update", node, fl, ln)
        # SQLAlchemy (Flask/FastAPI stacks) — high-signal patterns, never a false ✓:
        #   session.query(Model) / db.session.query(Model)  → read
        if isinstance(f, ast.Attribute) and f.attr == "query" and node.args \
                and isinstance(node.args[0], ast.Name) and node.args[0].id[:1].isupper():
            self.db.append((node.args[0].id, "read", fl, ln))
        #   session.add(obj) / db.session.add(obj) / .delete(obj) / .merge(obj)  → write
        if isinstance(f, ast.Attribute) and f.attr in {"add", "delete", "merge"} \
                and _is_session_receiver(f.value):
            who = self.var_types.get(_root_name(node.args[0])) if node.args else None
            self.db.append((who or "<instance>", "write", fl, ln))
        #   session.execute(select(Model) / insert(Model) / update(Model) / delete(Model))  (SQLAlchemy 2.x)
        if isinstance(f, ast.Attribute) and f.attr == "execute" and node.args \
                and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name):
            stmt = node.args[0].func.id
            marg = node.args[0].args[0] if node.args[0].args else None
            if stmt in {"select", "insert", "update", "delete"} \
                    and isinstance(marg, ast.Name) and marg.id[:1].isupper():
                self.db.append((marg.id, "read" if stmt == "select" else "write", fl, ln))
        # raw SQL: cursor.execute(sql, …) / .executemany(sql) / <qs>.raw(sql). The SQL text may be a
        # string literal, an f-string, or a module constant (a `_SQL = """…"""` folded into str_vars).
        # We pull table names straight out of its FROM/JOIN/INTO/UPDATE clauses — DB access the ORM
        # matchers above never see because there is no `.objects`. The table names ARE the SQL tables,
        # so they need no model→table resolution downstream (build_edges leaves them as-is).
        if isinstance(f, ast.Attribute) and f.attr in {"execute", "executemany", "raw"} and node.args:
            sql = _extract_sql(node.args[0], self.str_vars)
            if sql:
                for tbl, kind in _sql_tables(sql):
                    self.db.append((tbl, kind, fl, ln))
                # a sensitive column in the SQL (`SELECT u.email …`) is a PII read the ORM lens misses
                pii_cols = {m.group(1).lower() for m in _SQL_PII_RE.finditer(sql)}
                for col in pii_cols:
                    self.pii.append((col, fl, ln))
                # taint the cursor so `rows = cursor.fetchall()` carries the PII and a later
                # `yield row` / `return rows` registers where it egresses (a raw-SQL PII export).
                if pii_cols and isinstance(f.value, ast.Name):
                    self.tainted.setdefault(f.value.id, set()).update(pii_cols)
        # cache read / mutate (E7): cache.get(k) / cache.set(k, …) / cache.delete(k) / caches['x'].clear()
        if isinstance(f, ast.Attribute) and _is_cache_receiver(f.value) \
                and f.attr in (CACHE_READ | CACHE_WRITE):
            kind = "read" if f.attr in CACHE_READ else "write"
            detail = f"{f.attr} {_cache_key(node, self.str_vars)}".strip()
            self.cache.append((kind, detail, fl, ln))
        # external calls — ANY method call on a known network/SDK root is egress. The verb list is no
        # longer a filter (it missed `stripe.Charge.create`, `boto3.client(...).put_object`, …): these
        # libraries exist to talk to external services, and the root is already gated to EXTERNAL_ROOTS
        # (so `Order.objects.create` — root `Order`, not a lib — never matches). `request`-family verbs
        # still lead the label when present, for readability.
        if isinstance(f, ast.Attribute):
            root = _root_name(f.value)
            if root in EXTERNAL_ROOTS:
                self.external.append((f"{root}.{f.attr}", _dest_of(node, self.str_vars), fl, ln))
        # async: threading.Thread(target=fn)
        if _is_threading_thread(f):
            tgt = _kw(node, "target")
            self.async_.append(("threading.Thread", tgt or "?", fl, ln))
        # async: .delay(...) / .apply_async(...)
        if isinstance(f, ast.Attribute) and f.attr in {"delay", "apply_async"}:
            self.async_.append((f"celery.{f.attr}", _root_name(f.value) or "?", fl, ln))
        # async: send_task('name')
        if (isinstance(f, ast.Name) and f.id == "send_task") or \
           (isinstance(f, ast.Attribute) and f.attr == "send_task"):
            self.async_.append(("celery.send_task", _first_const(node) or "?", fl, ln))
        # follow targets
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("self", "cls"):
            self.self_calls.append(f.attr)   # a classmethod reaches siblings via `cls.x()`, same as `self.x()`
            self._note_args(f"self.{f.attr}", node)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id[:1].isupper() and f.attr not in {"objects", "as_view"}:
            # Class.classmethod(...)  e.g. OrderedItems.insert_ordered_item(...) / OrderService.create(...).
            # ORM-verb names (create/update/…) are NOT excluded: a service/repo method is often named
            # exactly `create`/`save`, and an unresolved callee simply doesn't follow (see _walk_follow).
            self.typed_calls.append((f.value.id, f.attr))
            self._note_args(f"{f.value.id}.{f.attr}", node)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id[:1].islower() and f.value.id not in {"self", "cls"}:
            # module.fn(...) / instance.method(...) — e.g. rows.list_entities(...) or uc.create(...).
            # Same: don't exclude ORM verbs; resolution (mod_imports / instance_types) gates the follow.
            self.typed_calls.append((f.value.id, f.attr))
            self._note_args(f"{f.value.id}.{f.attr}", node)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) and self._receiver_model(f.value):
            # `check.project.update_next_nag_dates()` / `request.project.num_checks_available()` /
            # `self.owner_profile.num_checks_available()`: a method on the model an FK, a typed request attribute
            # or a `-> Model` property points to
            target = self._receiver_model(f.value)
            self.typed_calls.append((target, f.attr))
            self._note_args(f"{target}.{f.attr}", node)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call) \
                and isinstance(f.value.func, ast.Name) and f.value.func.id[:1].isupper():
            # Service().method(...)  e.g. ProductService().get_product_details(...)
            self.typed_calls.append((f.value.func.id, f.attr))
            self._note_args(f"{f.value.func.id}.{f.attr}", node)
        elif isinstance(f, ast.Name):
            self.func_calls.append(f.id)
            self._note_args(f.id, node)
        # taint sink: a tainted (sensitive) value passed into an egress call → a traced leak
        sink = _sink_label(f)
        if sink:
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                for label in self._taint_labels(arg):
                    self.leaks.append((label, sink, fl, ln))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.ctx, ast.Load) and self.index is not None:     # reading a @property runs its body
            owner = self._receiver_model(node.value)
            if owner and self.index.property_def(owner, node.attr) is not None:
                self.typed_calls.append((owner, node.attr))
            rev = self.index.reverse_relation(owner, node.attr) if owner else None
            if rev and rev[1] == "o2o":                      # `user.profile` is a query on the profile table
                self.db.append((rev[0], "read", self.file, getattr(node, "lineno", 0)))
        if node.attr in SENSITIVE:
            self.pii.append((node.attr, self.file, getattr(node, "lineno", 0)))
        # SQLAlchemy declarative query: `Model.query.filter(...)` → a read of Model
        if node.attr == "query" and isinstance(node.value, ast.Name) and node.value.id[:1].isupper():
            self.db.append((node.value.id, "read", self.file, getattr(node, "lineno", 0)))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        # remember `x = <something that reveals a Model>` so a later `x.save()` names the table — an ORM
        # chain, a typed var (`row = qs.first()`, `x = y`), or a repo helper's return type (`obj = self.get_object(pk)`)
        model = self._value_model(node.value)
        if not model and isinstance(node.value, ast.Name) and node.value.id in self.known_models:
            model = node.value.id                                      # `link_model = SynonymLink`
            # a bare class alias: a later `link_model.objects.filter()` now resolves to the real model
        # `h = Helper(...)` / `h = module.Helper(...)` where Helper is a repo class → remember the type
        # so `h.method()` can be followed into Helper.method (where controllers hide the real DB writes).
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
            callee = node.value.func
            cls = callee.id if isinstance(callee, ast.Name) else (
                callee.attr if isinstance(callee, ast.Attribute) else None)
            if cls and cls[:1].isupper():
                self.instance_types[node.targets[0].id] = cls
        # `spec = self.spec` / `spec = SPEC_X` / `svc = Service(Job)` → the config object, and the models it
        # carries, ride along so `spec.jobs()` follows into its class with `self.model` bound
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            b = self._binding_of(node.value)
            if b:
                self.inst_fields[node.targets[0].id] = b
                self.instance_types[node.targets[0].id] = b[0]
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                self.assigned_names.add(tgt.id)                        # a locally-bound name (see __init__)
            if isinstance(tgt, (ast.Name, ast.Attribute)) and model:   # x = … / self.x = Model(…)
                self.var_types[_receiver_key(tgt)] = model
            elif isinstance(tgt, (ast.Tuple, ast.List)) and tgt.elts \
                    and isinstance(tgt.elts[0], ast.Name) and isinstance(node.value, ast.Call):
                m, meth = _orm_model_method(node.value.func)           # obj, created = M.objects.get_or_create()
                if m and meth in {"get_or_create", "update_or_create"}:
                    self.var_types[tgt.elts[0].id] = m
                elif self.index is not None and self._ret_depth < RET_DEPTH:
                    fn, ffile, owner = self._resolve_callee(node.value.func)       # a, b = helper(...)
                    cols = _return_tuple(self.index, fn, ffile, owner, self._ret_depth) if fn is not None else None
                    for elt, em in zip(tgt.elts, cols or []):
                        if isinstance(elt, ast.Name) and em:
                            self.var_types[elt.id] = em
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            val = _fold_str(node.value, self.str_vars)       # `url = 'x' + settings.Y` → resolve dest later
            if val is not None:
                self.str_vars[name] = val
            labels = self._taint_labels(node.value)          # `email = user.email` → var is tainted
            if labels:
                self.tainted[name] = set(labels)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._fn_depth += 1
        if self._fn_depth > 1:                   # a genuinely nested def/closure — local, not a helper
            self.local_names.add(node.name)
        # a parameter annotated with a model type resolves `param.save()` inside the body
        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            self.local_names.add(a.arg)          # a param (e.g. a callback) shadows any global name
            m = _anno_model(a.annotation)
            if m:
                self.var_types[a.arg] = m
                if self.index is not None:           # `request: AuthenticatedHttpRequest` declares `profile: Profile`
                    for attr, am in self.index.class_attr_models(m).items():
                        self.var_types[f"{a.arg}.{attr}"] = am
        self.generic_visit(node)
        self._fn_depth -= 1
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node: ast.For):
        # `for order in Order.objects.filter(...)` → loop var is an Order (resolves `order.save()`). Not when
        # iterating `.values()`/`.values_list()` directly: those rows are dicts/tuples, not instances.
        if isinstance(node.target, ast.Name):
            it = node.iter
            rows = isinstance(it, ast.Call) and _orm_model_method(it.func)[1] in ("values", "values_list")
            model = None if rows else self._value_model(it)
            if model:
                self.var_types[node.target.id] = model
            labels = self._taint_labels(node.iter)           # iterating a tainted collection
            if labels:
                self.tainted[node.target.id] = set(labels)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return):
        # returning a tainted value → it leaves via the HTTP response
        if node.value is not None:
            fl, ln = self._loc(node)
            for label in self._taint_labels(node.value):
                self.leaks.append((label, "response", fl, ln))
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield):
        # `yield <tainted>` in a streaming generator (e.g. a raw-SQL CSV export) sends PII to the
        # client the same way `return` does — the row fetched from a PII SELECT leaves here.
        if node.value is not None:
            fl, ln = self._loc(node)
            for label in self._taint_labels(node.value):
                self.leaks.append((label, "response", fl, ln))
        self.generic_visit(node)
    visit_YieldFrom = visit_Yield

    def _taint_labels(self, node) -> set:
        """Sensitive field labels an expression carries — the taint half of the flow. Conservative:
        propagates through attribute/subscript reads of a sensitive name, request `.get('field')`,
        dict/list/f-string/concat construction, and serialize-only wrappers — but NOT arbitrary
        calls (which may sanitize), so a resulting leak is trustworthy, not a guess."""
        if node is None:
            return set()
        labels = set()
        if isinstance(node, ast.Attribute):
            if node.attr in SENSITIVE:
                labels.add(node.attr)
            if isinstance(node.value, ast.Name) and node.value.id in self.tainted:
                labels |= self.tainted[node.value.id]
        elif isinstance(node, ast.Name):
            labels |= self.tainted.get(node.id, set())
        elif isinstance(node, ast.Subscript):
            key = _subscript_key(node)
            if key in SENSITIVE:
                labels.add(key)
            labels |= self._taint_labels(node.value)
        elif isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if isinstance(f, ast.Attribute) and f.attr == "get" and node.args:
                if _const_str(node.args[0]) in SENSITIVE:
                    labels.add(_const_str(node.args[0]))
            if fname in TAINT_WRAPPERS:
                for a in list(node.args) + [k.value for k in node.keywords]:
                    labels |= self._taint_labels(a)
            # `cursor.fetchone/fetchmany/fetchall()` on a tainted cursor carries the cursor's taint —
            # so PII selected by a raw SQL SELECT flows into the fetched rows.
            if isinstance(f, ast.Attribute) and f.attr in {"fetchone", "fetchmany", "fetchall"} \
                    and isinstance(f.value, ast.Name):
                labels |= self.tainted.get(f.value.id, set())
        elif isinstance(node, ast.Dict):
            labels |= {_const_str(k) for k in node.keys if _const_str(k) in SENSITIVE}
            for v in node.values:
                labels |= self._taint_labels(v)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for e in node.elts:
                labels |= self._taint_labels(e)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    labels |= self._taint_labels(part.value)
        elif isinstance(node, ast.BinOp):
            labels |= self._taint_labels(node.left) | self._taint_labels(node.right)
        return labels

    def _arg_models(self, node):
        """Positional args of a call resolved to concrete model names (or None), so a followed helper
        can bind its param to the model actually passed — `fetch_all(Product)` lets `model.objects` in
        `def fetch_all(model)` resolve to Product instead of degrading to <instance>."""
        out = []
        for a in node.args:
            if isinstance(a, ast.Name) and a.id in self.known_models:
                out.append(a.id)                       # a model class passed by name
            elif self.var_types.get(_receiver_key(a) or ""):
                out.append(self.var_types[_receiver_key(a)])   # a local / `self.model` known to hold a model
            else:
                out.append(None)
        return out

    def _note_args(self, key, node):
        """Record what a call passes: models (`fetch_all(Product)`) and config objects (`deliver(spec)`)."""
        self.call_arg_models[key] = self._arg_models(node)
        insts = [self._binding_of(a) if isinstance(a, (ast.Name, ast.Attribute)) else None for a in node.args]
        if any(insts) and insts not in self.call_arg_insts.setdefault(key, []):
            self.call_arg_insts[key].append(insts)

    def _binding_of(self, expr):
        """The config object an expression holds: a bound var (`spec`, `self.spec`), a module const
        (`SPEC_X`), or a repo-class ctor with model args (`Service(Job)`) — else None."""
        key = _receiver_key(expr)
        if key in self.inst_fields:
            return self.inst_fields[key]
        if self.index is None:
            return None
        near = os.path.join(self.index.root, self.file)
        if isinstance(expr, ast.Name) and expr.id[:1].isupper() and expr.id not in self.known_models:
            return self.index.instance_const(expr.id, near=near)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) \
                and expr.value.id not in ("self", "cls") and expr.attr[:1].isupper():
            return self.index.instance_const(expr.attr, near=near)     # `push_queue.SPEC_FRESH`
        if isinstance(expr, ast.Call):
            b = _ctor_binding(self.index, expr, lambda e: self.var_types.get(_receiver_key(e) or ""))
            return b if b and b[1] else None      # a plain `Helper(req)` is already in instance_types
        return None

    def _attr_model(self, key):
        """Model behind a dotted `.objects` receiver: a bound attribute (`self.model`, `spec.model`), or a
        module-qualified class (`models.Job`, `serializers.MarketingCampaign`) — else None."""
        if key in self.var_types:
            return self.var_types[key]
        base, _, attr = key.rpartition(".")
        if base in self.inst_fields:
            return self.inst_fields[base][1].get(attr)
        if base not in ("self", "cls") and attr in self.known_models:
            return attr
        return None

    def _value_model(self, expr):
        """Model an assigned / iterated / saved expression evaluates to: `_expr_model`, else return-type
        inference on the repo call at the root of a same-model chain (`get_rows(...).first()`)."""
        m = _expr_model(expr, self.var_types)
        if m:
            return m
        if isinstance(expr, ast.Attribute):                    # `user.profile` / `channel.project` / `self.owner_profile`
            m = self._receiver_model(expr)
            if m:
                return m
        if isinstance(expr, ast.Name) and expr.id in ("self", "cls") and self.self_type \
                and self.index is not None and _is_model(self.index, self.self_type):
            return self.self_type                              # `self.save()` in a MODEL method (not a service class)
        root = _chain_root(expr)
        return self._call_return_model(root) if isinstance(root, ast.Call) else None

    def _call_return_model(self, call):
        """Model returned by the repo function/method `call` invokes (return-type inference), else None.
        Only while walking a followed frame (an `index` is attached), and within RET_DEPTH hops."""
        if self.index is None or self._ret_depth >= RET_DEPTH:
            return None
        fn, ffile, owner = self._resolve_callee(call.func)
        return _return_model(self.index, fn, ffile, owner, self._ret_depth) if fn is not None else None

    def _resolve_callee(self, f):
        """(def, file, owner class) for a call target we can pin to a repo def — `helper(...)`,
        `self.m(...)` / `cls.m(...)`, `Cls.m(...)`, `obj.m(...)` with `obj = Cls(...)` — else (None,)*3."""
        idx = self.index
        if isinstance(f, ast.Name):
            if f.id in self.local_names or f.id[:1].isupper():   # a closure/param, or a class being built
                return None, None, None
            fn, ffile = idx.find_func(f.id, near=os.path.join(idx.root, self.file), toplevel_only=True)
            return fn, ffile, None
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            recv = f.value.id
            owner = self.self_type if recv in ("self", "cls") else self.instance_types.get(recv, recv)
            if owner and owner[:1].isupper():
                meth, mfile = _find_method(idx, owner, f.attr)
                if meth is not None:
                    return meth, mfile, owner
        return None, None, None

    @staticmethod
    def _model_of(value):
        # `list(qs)` / `tuple(qs)` / `sorted(qs)` / `reversed(qs)` keep the element type — common when a
        # queryset must be forced inside an atomic block (`list(M.objects.select_for_update()…)`). Not
        # `set()`: `set.update()` is a real method and would turn into a false write.
        while isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in {"list", "tuple", "sorted", "reversed"} and len(value.args) == 1:
            value = value.args[0]
        if not isinstance(value, ast.Call):
            return None
        f = value.func
        if isinstance(f, ast.Name) and f.id[:1].isupper():          # Model(...)
            return f.id
        m, _ = _orm_model_method(f)                                 # Model.objects.<method>(...)
        if m and m[:1].isupper():
            return m                # skip a lowercase receiver (`table_class.objects.get()`): it's a
                                    # runtime-chosen model, not a class — don't propagate it as a type
                                    # that a later `.save()` would then write to a table named `table_class`
        if isinstance(f, ast.Name) and f.id in {"get_object_or_404", "get_list_or_404"} \
                and value.args and isinstance(value.args[0], ast.Name) \
                and value.args[0].id[:1].isupper():                 # get_object_or_404(Model, …)
            return value.args[0].id
        return None


def _orm_model_method(f, dotted=False):
    """Match Model.objects.method — return (Model, method). `dotted=True` also matches an attribute
    receiver and returns it dotted (`self.model.objects.x` → "self.model") for the caller to resolve."""
    # f is Attribute like  <...>.method ; walk down looking for `.objects`
    if not isinstance(f, ast.Attribute):
        return None, None
    method = f.attr
    cur = f.value
    while isinstance(cur, ast.Call):
        cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
    # cur should be Attribute `<Model>.objects` or Name via manager
    if isinstance(cur, ast.Attribute) and cur.attr == "objects" and isinstance(cur.value, ast.Name):
        return cur.value.id, method
    if dotted and isinstance(cur, ast.Attribute) and cur.attr == "objects" \
            and isinstance(cur.value, ast.Attribute) and _root_name(cur.value) is not None:
        return _dotted(cur.value), method          # a pure name chain only (not `x().model.objects`)
    return None, None


# ---- return-type inference ------------------------------------------------------------
# Methods that hand back the SAME model as their receiver (a queryset of it, or an instance), so
# `qs.filter(...).order_by(...).first()` on a typed `qs` is still that model. Deliberately NOT values()/
# values_list() (dicts/tuples), count()/exists() (scalars), update()/delete() (row counts).
_SAME_MODEL_METHODS = {"filter", "exclude", "all", "order_by", "distinct", "select_related",
                       "prefetch_related", "using", "select_for_update", "only", "defer", "annotate",
                       "reverse", "none", "first", "last", "get", "latest", "earliest", "create"}
_VALIDATION_ENTRY = {"is_valid", "full_clean", "validate"}      # entry points of Django forms / DRF serializers
_VALIDATORS = {"clean", "validate"}
_VALIDATOR_PREFIXES = ("clean_", "validate_")
_AGG_FUNCS = {"Count", "Sum", "Avg", "Min", "Max"}
RET_DEPTH = 3                     # helper → helper hops followed to resolve a returned value
_NON_MODEL_RETURN_SUFFIXES = NON_MODEL_SUFFIXES + ("Response", "Exception", "Error")
_MODEL_BASES = {"AbstractUser", "AbstractBaseUser"}


def _chain_root(expr):
    """Strip same-model queryset methods: `qs.filter(...).order_by(...).first()` → `qs`."""
    while isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr in _SAME_MODEL_METHODS:
        expr = expr.func.value
    return expr


def _expr_model(expr, var_types):
    """The model an expression evaluates to (an instance, or a queryset of it), else None: an ORM chain /
    model ctor / get_object_or_404 (`_model_of`), or a typed variable — bare (`return group`), indexed
    (`qs[0]`), or through same-model queryset methods (`qs.filter(...).order_by(...).first()`)."""
    m = FactCollector._model_of(expr)
    if m:
        return m
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
            and expr.func.id in {"get_object_or_404", "get_list_or_404"} and expr.args:
        return _expr_model(expr.args[0], var_types)        # `get_object_or_404(q, ...)` with a typed queryset
    cur = _chain_root(expr)
    if isinstance(cur, ast.Subscript):
        cur = cur.value
    key = _receiver_key(cur)
    return var_types.get(key) if key else None


def _ctor_name(expr):
    """`X(...)` / `mod.X(...)` → "X" (a Capitalized call: a class being instantiated), else None."""
    if not isinstance(expr, ast.Call):
        return None
    f = expr.func
    n = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
    return n if n[:1].isupper() else None


def _is_model(index, name, _stack=()):
    """True if `name` is a repo class that is (or inherits from) a Django model. Bases from outside the
    repo count when they look like one (`models.Model`, `TimeStampedModel`, `AbstractUser`). Memoized."""
    memo = index.__dict__.setdefault("_is_model_memo", {})
    if name in memo:
        return memo[name]
    cls, _ = index.find_class(name)
    ok = False
    if cls is not None and name not in _stack:
        for b in cls.bases:
            bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
            if bn.endswith("Model") or bn in _MODEL_BASES or (bn and _is_model(index, bn, _stack + (name,))):
                ok = True
                break
    memo[name] = ok
    return ok


def _find_method(index, cls_name, meth, _stack=()):
    """(def, file) of `meth` on repo class `cls_name`, searching up its bases; else (None, None)."""
    cls, cfile = index.find_class(cls_name)
    if cls is None or cls_name in _stack:
        return None, None
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == meth:
            return m, cfile
    for b in cls.bases:
        bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
        if bn:
            hit = _find_method(index, bn, meth, _stack + (cls_name,))
            if hit[0] is not None:
                return hit
    return None, None


# ---- injected models ------------------------------------------------------------------
# A model can reach `.objects` through an attribute instead of its name: a class attribute (`class
# JobList(View): model = Job` → `self.model.objects`), or a config object built with it (`SPEC = QueueSpec(
# model=M)`, `svc = Service(Article)` whose `__init__` stores `self.Article = article_model`). A binding is
# `"self.model" → "Job"` or `"self.spec" → ("QueueSpec", {"model": "M"})`, made ONLY on a static chain to a
# repo model — anything else stays an honest `<instance>` (a wrong table is worse than a miss).

def _name_of(expr):
    """`X` / `mod.X` → "X", else None."""
    if isinstance(expr, ast.Name):
        return expr.id
    return expr.attr if isinstance(expr, ast.Attribute) else None


def _ctor_binding(index, call, arg_model=None):
    """`Cls(...)` on a repo class → (Cls, {field: model}) for the args that are repo models: by kwarg name
    when the class has no `__init__` (a dataclass), else through `__init__`'s `self.<field> = <param>`.
    `arg_model(expr)` resolves a caller-local variable to a model. None when Cls isn't a repo class."""
    cls = _ctor_name(call)
    if not cls or index.find_class(cls)[0] is None:
        return None

    def model_of(e):
        n = _name_of(e)
        if n and _is_model(index, n):
            return n
        return arg_model(e) if arg_model else None
    init, _ = _find_method(index, cls, "__init__")
    fields = {}
    if init is None:
        # shortcut: positional args to a dataclass need its field order — only kwargs bind
        for kw in call.keywords:
            m = model_of(kw.value) if kw.arg else None
            if m:
                fields[kw.arg] = m
    else:
        params = [a.arg for a in init.args.posonlyargs + init.args.args][1:]
        bound = {p: model_of(a) for p, a in zip(params, call.args)}
        bound.update({kw.arg: model_of(kw.value) for kw in call.keywords if kw.arg})
        for st in ast.walk(init):
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Attribute) \
                    and isinstance(st.targets[0].value, ast.Name) and st.targets[0].value.id == "self":
                v = st.value
                m = bound.get(v.id) if isinstance(v, ast.Name) and v.id in bound else model_of(v)
                if m:
                    fields[st.targets[0].attr] = m
    return cls, fields


def _class_bindings(index, cls, cfile, _stack=()):
    """`self.<attr>` / `cls.<attr>` bindings a class body declares, its repo bases' first (nearest wins):
    `model = Job`, `spec = SPEC_X` (a config const), `spec = Cfg(model=M)`."""
    memo = index.__dict__.setdefault("_class_bindings_memo", {})
    if not _stack and id(cls) in memo:
        return memo[id(cls)]
    out = {}
    if cls.name in _stack:
        return out
    for b in cls.bases:
        bn = _name_of(b)
        bcls, bfile = index.find_class(bn, near=cfile) if bn else (None, None)
        if bcls is not None and bcls is not cls:
            out.update(_class_bindings(index, bcls, bfile, _stack + (cls.name,)))
    for st in cls.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            tgt, v = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            tgt, v = st.target, st.value
        else:
            continue
        if not isinstance(tgt, ast.Name):
            continue
        n = _name_of(v)
        if n and _is_model(index, n):
            b = n
        elif n:
            b = index.instance_const(n, near=cfile)
        else:
            b = _ctor_binding(index, v) if isinstance(v, ast.Call) else None
        if b:
            out[f"self.{tgt.id}"] = out[f"cls.{tgt.id}"] = b
    if not _stack:
        memo[id(cls)] = out
    return out


def _bind_sig(bindings):
    """Stable text form of a binding set — part of a follow key, so one helper reached with two
    different config objects is analyzed for each."""
    if not bindings:
        return ""
    return "|" + repr(sorted((k, v if isinstance(v, str) else (v[0], sorted(v[1].items())))
                             for k, v in bindings.items()))


def _own_returns(fn):
    """`return` statements of `fn` itself — not of closures / lambdas / classes nested inside it."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Return):
            yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))


def _return_model(index, fn, ffile, owner, depth):
    """Return-type inference: the ONE model a repo function hands back across its `return`s, else None.
    Error-path returns (`Response(status=404)`, None, a dict) don't count, so
    `try: return Campaign.objects.get(pk=pk)` / `except …: return Response(404)` is a Campaign. Two
    different models → None (ambiguous — stay conservative). The body is walked with a fresh collector
    so a local `qs = M.objects…; return qs.first()` resolves through `qs`. Memoized per def; depth-capped."""
    memo = index.__dict__.setdefault("_ret_memo", {})
    if id(fn) in memo:
        return memo[id(fn)]
    memo[id(fn)] = None                                         # recursion guard
    declared = _anno_model(getattr(fn, "returns", None))        # `-> Check` / `-> Check | None` is exact
    if declared and _is_model(index, declared):
        memo[id(fn)] = declared
        return declared
    known = index.__dict__.get("_known_models")
    if known is None:
        known = index.__dict__["_known_models"] = set(index.classes)
    c = FactCollector(_rel(index, ffile), consts=index.consts_for(ffile), class_models=index.class_models,
                      self_type=owner, known_models=known, index=index, ret_depth=depth + 1)
    c.visit(fn)
    models = set()
    for r in _own_returns(fn):
        if r.value is None:
            continue
        ctor = _ctor_name(r.value)
        if ctor:                                                # a bare `X(...)`: only a repo MODEL counts
            m = ctor if _is_model(index, ctor) else None
        else:
            m = c._value_model(r.value)
            if m and (m.endswith(_NON_MODEL_RETURN_SUFFIXES)
                      or (index.find_class(m)[0] is not None and not _is_model(index, m))):
                m = None                                        # a typed Helper / Response var, not a table
        if m:
            models.add(m)
    memo[id(fn)] = models.pop() if len(models) == 1 else None
    return memo[id(fn)]


def _return_tuple(index, fn, ffile, owner, depth):
    """Per-position models of a function that returns a tuple (`-> tuple[Check, bool]`, or `return check, rw`
    in every return): a list like [Check, None], else None. Positions that disagree between returns stay None."""
    memo = index.__dict__.setdefault("_ret_tuple_memo", {})
    if id(fn) in memo:
        return memo[id(fn)]
    memo[id(fn)] = None
    ann = getattr(fn, "returns", None)
    if isinstance(ann, ast.Subscript) and _dotted(ann.value).split(".")[-1] in {"tuple", "Tuple"}:
        cols = [m if m and _is_model(index, m) else None for m in map(_anno_model, _anno_args(ann))]
        memo[id(fn)] = cols if any(cols) else None
        return memo[id(fn)]
    known = index.__dict__.get("_known_models")
    if known is None:
        known = index.__dict__["_known_models"] = set(index.classes)
    c = FactCollector(_rel(index, ffile), consts=index.consts_for(ffile), class_models=index.class_models,
                      self_type=owner, known_models=known, index=index, ret_depth=depth + 1)
    c.visit(fn)
    cols = None
    for r in _own_returns(fn):
        if not isinstance(r.value, ast.Tuple):
            continue
        row = []
        for e in r.value.elts:
            m = c._value_model(e)
            row.append(m if m and not m.endswith(_NON_MODEL_RETURN_SUFFIXES) and _is_model(index, m) else None)
        if cols is None:
            cols = row
        elif len(cols) == len(row):
            cols = [a if a == b else None for a, b in zip(cols, row)]
        else:
            cols = None
            break
    memo[id(fn)] = cols if cols and any(cols) else None
    return memo[id(fn)]


# ---- raw SQL --------------------------------------------------------------------------
# Table extraction for DB access that bypasses the ORM (a `cursor.execute(sql)` or `.raw(sql)`).
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)          # strip -- line and /* */ comments
_SQL_WRITE_RE = re.compile(r'\b(?:insert\s+into|update|delete\s+from)\s+[`"\[]?([\w.]+)', re.I)
_SQL_READ_RE = re.compile(r'\b(?:from|join)\s+[`"\[]?([\w.]+)', re.I)
# CTE names: `WITH x AS (…), y AS (…)`. A later `FROM x`/`JOIN x` references the CTE, NOT a table —
# excluding these stops the query-local alias being hallucinated as a real table.
_SQL_CTE_RE = re.compile(r'(?:\bwith\s+|,\s*)([`"\[]?\w+[`"\]]?)\s+as\s*\(', re.I)
_SQL_STOPWORDS = {"select", "dual", "lateral", "only", "where", "values", "set"}

# High-confidence PII column names to scan for inside raw SQL (a `SELECT u.email …` reads PII the ORM
# lens would otherwise miss). Curated to avoid collisions (`address`/`pan`/`card` are too ambiguous as
# bare SQL tokens); `\b` bounds each so `email` in `email_verified` / `user.email` behaves correctly.
_SQL_PII_COLS = ("user_email", "billing_email", "email", "phone_number", "mobile_number",
                 "phone", "mobile", "password", "ssn", "aadhaar", "aadhar",
                 "date_of_birth", "dob", "card_number", "pan_number", "passport")
_SQL_PII_RE = re.compile(r"\b(" + "|".join(_SQL_PII_COLS) + r")\b", re.I)


def _extract_sql(arg, str_vars):
    """The SQL string behind an execute()/raw() first arg: a literal, an f-string, a module constant
    (`_SQL`, seeded into str_vars), or a `text("…")` wrapper. None when nothing static resolves."""
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
            and arg.func.id == "text" and arg.args:
        arg = arg.args[0]                                 # SQLAlchemy text("…")
    return _fold_str(arg, str_vars)


def _clean_table(tok):
    """Normalise a captured table token: strip quotes/backticks/brackets and any schema prefix. None
    for a `{placeholder}`, a non-identifier, or a SQL keyword that slipped past the clause match."""
    tok = tok.strip().strip('`"[]').split(".")[-1]        # "schema"."table" → table
    if not tok or "{" in tok or not re.match(r"^\w+$", tok):
        return None
    if tok.lower() in _SQL_STOPWORDS:
        return None
    return tok


def _sql_tables(sql):
    """(table, kind) pairs from a raw SQL string — writes from INSERT/UPDATE/DELETE, reads from
    FROM/JOIN. Comment-stripped and subquery-aware (a `FROM (subquery)` opener has no identifier to
    capture, so it's skipped). Conservative: only fires when the string actually looks like SQL, and
    a token it can't pin to a bare identifier is dropped, never guessed. A table that is both written
    and read is reported once, as a write."""
    if not sql:
        return []
    if not any(k in sql.lower() for k in ("select", "insert", "update", "delete")):
        return []
    sql = _SQL_COMMENT_RE.sub(" ", sql)
    ctes = {_clean_table(m.group(1)) for m in _SQL_CTE_RE.finditer(sql)}   # query-local, not tables
    ctes.discard(None)
    out, seen = [], set()
    for m in _SQL_WRITE_RE.finditer(sql):
        t = _clean_table(m.group(1))
        if t and t not in ctes and t not in seen:
            seen.add(t)
            out.append((t, "write"))
    for m in _SQL_READ_RE.finditer(sql):
        t = _clean_table(m.group(1))
        if t and t not in ctes and t not in seen:
            seen.add(t)
            out.append((t, "read"))
    return out


def _is_session_receiver(node) -> bool:
    """A SQLAlchemy Session-like receiver: `session`, `db.session`, `async_session`, `*_session`.
    Used to tell `session.add(obj)` (a DB write) apart from Django's m2m `related.add(obj)`."""
    if isinstance(node, ast.Name):
        return node.id == "session" or node.id.endswith("_session")
    if isinstance(node, ast.Attribute):
        return node.attr == "session" or node.attr.endswith("_session")
    return False


def _is_non_orm_receiver(node) -> bool:
    """True for `.save()/.delete()` receivers that are NOT the ORM: `cache`, `request.session`,
    `caches['default']`, storage — so we don't record them as DB writes."""
    if isinstance(node, ast.Name):
        return node.id in NON_ORM_RECEIVERS
    if isinstance(node, ast.Attribute):
        return node.attr in NON_ORM_RECEIVERS        # request.session, self.cache
    if isinstance(node, ast.Subscript):
        return _is_non_orm_receiver(node.value)       # caches['default']
    return False


def _is_cache_receiver(node) -> bool:
    """True for a Django cache receiver: `cache`, `caches['default']`, or `self.cache`."""
    if isinstance(node, ast.Name):
        return node.id in {"cache", "caches"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"cache", "caches"}
    if isinstance(node, ast.Subscript):
        return _is_cache_receiver(node.value)
    return False


def _cache_key(node: ast.Call, str_vars: dict) -> str:
    """The cache key (first arg) folded to a string, `{var}` if it's a variable, or '' if none."""
    if not node.args:
        return ""
    return _fold_str(node.args[0], str_vars) or _placeholder(node.args[0])


def _receiver_key(node):
    """var_types lookup key for a `.save()/.delete()` receiver: a bare name, or a dotted `self.x`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node)                          # self.instance -> "self.instance"
    return None


def _is_none(node):
    return isinstance(node, ast.Constant) and node.value is None


def _anno_args(ann):
    """The type arguments of a subscripted annotation (`tuple[A, B]` → [A, B]), tolerating ast.Index."""
    sl = ann.slice
    if sl.__class__.__name__ == "Index":                    # Python <3.9
        sl = sl.value
    return list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]


def _anno_model(ann):
    """A model name from a type annotation (`obj: EntityLock`, `models.Foo`, or a `"Foo"` forward
    ref), or None. Uppercase-first heuristic — a builtin like `str`/`int` is ignored. Looks through
    `Optional[X]`, `X | None`, `Union[X, None]` and `QuerySet[X]` (a queryset of X is typed as X)."""
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        sides = [x for x in (ann.left, ann.right) if not _is_none(x)]
        return _anno_model(sides[0]) if len(sides) == 1 else None
    if isinstance(ann, ast.Subscript):
        if _dotted(ann.value).split(".")[-1] in {"Optional", "QuerySet", "Union"}:
            args = [x for x in _anno_args(ann) if not _is_none(x)]
            return _anno_model(args[0]) if len(args) == 1 else None
        return None
    if isinstance(ann, ast.Name) and ann.id[:1].isupper():
        return ann.id
    if isinstance(ann, ast.Attribute) and ann.attr[:1].isupper():
        return ann.attr
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:                                          # a forward ref is an expression in a string:
            inner = ast.parse(ann.value.strip(), mode="eval").body       # "Foo" / "Optional[Foo]" / "QuerySet[Foo]"
        except SyntaxError:
            return None
        return _anno_model(inner)
    return None


def _root_name(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _subscript_key(node: ast.Subscript):
    """The constant string key of `x['key']`, tolerating the pre-3.9 ast.Index wrapper."""
    s = node.slice
    if s.__class__.__name__ == "Index":     # Python <3.9 compatibility
        s = s.value
    return _const_str(s)


def _sink_label(f):
    """If a call's callee `f` is a known egress sink, its label — else None.
    Sinks: an external client call, a Celery dispatch, a logging call, or an HTTP Response()."""
    if isinstance(f, ast.Attribute):
        root = _root_name(f.value)
        if root in EXTERNAL_ROOTS:                      # any call on a network/SDK root is egress
            return f"{root}.{f.attr}"
        if f.attr in {"delay", "apply_async"}:
            return f"celery.{f.attr}"
        if f.attr == "send_task":
            return "celery.send_task"
        if f.attr in LOG_METHODS and root in LOG_ROOTS:
            return f"log.{f.attr}"
    if isinstance(f, ast.Name):
        if f.id == "send_task":
            return "celery.send_task"
        if f.id == "print":
            return "log.print"
        if f.id in RESPONSE_CALLS:
            return "response"
    return None


def _fold_str(node, str_vars: dict) -> Optional[str]:
    """Best-effort static value of a string expression: literals, local vars (from `str_vars`),
    `a + b` concatenation, and f-strings. Parts we can't pin down (a `settings.X`, an unknown name)
    become a readable `{NAME}` placeholder — honest, not a guess. Returns None when nothing at all
    is resolvable, so callers can fall back to `?`."""
    if node is None:
        return None
    s = _const_str(node)
    if s is not None:
        return s
    if isinstance(node, ast.Name):
        return str_vars.get(node.id)
    if isinstance(node, ast.Attribute):               # settings.SESSION_DOMAIN_NAME → {SESSION_DOMAIN_NAME}
        return "{" + node.attr + "}"
    if isinstance(node, ast.JoinedStr):               # f'{prefix}/send'
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                out.append(_fold_str(part.value, str_vars) or _placeholder(part.value))
            else:
                out.append("{?}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_str(node.left, str_vars)
        right = _fold_str(node.right, str_vars)
        if left is None and right is None:            # no literal/known anchor → stay honest "?"
            return None
        return (left or _placeholder(node.left)) + (right or _placeholder(node.right))
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in {"str", "int"} and node.args:
            return _fold_str(node.args[0], str_vars) or _placeholder(node.args[0])   # str(pk) → {pk}
        if isinstance(f, ast.Attribute) and f.attr in {"format", "strip", "rstrip", "lstrip",
                                                        "lower", "upper"}:
            return _fold_str(f.value, str_vars)       # URL.format(pk=pk) → keep the "…/{pk}/…" template
        # os.getenv("X") / os.environ.get("X") / getenv("X") → {X} (the env var name, a readable host)
        is_env = (isinstance(f, ast.Attribute) and f.attr == "getenv") \
            or (isinstance(f, ast.Name) and f.id == "getenv") \
            or (isinstance(f, ast.Attribute) and f.attr == "get"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "environ")
        if is_env and node.args:
            name = _const_str(node.args[0])
            if name:
                return "{" + name + "}"
    return None


def _perm_list_of(node):
    """`[Perm1, Perm2]` / `(Perm1,)` → ['Perm1', 'Perm2']; None if not a list/tuple of names."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.id if isinstance(e, ast.Name) else _dotted(e) for e in node.elts]
    return None


def _module_perm_consts(tree):
    """Module-level `NAME = [ ... ]` list constants, so `permission_classes = SOME_CONSTANT`
    resolves to the real classes instead of showing empty."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            lst = _perm_list_of(node.value)
            if lst is not None:
                out[node.targets[0].id] = lst
    return out


def _class_meta_model(classdef):
    """The `model = X` inside a class's inner `class Meta:` — the table a ModelSerializer/ModelForm
    actually writes to. Returns the model name or None."""
    for n in classdef.body:
        if isinstance(n, ast.ClassDef) and n.name == "Meta":
            for s in n.body:
                if isinstance(s, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "model" for t in s.targets):
                    if isinstance(s.value, ast.Name):
                        return s.value.id
                    if isinstance(s.value, ast.Attribute):
                        return s.value.attr
    return None


def _class_db_table(classdef):
    """The explicit `db_table = '...'` inside a model's inner `class Meta:` — the real SQL table
    name. Returns the table string or None (then callers fall back to Django's app_label_model)."""
    for n in classdef.body:
        if isinstance(n, ast.ClassDef) and n.name == "Meta":
            for s in n.body:
                if isinstance(s, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "db_table" for t in s.targets):
                    return _const_str(s.value)
    return None


def _class_tablename(classdef):
    """SQLAlchemy's `__tablename__ = '...'` class attribute — the real SQL table name for a
    declarative model (Flask-SQLAlchemy / SQLAlchemy). Returns the string or None."""
    for s in classdef.body:
        if isinstance(s, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets):
            return _const_str(s.value)
    return None


def _class_manager(classdef):
    """The custom Manager class from a model's `objects = XManager()` — so `Model.objects.<custom>()`
    can be followed into that Manager's body. Returns the class name or None. Only a direct `objects =
    <Name>()` counts (the default manager); the default `models.Manager` gives no extra methods to
    follow, so a `models.Manager()` value (an Attribute call) is intentionally ignored."""
    for s in classdef.body:
        if isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "objects" for t in s.targets) \
                and isinstance(s.value, ast.Call) and isinstance(s.value.func, ast.Name):
            return s.value.func.id
    return None


def _class_ref(node):
    """`Check` / `models.Check` / "Check" / "api.Check" → "Check"; anything else → None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split(".")[-1]
    return None


def _app_label(path):
    """Django app label from a file path: the segment right under an `apps/` directory, else the folder
    holding the models file (or its `models/` package) when that folder is a Django app (has apps.py)."""
    parts = path.replace("\\", "/").split("/")
    if "apps" in parts:
        i = parts.index("apps")
        if i + 1 < len(parts):
            return parts[i + 1]
    d = os.path.dirname(path)
    if os.path.basename(d) == "models":
        d = os.path.dirname(d)
    return os.path.basename(d) if os.path.isfile(os.path.join(d, "apps.py")) else None


def _module_str_consts(tree, base=None):
    """Top-level `NAME = <string>` constants in a module, folded to their static value (with
    {placeholder} for parts like settings.X). Accumulates so `B = A + '/x'` resolves via `A`, and
    seeds from `base` (the repo-wide table) so a constant built from an imported one resolves too."""
    consts = dict(base or {})
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            val = _fold_str(node.value, consts)
            if val is not None:
                consts[node.targets[0].id] = val
    return consts


def _placeholder(node) -> str:
    """A readable `{name}` stand-in for an expression part we can't fold to a literal."""
    if isinstance(node, ast.Name):
        return "{" + node.id + "}"
    if isinstance(node, ast.Attribute):
        return "{" + node.attr + "}"
    return "{?}"


def _dest_of(node: ast.Call, str_vars: dict = None) -> str:
    if node.args:
        a = node.args[0]
        if isinstance(a, ast.Attribute):          # settings.PAYMENT_STATUS_URL
            return _dotted(a)
        s = _const_str(a)
        if s:
            return s
        folded = _fold_str(a, str_vars or {})     # variable / 'x'+settings.Y / f-string
        if folded:
            return folded
    url_kw = _kw(node, "url")
    return url_kw or "?"


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            if isinstance(k.value, ast.Name):
                return k.value.id
            if isinstance(k.value, ast.Attribute):
                return _dotted(k.value)
            s = _const_str(k.value)
            if s:
                return s
    return None


def _first_const(call: ast.Call):
    return _const_str(call.args[0]) if call.args else None


def _is_threading_thread(f):
    return (isinstance(f, ast.Attribute) and f.attr == "Thread") or \
           (isinstance(f, ast.Name) and f.id == "Thread")


def _auth_of(cls: ast.ClassDef, index=None, _seen=None):
    """Permission/authentication class names on the class or an inherited base; None if none."""
    found = None
    for n in cls.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in {"permission_classes", "authentication_classes"}:
                    lst = _perm_list_of(n.value)                       # a literal [Perm, …]
                    if lst is None and isinstance(n.value, ast.Name) and index is not None:
                        lst = index.perm_consts.get(n.value.id)        # resolve `= SOME_CONSTANT`
                    if lst is not None:                                # resolved (possibly to empty)
                        found = (found or []) + [f"{t.id}={lst}"]
                    else:                                              # a constant/expr we can't read
                        ref = n.value.id if isinstance(n.value, ast.Name) else "?"
                        found = (found or []) + [f"{t.id}=<{ref}> unresolved"]
    if found is not None or index is None:
        return found
    # walk base classes defined in the repo (custom base views may set permissions)
    _seen = _seen or set()
    for base in cls.bases:
        bname = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        if not bname or bname in _seen:
            continue
        _seen.add(bname)
        bcls, _ = index.find_class(bname)
        if bcls is not None:
            inherited = _auth_of(bcls, index, _seen)
            if inherited is not None:
                return [f"{x} (inherited from {bname})" for x in inherited]
    return None


def analyze_handler(name: str, index: RepoIndex, near: str = "") -> Endpoint:
    ep = Endpoint(route="", handler=name)
    cls, cfile = index.find_class(name)
    if cls is None:
        # a URL routes to a module-level view (`views.ping`), never to a same-named model/class method
        # (`Check.ping`); `near` (the urls file) picks the right app when two apps share a view name
        fn, ffile = index.find_func(name, near=near, toplevel_only=True)
        if fn is None:
            fn, ffile = index.find_func(name, near=near)
        if fn is None:
            ep.e1_route_handler = Edge(UNKNOWN, note="handler not found in repo")
            for a in ("e2_auth", "e3_db_tables", "e4_external", "e5_async", "e6_pii", "e7_cache"):
                setattr(ep, a, Edge(UNKNOWN))
            return ep
        ep.file, ep.line, ep.methods = _rel(index, ffile), fn.lineno, ["<func>"]
        methods = [fn]
        auth = None
        owner_file = ffile
    else:
        ep.file, ep.line = _rel(index, cfile), cls.lineno
        methods = [m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name in HTTP_METHODS]
        ep.methods = [m.name.upper() for m in methods]
        auth = _auth_of(cls, index)
        owner_file = cfile

    ep.e1_route_handler = Edge(VERIFIED, [f"{name} @ {ep.file}:{ep.line}"])

    # E2 auth — explicit on class/base, else the resolved DRF default, else unknown
    if auth is not None:
        unresolved = any("unresolved" in a for a in auth)
        open_empty = any(a.startswith("permission_classes=[]") for a in auth)   # no perms → open
        allow_all = any("AllowAny" in a for a in auth) or open_empty
        if unresolved:
            ep.e2_auth = Edge(UNKNOWN, auth, note="permission_classes set to an unresolved constant — verify")
        else:
            ep.e2_auth = Edge(VERIFIED, auth, note="open — no permission classes" if open_empty
                              else ("explicitly open" if allow_all else ""))
    elif index.default_permissions is not None:
        dp = index.default_permissions
        ep.e2_auth = Edge(VERIFIED, [f"DEFAULT_PERMISSION_CLASSES={dp}"],
                          note="DRF settings default" + (" — open" if any("AllowAny" in x for x in dp) else ""))
    elif index.drf_present:
        # DRF with no DEFAULT_PERMISSION_CLASSES → framework default is AllowAny (open)
        ep.e2_auth = Edge(VERIFIED, ["AllowAny"],
                          note="DRF default (no DEFAULT_PERMISSION_CLASSES set → permits all)")
    else:
        ep.e2_auth = Edge(UNKNOWN, note="no permission_classes and no DRF default resolvable")

    # walk methods with 1-level follow into self-methods and module funcs
    same_module_classmethods = {m.name: m for m in (cls.body if cls else []) if isinstance(m, ast.FunctionDef)}
    build_edges(ep, methods, index, owner_file,
                self_type=(name if cls is not None else None),
                class_methods=same_module_classmethods,
                bindings=_class_bindings(index, cls, cfile) if cls is not None else None)
    return ep


def build_edges(ep, method_nodes, index, owner_file, self_type=None, class_methods=None, bindings=None):
    """Fill an Endpoint's behaviour edges (E3–E7 + resolved tables) by walking the handler's
    function bodies, following 1–2 levels into helpers. Framework-agnostic — Django resolves a
    class/func handler upstream and passes its methods here; Flask/FastAPI pass the single
    decorated function. The FactCollector + scorers below are pure Python analysis."""
    agg = FactCollector(_rel(index, owner_file), known_models=set(index.classes))
    followed = set()
    seen = set()                                    # every fact key found ANYWHERE in this endpoint
    stop = {"reason": "dried_out", "depth": 0, "first_fact": None}   # adaptive-follow outcome
    for m in method_nodes:
        _walk_follow(m, agg, index, owner_file, class_methods or {}, followed, depth=0,
                     self_type=self_type, seen=seen, dry_streak=0, stop=stop, bindings=bindings)
    ep.follow_depth = stop["depth"]                 # deepest fact-bearing hop reached (observability)
    ep.follow_stop = stop["reason"]                 # 'dried_out' complete, else possibly-incomplete
    ep.e3_db_tables = _score_db(agg)
    ep.e4_external = _score_external(agg)
    ep.e5_async = _score_async(agg)
    ep.e6_pii = _score_pii(agg)
    ep.e7_cache = _score_cache(agg)
    ep.writes_cols = [{"model": m, "cols": (sorted(c) if c is not None else None), "loc": f"{f}:{ln}"}
                      for (m, f, ln), c in sorted(agg.write_cols.items())]
    # resolve each touched model to its real SQL table (declared db_table/__tablename__, else convention)
    for model in {m for m, _k, _f, _l in agg.db if m and m != "<instance>"}:
        tbl, explicit = index.table_for(model)
        if tbl:
            ep.tables[model] = {"table": tbl, "explicit": explicit}
    return ep


# --- adaptive follow depth (was a flat MAX_FOLLOW_DEPTH=3) --------------------------------------
# Depth is no longer a magic constant. A branch deepens while it keeps revealing NEW facts and stops
# once it "dries out" — DRY_STREAK_K consecutive fact-bearing hops that add nothing new. This adapts
# to the repo, the endpoint, and its abstraction depth automatically: measured recall saturates at
# depth 4 on one service and 5 on another (measured on real Django repos), and a
# findings-driven stop reproduces each without per-repo tuning. Deeper follow is ~free — wall time is
# flat across depth because `followed` + pass-through-free already bound the work — so the ceiling is
# a loose safety net, not the primary limit.
DEPTH_CEILING = 8      # hard safety cap on fact-bearing hops; rarely the actual stop (dry-streak is)
DRY_STREAK_K = 3       # stop a branch after K fact-bearing hops that reveal NO new fact (the adaptive
                       # stop). K=3 makes the walk a provable recall-superset of the old flat depth-3.
FOLLOW_BUDGET = 250    # unchanged global backstop on total functions followed per endpoint. The
                       # `followed` set prevents re-visiting, so this only caps pathological fan-out.


def _fact_keys(local):
    """Content identity of a frame's facts — the marginal-yield signal ("did this frame teach us
    anything new?"). Excludes the volatile line number (keeps the file, for cross-file distinctness)
    and the cosmetic '*'/'(via call)' tags — computed on the RAW frame before `_tag_indirect` runs —
    so the new-vs-seen decision is a pure, hash-seed-independent function of the source."""
    keys = set()
    for (m, k, f, _ln) in local.db:        keys.add(("db", m, k.rstrip("*"), f))
    for (r, d, f, _ln) in local.external:  keys.add(("ext", r, d, f))
    for (me, t, f, _ln) in local.async_:   keys.add(("async", me, t, f))
    for (k, d, f, _ln) in local.cache:     keys.add(("cache", k.rstrip("*"), d, f))
    for (a, f, _ln) in local.pii:          keys.add(("pii", a, f))
    for (fld, snk, f, _ln) in local.leaks: keys.add(("leak", fld, snk, f))
    return keys


def _manager_followable(index, model, method):
    """True if `model.objects.<method>()` is a custom Manager method we can actually follow (the
    Manager class is in the repo and defines the method) — so the call needs no fallback read."""
    mgr = index.model_managers.get(model)
    if not mgr:
        return False
    cls, _ = index.find_class(mgr)
    return cls is not None and any(
        isinstance(m, ast.FunctionDef) and m.name == method for m in cls.body)


def _seed_from_args(callee, arg_models):
    """Map a callee's params to the concrete models passed at the call site: `fetch_all(Product)` +
    `def fetch_all(model)` → {model: Product}. Skips a leading self/cls so methods align too."""
    if not arg_models or not isinstance(callee, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    params = [a.arg for a in (callee.args.posonlyargs + callee.args.args)]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    return {params[i]: arg_models[i]
            for i in range(min(len(params), len(arg_models))) if arg_models[i]}


def _arg_bindings(fn, sets):
    """{param: config object} per distinct call site that passes one; `[{}]` when none does."""
    out = []
    for s in sets or []:
        b = _seed_from_args(fn, s)
        if b not in out:
            out.append(b)
    return out or [{}]


def _walk_follow(fn, agg, index, owner_file, classmethods, followed, depth, self_type=None,
                 seen=None, dry_streak=0, stop=None, seed_types=None, manager_model=None, bindings=None):
    """Collect facts in `fn`; recurse into the functions it calls with an ADAPTIVE, findings-driven
    depth (see the constants above). A branch keeps descending while it reveals NEW facts and brakes
    once it dries out — so deep service/repository stacks get chased and thin handlers stop early,
    with no magic depth number.

    Facts in the handler itself (depth 0) are direct; anything reached through a call (depth ≥ 1) is
    tagged "(via call)" so the report stays honest about indirection. `self_type` is the class `fn`
    belongs to, so a `self.objects.x()` inside it resolves to that class's table.

    `seen` (per-endpoint, shared) is every fact key discovered so far; `dry_streak` (per-branch, by
    value) counts consecutive fact-bearing hops that revealed nothing new; `stop` (shared) records
    the deepest hop reached and why the walk ended — for the published-blindspot report.
    """
    if seen is None:
        seen = set()
    if stop is None:
        stop = {"reason": "dried_out", "depth": 0, "first_fact": None}
    owner_rel = _rel(index, owner_file)
    local = FactCollector(owner_rel, consts=index.consts_for(owner_file),
                          class_models=index.class_models, self_type=self_type,
                          known_models=set(index.classes), manager_model=manager_model, index=index)
    if seed_types:
        local.var_types.update(seed_types)     # bind params to the models passed at the call site
    for k, b in (bindings or {}).items():      # injected models: class attrs / a config object's fields
        if isinstance(b, str):
            local.var_types[k] = b
        else:
            local.inst_fields[k] = b
            if "." not in k:
                local.instance_types[k] = b[0]
    local.visit(fn)
    # A custom-manager call (`Order.objects.import_batch()`) records NO fact at the call site — the
    # Manager is followed below and its body supplies the real reads/writes. If the Manager can't be
    # resolved (not in the repo), keep a fallback READ so the table isn't lost entirely.
    for m_model, m_method, m_fl, m_ln in local.manager_fallback:
        if not _manager_followable(index, m_model, m_method):
            tag = "" if depth == 0 else "*"
            agg.db.append((m_model, "read" + tag, local.file, m_ln))
    if depth == 0:
        agg.db += local.db
        _merge_write_cols(agg, local)
        agg.external += local.external
        agg.async_ += local.async_
        agg.cache += local.cache
        agg.pii += local.pii
        agg.leaks += local.leaks
    else:
        _tag_indirect(agg, local)      # reached via a call → mark it

    # marginal-yield signal: what did THIS frame teach us that we didn't already know? Computed on the
    # raw frame (line- and tag-independent) so it's deterministic and stable across runs.
    keys = _fact_keys(local)
    new_keys = keys - seen
    # path-local novelty: children inherit this frame's + its ANCESTORS' facts, never a SIBLING's. A
    # single shared `seen` let an earlier sibling's finds count as "already seen" for a later branch,
    # advancing that branch's dry-streak on facts it never itself saw — silently dropping the branch's
    # own unique deep reads while still labelling the endpoint 'dried_out' (complete). Forking a fresh
    # set per frame keeps each branch's dry-out decision its own, restoring the equals-unbounded recall.
    child_seen = seen | keys
    contributed = bool(keys)                       # same "has any facts" test as before
    if new_keys and depth > stop["depth"]:
        stop["depth"] = depth                      # deepest hop that actually YIELDED a new fact
    if new_keys and stop["first_fact"] is None:
        stop["first_fact"] = depth

    # Depth AND the dry-streak both treat a pass-through frame — a facade/delegator carrying no facts
    # of its own, just forwarding — as FREE: plumbing layers never spend depth or advance the streak,
    # so we reach the real data logic through any amount of indirection. A fact-bearing frame advances
    # depth; it resets the streak if it revealed something new, else it extends the dry run.
    if depth == 0:
        child_depth, child_streak = depth + 1, 0
    elif contributed:
        child_depth = depth + 1
        child_streak = 0 if new_keys else dry_streak + 1
    else:
        child_depth, child_streak = depth, dry_streak

    # dry-out is the natural, COMPLETE stop — return with no reason so the label stays 'dried_out'.
    if child_streak >= DRY_STREAK_K:
        return

    def _capped():
        """Record a possibly-incomplete stop ONLY at the point a resolvable, not-yet-followed child is
        about to be skipped for a cap — so 'ceiling'/'budget' never fires on a leaf (or an all-already-
        followed frame) that merely touched the boundary while actually being complete."""
        if child_depth > DEPTH_CEILING:
            stop["reason"] = "ceiling"
            return True
        if len(followed) >= FOLLOW_BUDGET:
            stop["reason"] = "budget"
            return True
        return False

    # self.method(...) — resolve against the current class's own methods (same self_type).
    # NOTE: all three follow loops iterate `sorted(set(...))`, not a bare `set(...)`: under the
    # shared FOLLOW_BUDGET cap, which calls get followed first decides the result, and a set's
    # iteration order depends on PYTHONHASHSEED — sorting makes the extraction deterministic.
    # A follow key carries the binding set (`_bind_sig`), so a helper reached with two different config
    # objects is analyzed once per object; with no bindings the key is exactly what it always was.
    self_b = {k: v for k, v in (bindings or {}).items() if k.startswith(("self.", "cls."))}
    for mname in sorted(set(local.self_calls)):
        meth = classmethods.get(mname)
        if meth is None:
            continue
        for arg_b in _arg_bindings(meth, local.call_arg_insts.get(f"self.{mname}")):
            sub = {**self_b, **arg_b}                # same instance → its bindings carry to siblings
            key = f"self:{owner_rel}:{mname}" + _bind_sig(sub)
            if key in followed:
                continue
            if _capped():
                return
            followed.add(key)
            _walk_follow(meth, agg, index, getattr(meth, "lc_file", owner_file), classmethods, followed,
                         child_depth, self_type=self_type,
                         seen=child_seen, dry_streak=child_streak, stop=stop,
                         seed_types=_seed_from_args(meth, local.call_arg_models.get(f"self.{mname}")),
                         bindings=sub)

    # module-level func(...) defined in the repo (no `self`). Names bound locally — nested
    # closures, params — are excluded: a call to a local `resolve()` must not bind to a same-named
    # module function in another app.
    for fname in sorted(set(local.func_calls) - local.local_names):
        node, ffile = index.find_func(fname, near=owner_file, toplevel_only=True)
        if node is None:
            continue
        for arg_b in _arg_bindings(node, local.call_arg_insts.get(fname)):
            key = f"func:{fname}" + _bind_sig(arg_b)
            if key in followed:
                continue
            if _capped():
                return
            followed.add(key)
            _walk_follow(node, agg, index, ffile, {}, followed, child_depth,
                         seen=child_seen, dry_streak=child_streak, stop=stop,
                         seed_types=_seed_from_args(node, local.call_arg_models.get(fname)), bindings=arg_b)

    # Model.classmethod(...) / Service().method(...) — recurse with THAT class's methods,
    # so a `self.x()` inside the service resolves correctly at the next level down.
    # Fallback: if `cname` is not a class but IS a relative module import (`from . import rows`),
    # treat `rows.fn()` as a bare module-level call into that sibling module.
    mod_imports = index.imports_for(owner_file)
    for cname, mname in sorted(set(local.typed_calls)):
        # resolve an instance variable to the class it holds: `profile_helper.validate()` where
        # `profile_helper = cus_api_helper.UpdateProfileFlow(req)` → follow UpdateProfileFlow.validate.
        target_cls = local.instance_types.get(cname) or local.var_types.get(cname) or cname
        key = f"{target_cls}.{mname}"
        cls, cfile = index.find_class(target_cls, near=owner_file)
        if cls is not None:
            method = next((m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == mname), None)
            if method is None:
                if mname in _VALIDATION_ENTRY:           # `form.is_valid()` runs clean() / clean_<field>()
                    inh = _inherited_methods(index, target_cls)
                    for vname, vm in sorted(inh.items()):
                        if vname not in _VALIDATORS and not vname.startswith(_VALIDATOR_PREFIXES):
                            continue
                        vkey = f"{target_cls}.{vname}"
                        if vkey in followed:
                            continue
                        if _capped():
                            return
                        followed.add(vkey)
                        _walk_follow(vm, agg, index, getattr(vm, "lc_file", cfile), inh, followed, child_depth,
                                     self_type=target_cls, seen=child_seen, dry_streak=child_streak, stop=stop)
                continue
            # the receiver's bindings: its class attrs, under the fields of the config object it holds
            inst = local.inst_fields.get(cname)
            recv_b = {**_class_bindings(index, cls, cfile),
                      **({f"self.{k}": v for k, v in inst[1].items()} if inst else {})}
            cls_methods = {m.name: m for m in cls.body if isinstance(m, ast.FunctionDef)}
            for arg_b in _arg_bindings(method, local.call_arg_insts.get(f"{cname}.{mname}")):
                sub = {**recv_b, **arg_b}
                bkey = key + _bind_sig(sub)
                if bkey in followed:
                    continue
                if _capped():
                    return
                followed.add(bkey)
                _walk_follow(method, agg, index, cfile, cls_methods, followed, child_depth, self_type=target_cls,
                             seen=child_seen, dry_streak=child_streak, stop=stop,
                             seed_types=_seed_from_args(method, local.call_arg_models.get(f"{cname}.{mname}")),
                             bindings=sub)
        elif cname in mod_imports:
            # `cname` is a repo module (`from . import cname` / `from pkg import cname`); a module-level call
            mod_file = mod_imports[cname]
            node, ffile = index.find_func(mname, near=mod_file, toplevel_only=True)
            if node is None:
                continue
            for arg_b in _arg_bindings(node, local.call_arg_insts.get(key)):
                bkey = key + _bind_sig(arg_b)
                if bkey in followed:
                    continue
                if _capped():
                    return
                followed.add(bkey)
                _walk_follow(node, agg, index, ffile, {}, followed, child_depth,
                             seen=child_seen, dry_streak=child_streak, stop=stop,
                             seed_types=_seed_from_args(node, local.call_arg_models.get(key)), bindings=arg_b)

    # Model.objects.<custom>() — a custom Manager method. Resolve the model's Manager class and follow
    # its body with `manager_model` set to the managed Model, so `self.create()` inside surfaces as a
    # real write to that model's table (not the fallback read the call site would otherwise guess).
    for cname, mname in sorted(set(local.manager_calls)):
        key = f"manager:{cname}.{mname}"
        if key in followed:
            continue
        mgr = index.model_managers.get(cname)
        if not mgr:
            continue
        mcls, mfile = index.find_class(mgr, near=owner_file)
        if mcls is None:
            continue
        method = next((m for m in mcls.body if isinstance(m, ast.FunctionDef) and m.name == mname), None)
        if method is None:
            continue
        if _capped():
            return
        followed.add(key)
        mcls_methods = {m.name: m for m in mcls.body if isinstance(m, ast.FunctionDef)}
        _walk_follow(method, agg, index, mfile, mcls_methods, followed, child_depth, self_type=mgr,
                     seen=child_seen, dry_streak=child_streak, stop=stop,
                     manager_model=cname)     # cname is the managed Model — `self.create()` writes it


def _merge_write_cols(agg, sub):
    for k, v in sub.write_cols.items():
        if k in agg.write_cols:
            cur = agg.write_cols[k]
            agg.write_cols[k] = None if (cur is None or v is None) else (cur | v)
        else:
            agg.write_cols[k] = v


def _tag_indirect(agg, sub):
    # facts discovered one level down are still real, but flagged as inter-procedural
    agg.db += [(m, k + "*", f, ln) for (m, k, f, ln) in sub.db]
    _merge_write_cols(agg, sub)
    agg.external += [(r, d + " (via call)", f, ln) for (r, d, f, ln) in sub.external]
    agg.async_ += [(mech + " (via call)", t, f, ln) for (mech, t, f, ln) in sub.async_]
    agg.cache += [(k + "*", d, f, ln) for (k, d, f, ln) in sub.cache]
    agg.pii += sub.pii
    agg.leaks += sub.leaks     # a leak fully inside a helper is still an intra-procedural leak


def _first_loc(seen, key, f, ln):
    """Keep the first source location seen for a fact key."""
    if key not in seen:
        seen[key] = f"{f}:{ln}"


def _score_db(agg) -> Edge:
    if not agg.db:
        return Edge(NA)
    seen, indirect, unknown = {}, False, False
    for model, kind, f, ln in agg.db:
        ind = kind.endswith("*")
        indirect = indirect or ind
        unknown = unknown or model == "<instance>"
        _first_loc(seen, f"{model}:{kind.rstrip('*')}{' (via call)' if ind else ''}", f, ln)
    items = [f"{fact} @ {loc}" for fact, loc in seen.items()]
    status = POTENTIAL if (indirect or unknown) else VERIFIED
    return Edge(status, items, note="write hidden in manager/service" if indirect else "")


def _score_external(agg) -> Edge:
    if not agg.external:
        return Edge(NA)
    seen = {}
    via = cfg = partial = unk = False
    for r, d, f, ln in agg.external:
        via = via or "via call" in d
        clean = d.replace(" (via call)", "")
        cfg = cfg or clean.startswith("settings.")
        unk = unk or clean == "?"           # nothing at all resolved → blunt ?
        partial = partial or "{" in clean   # host/path with a runtime {placeholder} → known-but-partial
        _first_loc(seen, f"{r} -> {d}", f, ln)
    items = [f"{fact} @ {loc}" for fact, loc in seen.items()]
    # never a clean ✓ when the destination isn't fully pinned down: verify by hand.
    status = POTENTIAL if (via or cfg or partial or unk) else VERIFIED
    note = "; ".join(x for x in ["indirect" if via else "", "host in settings" if cfg else "",
                                 "dest partially resolved" if partial else "",
                                 "dest unresolved" if unk else ""] if x)
    return Edge(status, items, note=note)


def _score_async(agg) -> Edge:
    if not agg.async_:
        return Edge(NA)
    seen = {}
    for mech, t, f, ln in agg.async_:
        _first_loc(seen, f"{mech} -> {t}", f, ln)
    items = [f"{fact} @ {loc}" for fact, loc in seen.items()]
    return Edge(POTENTIAL, items, note="pattern-detected; not in a Celery-only view")


def _score_cache(agg) -> Edge:
    if not agg.cache:
        return Edge(NA)
    seen, indirect, unk = {}, False, False
    for kind, detail, f, ln in agg.cache:
        ind = kind.endswith("*")
        indirect = indirect or ind
        unk = unk or "{" in detail or detail.endswith(" ?")   # key not fully pinned down
        _first_loc(seen, f"{kind.rstrip('*')} {detail}{' (via call)' if ind else ''}", f, ln)
    items = [f"{fact} @ {loc}" for fact, loc in seen.items()]
    status = POTENTIAL if (indirect or unk) else VERIFIED
    return Edge(status, items, note="via helper" if indirect else "")


PERSON_MODELS = {"User", "Profile", "UserProfile", "Student", "Customer", "Account",
                 "Applicant", "Lead", "Contact", "Address"}


def _score_pii(agg) -> Edge:
    seen = {}
    for attr, f, ln in agg.pii:
        _first_loc(seen, attr, f, ln)
    for model, kind, f, ln in agg.db:
        if model in PERSON_MODELS and kind.rstrip("*").startswith("read"):
            _first_loc(seen, f"reads {model}", f, ln)
    # Split traced leaks by WHERE the PII goes. A sensitive field reaching a THIRD PARTY — an external
    # API, a log, a task queue — is the real egress concern (✓). The same field merely returned to the
    # CLIENT in the response is worth surfacing but is a weaker signal (⚠): ~85% of "traced leaks"
    # across the repos we measured are just an endpoint handing a caller back its own data, and lumping those
    # in with third-party egress buried the ~3-per-1000 that actually matter.
    offplatform, client = {}, {}
    for field, sink, f, ln in agg.leaks:
        _first_loc(client if sink == "response" else offplatform, f"{field} → {sink}", f, ln)
    egress = bool(agg.external or agg.async_)
    if not seen and not offplatform and not client:
        return Edge(NA)
    src_items = [f"{fact} @ {loc}" for fact, loc in seen.items()]
    off_items = [f"{fact} @ {loc}" for fact, loc in offplatform.items()]
    cli_items = [f"{fact} @ {loc}" for fact, loc in client.items()]
    if offplatform:
        # a sensitive field flows OFF-PLATFORM (external call / log / queue) — the real leak.
        return Edge(VERIFIED, off_items + cli_items + src_items,
                    note="traced: a sensitive field flows off-platform (external / log / task queue)")
    if client:
        # PII flows back to the client in the response — real, but not a third-party leak.
        return Edge(POTENTIAL, cli_items + src_items,
                    note="a sensitive field is returned to the client — verify the caller is authorised")
    if egress:
        # source and an egress path co-occur but no flow was traced between them → honestly potential
        return Edge(POTENTIAL, src_items,
                    note="PII source co-occurs with an egress path — object-level taint not traced")
    return Edge(UNKNOWN, src_items, note="PII source read; downstream egress not proven")


def _rel(index, path):
    return os.path.relpath(path, index.root) if path else ""


# ---- top-level -----------------------------------------------------------------------

# ---- non-HTTP entrypoints ---------------------------------------------------------------
# Celery tasks, management commands and signal receivers write the same tables as HTTP views but route
# discovery can't see them. Each becomes an entrypoint analyzed by the SAME handler analyzer; its `kind`
# tells consumers it isn't network-reachable, so auth is n/a — never an "unauthenticated" finding.
_TASK_DECOS = {"shared_task", "task", "periodic_task", "app_task"}      # celery @shared_task / @app.task
_SIGNAL_DECOS = {"receiver"}                                           # django @receiver(post_save, …)
DJANGO_SIGNALS = {"pre_save", "post_save", "pre_delete", "post_delete", "m2m_changed", "pre_init",
                  "post_init", "pre_migrate", "post_migrate", "request_started", "request_finished",
                  "got_request_exception", "user_logged_in", "user_logged_out", "user_login_failed"}


def _signal_connect(node):
    """`post_save.connect(handler, sender=Order)` → ("handler", "post_save", "Order"), else None. Only
    Django's built-in signals, so `ssh.connect(host)` / `conn.connect()` are never taken for wiring."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "connect"
            and node.args and isinstance(node.args[0], ast.Name)):
        return None
    sig = _dotted(node.func.value).split(".")[-1]
    if sig not in DJANGO_SIGNALS:
        return None
    sender = next((_dotted(k.value).split(".")[-1] for k in node.keywords if k.arg == "sender"), "")
    return node.args[0].id, sig, sender


def _deco_names(node):
    names = set()
    for d in getattr(node, "decorator_list", []):
        f = d.func if isinstance(d, ast.Call) else d
        names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def _receiver_trigger(node):
    """`@receiver(post_save, sender=Order)` → "post_save:Order" — what fires the handler."""
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and _dotted(d.func).split(".")[-1] == "receiver" and d.args:
            sig = _dotted(d.args[0]).split(".")[-1] or "signal"
            sender = next((_dotted(k.value).split(".")[-1] for k in d.keywords if k.arg == "sender"), "")
            return f"{sig}:{sender}" if sender else sig
    return "signal"


def _job_ep(index, kind, route, name, method_nodes, path, trigger, self_type=None, class_methods=None,
            line=None, owner_file=None, via="", bindings=None):
    """`path`/`line` is where the entrypoint is declared; `owner_file` is where its code lives, when
    that differs (a command whose handle() is inherited from a base in another module)."""
    ep = Endpoint(route=route, handler=name, kind=kind)
    ep.file, ep.line, ep.methods = _rel(index, path), line or method_nodes[0].lineno, [trigger]
    ep.e1_route_handler = Edge(VERIFIED, [f"{name} @ {ep.file}:{ep.line}"] + ([via] if via else []))
    ep.e2_auth = Edge(NA, note=f"not reachable over HTTP — runs as a {kind}")
    build_edges(ep, method_nodes, index, owner_file or path, self_type=self_type,
                class_methods=class_methods or {}, bindings=bindings)
    return ep


def _inherited_methods(index, cls_name, _stack=()):
    """{name: def} of repo class `cls_name` plus what it inherits from repo bases — nearest class wins.
    Each def is tagged with its own module (`lc_file`), so a followed self.x() resolves and reports there."""
    cls, cfile = index.find_class(cls_name)
    if cls is None or cls_name in _stack:
        return {}
    out = {}
    for b in reversed(cls.bases):
        bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
        if bn:
            out.update(_inherited_methods(index, bn, _stack + (cls_name,)))
    for m in cls.body:
        if isinstance(m, ast.FunctionDef):
            m.lc_file = cfile
            out[m.name] = m
    return out


def job_entrypoints(index):
    """Celery tasks, management-command handle() methods and signal receivers (`@receiver` and
    `post_save.connect(h, sender=M)`), each analyzed like an endpoint. Best-effort: one bad def never
    sinks the run. `methods` carries the trigger (celery / manage.py / post_save:Order)."""
    out, seen = [], {}

    def add(kind, route, name, nodes, path, trigger, **kw):
        if (route, path) in seen:                  # one receiver wired to several senders (or decorated AND
            ep = seen[(route, path)]               # connected) → one entrypoint that lists every trigger
            if trigger not in ep.methods:
                ep.methods.append(trigger)
            return
        try:
            ep = _job_ep(index, kind, route, name, nodes, path, trigger, **kw)
        except Exception:
            return
        seen[(route, path)] = ep
        out.append(ep)

    for name, defs in index.funcs.items():                 # tasks + decorated receivers are functions
        for node, path, _toplevel in defs:
            decos = _deco_names(node)
            if decos & _TASK_DECOS:
                add("task", f"task:{name}", name, [node], path, "celery")
            elif decos & _SIGNAL_DECOS:
                add("signal", f"signal:{name}", name, [node], path, _receiver_trigger(node))
    for handler, sig, sender in index.signal_connects:
        node, path = index.find_func(handler, toplevel_only=True)
        if node is not None:
            add("signal", f"signal:{handler}", handler, [node], path, f"{sig}:{sender}" if sender else sig)
    for cname, defs in index.classes.items():              # management commands → their handle()
        for node, path in defs:
            if "management" + os.sep + "commands" not in path:
                continue
            if not any(_dotted(b).split(".")[-1].endswith("Command") for b in node.bases):
                continue
            methods = {m.name: m for m in node.body if isinstance(m, ast.FunctionDef)}
            handle = methods.get("handle") or methods.get("handle_noargs")
            kw = {}
            if handle is None:              # a thin subclass whose handle() comes from a shared base command
                for b in node.bases:
                    bn = _dotted(b).split(".")[-1]
                    handle, hfile = _find_method(index, bn, "handle")
                    if handle is not None:
                        for m in methods.values():
                            m.lc_file = path
                        methods = {**_inherited_methods(index, bn), **methods}   # subclass overrides win
                        kw = dict(line=node.lineno, owner_file=hfile,
                                  via=f"handle() inherited from {bn} @ {_rel(index, hfile)}:{handle.lineno}")
                        break
            if handle is None:
                continue
            cmd = os.path.splitext(os.path.basename(path))[0]      # Django: command name == file stem
            add("command", f"command:{cmd}", cname, [handle], path, "manage.py", self_type=cname,
                class_methods=methods, bindings=_class_bindings(index, node, path), **kw)
    return out


def analyze_repo(root: str, index=None):
    index = index or RepoIndex(root)
    results = []
    # Django / DRF — routes come from urls.py, handlers resolved by name
    for route, handler, urls_file in parse_endpoints(index):
        ep = analyze_handler(handler, index, near=urls_file)
        ep.route = route
        results.append(ep)
    # Flask / FastAPI — routes come from decorators on the handler function itself
    try:
        import frameworks
        results.extend(frameworks.discover(index))
    except Exception:
        try:
            from extractor import frameworks   # packaged import path
            results.extend(frameworks.discover(index))
        except Exception:
            pass                                # decorator frameworks are additive; never break Django
    results.extend(job_entrypoints(index))      # celery tasks / management commands / signal receivers
    # dedupe by (route, handler, file) — a repo could mix frameworks
    seen, out = set(), []
    for ep in results:
        key = (ep.route, ep.handler, ep.file)
        if key not in seen:
            seen.add(key)
            out.append(ep)
    return out


def to_dict(ep: Endpoint) -> dict:
    d = asdict(ep)
    return d
