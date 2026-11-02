"""The 6-edge lens on a fixture Django app. Lenscheck parses the fixtures with `ast`; it never
imports or runs them, so Django/DRF don't need to be installed."""
import ast

from conftest import BLOG, facts, by_route
from extractor import analyze_repo
from extractor.analyzer import FactCollector


def eps():
    return analyze_repo(BLOG)


def test_endpoints_discovered():
    routes = {e.route for e in eps()}
    assert any("api/orders" in r for r in routes)
    assert any("api/stats" in r for r in routes)
    assert any("open-write" in r for r in routes)


def test_route_and_auth_resolved():
    co = by_route(eps(), "api/orders")
    assert co.e1_route_handler.status == "✓"          # E1: route → handler
    assert co.handler == "CreateOrder"
    assert co.e2_auth.status == "✓"                    # E2: auth
    assert "IsAuthenticated" in " ".join(co.e2_auth.items)


def test_db_edge_direct_and_via_call():
    co = by_route(eps(), "api/orders")
    f = facts(co, "e3_db_tables")
    assert "Order:write" in f                          # direct ORM write
    assert "OrderItem:write" in f                      # write hidden in a model helper (follow)
    assert "User:read" in f                            # read (also the PII source)
    # a write reached via a call keeps the endpoint honest (⚠, not a clean ✓)
    assert co.e3_db_tables.status in ("⚠", "✓")


def test_external_via_service_resolves_settings_name():
    co = by_route(eps(), "api/orders")
    f = facts(co, "e4_external")
    assert any("requests.post" in x for x in f)        # E4: external call (found via service)
    assert any("settings.PAYMENT_URL" in x for x in f)  # destination resolved to the config name


def test_async_dispatch_detected():
    co = by_route(eps(), "api/orders")
    assert any("threading.Thread" in x for x in facts(co, "e5_async"))  # E5


def test_pii_flagged_potential_not_safe():
    co = by_route(eps(), "api/orders")
    # PII source (email / User) co-occurs with an egress path → potential, never a false "safe"
    assert co.e6_pii.status in ("⚠", "?")
    assert co.e6_pii.status != "✓"


def test_permission_classes_constant_resolves(tmp_path):
    # `permission_classes = SOME_CONSTANT` (even imported) resolves to the real classes, not empty
    from diff_pr import is_open
    (tmp_path / "perms.py").write_text("PERMS = [ApiKeyPermission, IsAuthenticated]\nOPEN_PERMS = []\n")
    (tmp_path / "views.py").write_text(
        "from rest_framework.views import APIView\nfrom .perms import PERMS, OPEN_PERMS\n"
        "class Guarded(APIView):\n    permission_classes = PERMS\n    def get(self, r): return None\n"
        "class Wide(APIView):\n    permission_classes = OPEN_PERMS\n    def get(self, r): return None\n")
    (tmp_path / "urls.py").write_text(
        "from django.urls import path\nfrom .views import Guarded, Wide\n"
        "urlpatterns = [path('g', Guarded.as_view()), path('w', Wide.as_view())]\n")
    eps = {e.route: e for e in analyze_repo(str(tmp_path))}
    guarded = " ".join(eps["g"].e2_auth.items)
    assert "ApiKeyPermission" in guarded and "IsAuthenticated" in guarded
    assert not is_open(eps["g"])                       # real perms → not open
    assert is_open(eps["w"])                           # constant resolves to [] → open


def test_permission_classes_unresolved_constant_is_unknown(tmp_path):
    # a permission constant defined nowhere → honest UNKNOWN, never a silent empty/✓
    (tmp_path / "views.py").write_text(
        "from rest_framework.views import APIView\n"
        "class V(APIView):\n    permission_classes = MYSTERY_PERMS\n    def get(self, r): return None\n")
    (tmp_path / "urls.py").write_text(
        "from django.urls import path\nfrom .views import V\nurlpatterns = [path('v', V.as_view())]\n")
    v = next(e for e in analyze_repo(str(tmp_path)) if e.route == "v")
    assert v.e2_auth.status == "?"
    assert any("unresolved" in x for x in v.e2_auth.items)


def test_drf_default_permission_is_open():
    ps = by_route(eps(), "api/stats")
    # no permission_classes + REST_FRAMEWORK without DEFAULT_PERMISSION_CLASSES → AllowAny (open)
    assert "AllowAny" in " ".join(ps.e2_auth.items)


def test_explicit_allow_any_write_is_open():
    ow = by_route(eps(), "open-write")
    assert "AllowAny" in " ".join(ow.e2_auth.items)
    assert "Order:write" in facts(ow, "e3_db_tables")


def _external_dests(src):
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    return [dest for _root, dest, _f, _l in fc.external]


def _db_models(src):
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    return {model for model, _kind, _f, _l in fc.db}


def test_cache_and_session_ops_are_not_db_writes():
    # `cache.delete(k)` / `request.session.save()` are not ORM writes → no <instance> table
    src = ("from django.core.cache import cache\n"
           "class H:\n"
           "    def f(self, request):\n"
           "        cache.delete('k')\n"
           "        request.session.save()\n")
    assert _db_models(src) == set()


def _cache_edge(src):
    from extractor.analyzer import _score_cache
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    agg = FactCollector("m.py")
    agg.cache = fc.cache
    return _score_cache(agg)


def test_cache_lens_reads_and_writes_surface_as_e7():
    src = ("from django.core.cache import cache\n"
           "class H:\n"
           "    def f(self, pk):\n"
           "        v = cache.get('stats')\n"
           "        cache.set('user:' + str(pk), 1)\n"
           "        cache.delete('lock')\n")
    e = _cache_edge(src)
    joined = " | ".join(e.items)
    assert "read get stats" in joined
    assert "write set user:{pk}" in joined     # str(pk) unwrapped to {pk}
    assert "write delete lock" in joined


def test_cache_edge_is_na_when_no_cache_used():
    assert _cache_edge("class H:\n    def f(self):\n        return 1\n").status == "n/a"


def test_instance_write_resolves_via_param_annotation():
    src = ("class H:\n"
           "    def f(self, obj: EntityLock):\n"
           "        obj.save()\n")
    assert _db_models(src) == {"EntityLock"}


def test_instance_write_resolves_via_self_attr_and_tuple():
    src = ("class H:\n"
           "    def f(self):\n"
           "        self.row = Widget()\n"
           "        self.row.save()\n"
           "        obj, created = Gadget.objects.get_or_create(id=1)\n"
           "        obj.save()\n")
    assert _db_models(src) == {"Widget", "Gadget"}


def test_model_db_table_resolves_declared_and_convention(tmp_path):
    # each model resolves to its real SQL table: declared Meta.db_table, else app_label_model
    app = tmp_path / "apps" / "shop"
    app.mkdir(parents=True)
    (app / "models.py").write_text(
        "class Thing(models.Model):\n    class Meta:\n        db_table = 'custom_thing'\n"
        "class Plain(models.Model):\n    pass\n")
    (app / "views.py").write_text(
        "from rest_framework.views import APIView\n"
        "class V(APIView):\n    def get(self, r):\n        Thing.objects.all()\n        Plain.objects.all()\n        return None\n")
    (app / "urls.py").write_text(
        "from django.urls import path\nfrom .views import V\nurlpatterns = [path('v', V.as_view())]\n")
    ep = next(e for e in analyze_repo(str(tmp_path)) if e.route == "v")
    assert ep.tables["Thing"] == {"table": "custom_thing", "explicit": True}      # declared
    assert ep.tables["Plain"] == {"table": "shop_plain", "explicit": False}       # Django convention


def test_serializer_save_resolves_to_meta_model_not_class():
    # `s = WebinarSerializer(...); s.save()` writes to the serializer's Meta.model, not a
    # table named "WebinarSerializer"
    fc = FactCollector("m.py", class_models={"WebinarSerializer": "Webinar"})
    fc.visit(ast.parse("class H:\n"
                       "    def f(self):\n"
                       "        s = WebinarSerializer(data=1)\n"
                       "        s.save()\n"))
    assert {m for m, _k, _f, _l in fc.db} == {"Webinar"}


def test_unmapped_serializer_is_unknown_not_a_fake_table():
    # a *Serializer we can't map to a model must not appear as a table named "...Serializer"
    fc = FactCollector("m.py", class_models={})
    fc.visit(ast.parse("class H:\n"
                       "    def f(self):\n"
                       "        s = MysterySerializer(data=1)\n"
                       "        s.save()\n"))
    assert {m for m, _k, _f, _l in fc.db} == {"<instance>"}


def test_self_objects_resolves_to_enclosing_model(tmp_path):
    # `cls.objects.filter()` inside a model method → the model's table, not a fake table named "self"
    (tmp_path / "models.py").write_text(
        "class Question(models.Model):\n"
        "    class Meta:\n        db_table = 'questions'\n"
        "    @classmethod\n    def recent(cls):\n        return cls.objects.filter(active=1)\n")
    (tmp_path / "views.py").write_text(
        "from rest_framework.views import APIView\n"
        "class V(APIView):\n    def get(self, r):\n        Question.recent()\n        return None\n")
    (tmp_path / "urls.py").write_text(
        "from django.urls import path\nfrom .views import V\nurlpatterns = [path('v', V.as_view())]\n")
    v = next(e for e in analyze_repo(str(tmp_path)) if e.route == "v")
    models = {x.split(":")[0] for x in v.e3_db_tables.items}
    assert "Question" in models and "self" not in models and "cls" not in models


def test_self_objects_unknown_enclosing_is_instance_not_self():
    # no enclosing class known → honest <instance>, never a table literally named "self"
    fc = FactCollector("m.py", self_type=None)
    fc.visit(ast.parse("class H:\n    def f(self):\n        self.objects.all()\n"))
    assert {m for m, _k, _f, _l in fc.db} == {"<instance>"}


def test_unresolved_instance_write_stays_honest():
    # a receiver we genuinely can't type is still surfaced as <instance>:write, never dropped
    src = ("class H:\n"
           "    def f(self):\n"
           "        obj = build_thing()\n"
           "        obj.save()\n")
    assert "<instance>" in _db_models(src)


def test_external_dest_folds_var_and_concat():
    # `requests.post(url)` where url = 'lit' + settings.X + 'lit' → resolved, not a bare "?"
    src = ("import requests\n"
           "from django.conf import settings\n"
           "def send():\n"
           "    url = 'https://mailer-service' + settings.SESSION_DOMAIN_NAME + '/api/1/send'\n"
           "    requests.post(url, data=1)\n")
    assert _external_dests(src) == ["https://mailer-service{SESSION_DOMAIN_NAME}/api/1/send"]


def test_external_dest_folds_fstring_var():
    src = ("import requests\n"
           "def send(base):\n"
           "    url = f'{base}/hook'\n"
           "    requests.post(url)\n")
    assert _external_dests(src) == ["{base}/hook"]


def test_external_dest_resolves_format_and_getenv():
    # URL = os.getenv('X') + '/path/{id}' ; requests.post(URL.format(id=id)) → readable, not "?"
    src = ("import os, requests\n"
           "BASE = os.getenv('API_URL', '')\n"
           "URL = BASE + '/v1/item/{id}/'\n"
           "def f(id):\n"
           "    requests.post(URL.format(id=id))\n")
    assert _external_dests(src) == ["{API_URL}/v1/item/{id}/"]


def test_module_str_consts_accumulate():
    from extractor.analyzer import _module_str_consts
    tree = ast.parse("base = 'https://x'\nAPI = base + '/v1'\n")
    assert _module_str_consts(tree) == {"base": "https://x", "API": "https://x/v1"}


def test_imported_url_constant_resolves_across_modules(tmp_path):
    # a common cross-module shape: a URL constant defined in one module, imported and used in another
    (tmp_path / "service_urls.py").write_text(
        "import os\nBASE = os.getenv('API_URL', '')\nSET_KW = BASE + '/api/1/kw/{pk}/set/'\n")
    (tmp_path / "settings.py").write_text(
        "REST_FRAMEWORK = {'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny']}\n")
    (tmp_path / "views.py").write_text(
        "import requests\nfrom rest_framework.views import APIView\nfrom .service_urls import SET_KW\n"
        "class V(APIView):\n    def post(self, r, pk):\n        requests.post(SET_KW.format(pk=pk))\n        return None\n")
    (tmp_path / "urls.py").write_text(
        "from django.urls import path\nfrom .views import V\nurlpatterns = [path('api/v/<int:pk>', V.as_view())]\n")
    eps = analyze_repo(str(tmp_path))
    v = next(e for e in eps if "api/v" in e.route)
    dests = [x.split(" -> ", 1)[-1].split(" @ ")[0] for x in v.e4_external.items]
    assert "{API_URL}/api/1/kw/{pk}/set/" in dests     # imported constant + .format resolved


def test_external_dest_unresolvable_stays_unknown():
    # a destination built from a function call is not guessed → honest "?"
    src = ("import requests\n"
           "def send():\n"
           "    requests.post(build_url())\n")
    assert _external_dests(src) == ["?"]


def _external_status(src):
    from extractor.analyzer import _score_external
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    agg = FactCollector("m.py")
    agg.external = fc.external
    return _score_external(agg).status


def test_external_status_partial_dest_is_not_verified():
    # a {placeholder} host (runtime-dependent) is known-but-partial → ⚠, never a clean ✓
    src = ("import requests\n"
           "from django.conf import settings\n"
           "def send():\n"
           "    url = 'https://x' + settings.Y + '/send'\n"
           "    requests.post(url)\n")
    assert _external_status(src) == "⚠"


def test_external_status_literal_dest_is_verified():
    src = ("import requests\n"
           "def send():\n"
           "    requests.post('https://api.stripe.com/v1/charges')\n")
    assert _external_status(src) == "✓"


# ---- raw SQL (cursor.execute / .raw) --------------------------------------------------

def test_sql_tables_unit_reads_writes_and_subquery():
    from extractor.analyzer import _sql_tables
    read = _sql_tables("SELECT a.x FROM foo a JOIN bar b ON b.id = a.id "
                       "-- comment mentioning FROM nothing\n WHERE a.x = %s")
    assert read == [("foo", "read"), ("bar", "read")]                 # FROM + JOIN, comment ignored
    assert _sql_tables("INSERT INTO audit_log (msg) VALUES (%s)") == [("audit_log", "write")]
    assert _sql_tables("DELETE FROM sessions WHERE id = %s") == [("sessions", "write")]
    # a FROM (subquery) opener has no table identifier → skipped, only the real outer table counts
    assert _sql_tables("SELECT * FROM (SELECT 1) t JOIN users u ON u.id = t.id") == [("users", "read")]
    assert _sql_tables("just a plain string, not sql") == []          # conservative: no SQL, no tables


def test_sql_tables_cte_alias_is_not_a_table():
    from extractor.analyzer import _sql_tables
    # `WITH recent AS (…)` defines a query-local alias; a later FROM/JOIN recent references the CTE,
    # NOT a table — it must not be hallucinated (regression: the torture fixture caught this).
    got = _sql_tables("WITH recent AS (SELECT id FROM orders WHERE created > %s) "
                      "SELECT r.id, p.amount FROM recent r JOIN payments p ON p.order_id = r.id")
    assert got == [("orders", "read"), ("payments", "read")]          # orders (in the CTE) + payments
    assert ("recent", "read") not in got                             # the CTE name is NOT a table
    # multiple CTEs
    multi = _sql_tables("WITH a AS (SELECT 1 FROM users), b AS (SELECT 1 FROM orders) "
                        "SELECT * FROM a JOIN b ON 1=1 JOIN payments p ON 1=1")
    assert set(multi) == {("users", "read"), ("orders", "read"), ("payments", "read")}


def test_sql_tables_update_subquery_schema():
    from extractor.analyzer import _sql_tables
    # UPDATE target is a write; a table read inside its subquery is a read
    assert _sql_tables("UPDATE payments SET amount=%s WHERE order_id IN "
                       "(SELECT id FROM orders WHERE status=%s)") == [("payments", "write"), ("orders", "read")]
    # schema-qualified name → the table part; alias ignored
    assert _sql_tables("SELECT * FROM analytics.events e JOIN users u ON u.id=e.uid") \
        == [("events", "read"), ("users", "read")]


def test_raw_execute_extracts_tables_from_module_constant():
    # the real-world shape: a `_SQL = """…"""` module constant run via `cursor.execute(_SQL, …)`.
    # The constant is seeded into str_vars exactly as the repo index does, so it must resolve.
    sql = "SELECT c.uid FROM ad_target_slice c JOIN users u ON u.uid = c.uid"
    src = ("def rows(cursor):\n"
           "    cursor.execute(_SQL, [1, 2])\n")
    fc = FactCollector("m.py", consts={"_SQL": sql})
    fc.visit(ast.parse(src))
    got = {(model, kind) for model, kind, _f, _l in fc.db}
    assert got == {("ad_target_slice", "read"), ("users", "read")}


def test_raw_sql_sensitive_column_is_a_pii_source():
    # `SELECT u.email …` in raw SQL reads PII the ORM lens would miss → recorded as a PII source
    src = ("def rows(cursor):\n"
           "    cursor.execute('SELECT u.email, u.phone FROM users u WHERE u.id = %s', [1])\n")
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    labels = {a for a, _f, _l in fc.pii}
    assert "email" in labels and "phone" in labels
    # a non-PII column must not be flagged
    fc2 = FactCollector("m.py")
    fc2.visit(ast.parse("def r(cur):\n    cur.execute('SELECT id, name FROM users')\n"))
    assert fc2.pii == []


def test_raw_execute_ignores_non_sql_strings():
    # `.execute()` / `.raw()` exist on non-DB objects too — a non-SQL arg must not invent tables
    src = ("def f(runner):\n"
           "    runner.execute('do the thing')\n"
           "    thing.raw('not sql either')\n")
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    assert fc.db == []


# ---- FK-traversal read is stamped at the kwarg line, not the .filter() call line ------

def test_lowercase_orm_receiver_is_not_a_table_name():
    # a lowercase `.objects` receiver is a variable (param / runtime `apps.get_model`), never a model
    # class — it must not invent a table literally named `model` / `table_class`.
    src = ("def handler(model):\n"                       # `model` is a param → dynamic
           "    model.objects.filter(pk__in=[1])\n"
           "    inst = table_class.objects.get(id=1)\n"   # lowercase local → not a type
           "    inst.save()\n")
    fc = FactCollector("m.py")
    fc.visit(ast.parse(src))
    models = {m for m, _k, _f, _l in fc.db}
    assert "model" not in models and "table_class" not in models    # no fake table names
    assert models == {"<instance>"}                                  # honest unknown, read + write


def test_class_alias_assignment_resolves_to_real_model():
    # `link_model = SynonymLink` (a bare class alias) → a later `.objects` names the real model
    src = ("def handler():\n"
           "    link_model = SynonymLink\n"
           "    link_model.objects.filter(status=1)\n")
    fc = FactCollector("m.py", known_models={"SynonymLink"})
    fc.visit(ast.parse(src))
    assert {m for m, _k, _f, _l in fc.db} == {"SynonymLink"}


def test_fk_traversal_read_uses_kwarg_line_not_call_line():
    # `Child.objects.filter(parent__col=…)` reads BOTH Child (the call) and Parent (the FK join).
    # They must not share a source line, or clicking the FK'd table jumps to the parent's query.
    src = ("class V:\n"
           "    def get(self, r):\n"
           "        return Child.objects.filter(\n"           # line 3: the .filter( call
           "            name='x',\n"
           "            parent__added__gte=r.a,\n"            # line 5: the FK kwarg
           "        )\n")
    fc = FactCollector("m.py", known_models={"Child", "Parent"})
    fc.visit(ast.parse(src))
    lines = {model: ln for model, _kind, _f, ln in fc.db}
    assert lines["Child"] == 3                                # the parent query owns the call line
    assert lines["Parent"] == 5                               # the FK read points at its own kwarg
    assert lines["Child"] != lines["Parent"]


def test_reverse_fk_manager_resolves_to_the_related_model(tmp_path):
    # `check.flip_set...` is Django's default reverse-FK manager → a read of Flip; an unknown stem
    # (`check.thing_set`) must record nothing rather than invent a table
    import os
    from extractor import analyze_repo
    os.makedirs(tmp_path / "app")
    (tmp_path / "app/models.py").write_text(
        "from django.db import models\n"
        "class Check(models.Model):\n    pass\n"
        "class Flip(models.Model):\n    owner = models.ForeignKey(Check, on_delete=models.CASCADE)\n")
    (tmp_path / "app/urls.py").write_text(
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('c/', views.details)]\n")
    (tmp_path / "app/views.py").write_text(
        "from .models import Check\n"
        "def details(request):\n"
        "    check = Check.objects.get(id=1)\n"
        "    flips = list(check.flip_set.order_by('-id'))\n"
        "    other = check.thing_set.all()\n"
        "    check.flip_set.create()\n"
        "    return flips\n")
    ep = [e for e in analyze_repo(str(tmp_path)) if e.handler == "details"][0]
    facts = {it.split(" @ ")[0] for it in ep.e3_db_tables.items}
    assert {"Check:read", "Flip:read", "Flip:write"} <= facts
    assert not any(f.lower().startswith("thing") for f in facts)


def test_url_resolves_to_the_module_view_not_a_same_named_method(tmp_path):
    import os
    from extractor import analyze_repo
    os.makedirs(tmp_path / "app")
    (tmp_path / "app/models.py").write_text(
        "from django.db import models\nclass Check(models.Model):\n    def ping(self):\n        self.save()\n")
    (tmp_path / "app/urls.py").write_text(
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('p/', views.ping)]\n")
    (tmp_path / "app/views.py").write_text(
        "from .models import Check\ndef ping(request):\n    return Check.objects.count()\n")
    ep = [e for e in analyze_repo(str(tmp_path)) if e.route == "p/"][0]
    assert ep.file.endswith("views.py"), f"resolved to {ep.file}"


def _mini_repo(tmp_path, models, view):
    import os
    os.makedirs(tmp_path / "app", exist_ok=True)
    (tmp_path / "app/models.py").write_text(models)
    (tmp_path / "app/urls.py").write_text(
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('v/', views.v)]\n")
    (tmp_path / "app/views.py").write_text(view)
    from extractor import analyze_repo
    ep = [e for e in analyze_repo(str(tmp_path)) if e.route == "v/"][0]
    return {it.split(" @ ")[0].replace(" (via call)", "") for it in ep.e3_db_tables.items}


MODELS = ("from django.db import models\n"
          "class Project(models.Model):\n"
          "    def bump(self):\n        Project.objects.filter(id=self.id).update(n=1)\n"
          "class Tag(models.Model):\n    pass\n"
          "class Link(models.Model):\n    pass\n"
          "class Check(models.Model):\n"
          "    project = models.ForeignKey(Project, on_delete=models.CASCADE)\n"
          "    tags = models.ManyToManyField(Tag)\n"
          "    links = models.ManyToManyField(Tag, through='Link', related_name='+')\n"
          "    class Meta:\n        db_table = 'checks'\n"
          "    def create_flip(self):\n        Flip.objects.create(owner=self)\n"
          "class Flip(models.Model):\n    owner = models.ForeignKey(Check, on_delete=models.CASCADE)\n")


def test_method_on_a_fetched_model_instance_is_followed(tmp_path):
    facts = _mini_repo(tmp_path, MODELS, "from .models import Check\ndef v(request):\n"
                       "    check = Check.objects.get(id=1)\n    check.create_flip()\n")
    assert "Flip:write" in facts


def test_method_through_an_fk_field_is_followed(tmp_path):
    facts = _mini_repo(tmp_path, MODELS, "from django.shortcuts import get_object_or_404\nfrom .models import Check\n"
                       "def v(request):\n    check = get_object_or_404(Check, id=1)\n    check.project.bump()\n")
    assert "Project:write" in facts


def test_m2m_add_writes_the_join_table_and_all_reads_the_target(tmp_path):
    facts = _mini_repo(tmp_path, MODELS, "from .models import Check\ndef v(request):\n"
                       "    check = Check.objects.get(id=1)\n    check.tags.add(1)\n"
                       "    check.links.remove(2)\n    return list(check.tags.all())\n")
    assert "checks_tags:write" in facts            # Django's default <owner table>_<field>
    assert "Link:write" in facts                   # explicit through= model
    assert "Tag:read" in facts
    assert not any(f.startswith("<instance>") for f in facts)


def test_reverse_m2m_set_writes_the_join_table_not_the_other_model(tmp_path):
    models = ("from django.db import models\n"
              "class Check(models.Model):\n"
              "    class Meta:\n        db_table = 'checks'\n"
              "    def assign_all_channels(self):\n"
              "        self.channel_set.set(Channel.objects.all())\n"
              "class Channel(models.Model):\n    checks = models.ManyToManyField(Check)\n"
              "    class Meta:\n        db_table = 'channels'\n")
    facts = _mini_repo(tmp_path, models, "from .models import Check\ndef v(request):\n"
                       "    check = Check.objects.get(id=1)\n    check.assign_all_channels()\n")
    assert "channels_checks:write" in facts and "Channel:read" in facts
    assert "Channel:write" not in facts


def test_default_table_uses_the_django_app_folder(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/apps.py").write_text("")
    facts = _mini_repo(tmp_path, "from django.db import models\nclass Tag(models.Model):\n    pass\n"
                       "class Note(models.Model):\n    tags = models.ManyToManyField(Tag)\n",
                       "from .models import Note\ndef v(request):\n    n = Note.objects.get(id=1)\n    n.tags.add(1)\n")
    assert "app_note_tags:write" in facts


HELPER_MODELS = ("from django.db import models\n"
                 "class Check(models.Model):\n"
                 "    project = models.ForeignKey('Project', on_delete=models.CASCADE)\n"
                 "    def create_flip(self):\n        Flip.objects.create(owner=self)\n"
                 "    @property\n    def visible_pings(self) -> 'QuerySet[Ping]':\n        return self.ping_set.all()\n"
                 "class Flip(models.Model):\n    owner = models.ForeignKey(Check, on_delete=models.CASCADE)\n"
                 "class Ping(models.Model):\n    owner = models.ForeignKey(Check, on_delete=models.CASCADE)\n"
                 "class Channel(models.Model):\n    checks = models.ManyToManyField(Check)\n"
                 "class Project(models.Model):\n    pass\n"
                 "class Profile(models.Model):\n    pass\n")


def test_helper_with_a_return_annotation_types_the_caller_variable(tmp_path):
    # the body is opaque (a registry lookup): only the `-> Check` annotation can say what comes back
    view = ("from .models import Check\nimport registry\n"
            "def _get_check(request, code) -> Check:\n    return registry.fetch(code)\n"
            "def v(request):\n    check = _get_check(request, 1)\n    check.create_flip()\n")
    assert "Flip:write" in _mini_repo(tmp_path, HELPER_MODELS, view)


def test_tuple_returning_helper_types_the_unpacked_names(tmp_path):
    # annotation form, and the un-annotated `return check, True` form with a typed queryset variable
    annotated = ("from .models import Check\n"
                 "def _get(request) -> tuple[Check, bool]:\n    raise NotImplementedError\n"
                 "def v(request):\n    check, rw = _get(request)\n    check.create_flip()\n")
    assert "Flip:write" in _mini_repo(tmp_path, HELPER_MODELS, annotated)
    plain = ("from django.shortcuts import get_object_or_404\nfrom .models import Check\n"
             "def _get(request, code):\n    q = Check.objects.select_related('project')\n"
             "    check = get_object_or_404(q, id=code)\n    if request.user.is_superuser:\n        return check, True\n"
             "    return check, False\n"
             "def v(request):\n    check, rw = _get(request, 1)\n    check.create_flip()\n")
    import shutil
    shutil.rmtree(tmp_path / "app")
    assert "Flip:write" in _mini_repo(tmp_path, HELPER_MODELS, plain)


def test_optional_and_queryset_annotations_are_looked_through():
    import ast
    from extractor.analyzer import _anno_model
    for src, want in [("Check", "Check"), ("Check | None", "Check"), ("Optional[Check]", "Check"),
                      ("QuerySet[Check]", "Check"), ("Union[Check, None]", "Check"),
                      ("Union[Check, Flip]", None), ("list[Check]", None), ("int", None)]:
        assert _anno_model(ast.parse(src, mode="eval").body) == want, src


def test_request_attribute_declared_on_the_request_class_types_the_write(tmp_path):
    import os
    os.makedirs(tmp_path / "app", exist_ok=True)
    (tmp_path / "app/http.py").write_text(
        "from .models import Profile\nclass AuthedRequest:\n    profile: Profile\n")
    view = ("from .http import AuthedRequest\ndef v(request: AuthedRequest):\n    request.profile.save()\n")
    facts = _mini_repo(tmp_path, HELPER_MODELS, view)
    assert "Profile:write" in facts and not any(f.startswith("<instance>") for f in facts)


def test_property_returning_a_queryset_reads_the_declared_model(tmp_path):
    view = ("from .models import Check\ndef v(request):\n    check = Check.objects.get(id=1)\n"
            "    return list(check.visible_pings.order_by('-id'))\n")
    assert "Ping:read" in _mini_repo(tmp_path, HELPER_MODELS, view)


def test_form_is_valid_follows_the_clean_methods(tmp_path):
    view = ("from django import forms\nfrom .models import Check, Flip\n"
            "class F(forms.Form):\n    def clean_code(self):\n        return Check.objects.get(id=1)\n"
            "    def clean(self):\n        Flip.objects.filter(id=1).exists()\n"
            "    def save_all(self):\n        Ping.objects.create()\n"
            "def v(request):\n    form = F(request.POST)\n    if form.is_valid():\n        return 1\n")
    facts = _mini_repo(tmp_path, HELPER_MODELS, view)
    assert {"Check:read", "Flip:read"} <= facts
    assert "Ping:write" not in facts                 # only clean / clean_* run on validation


def test_annotate_count_reads_the_related_table(tmp_path):
    view = ("from django.db.models import Count\nfrom .models import Channel\ndef v(request):\n"
            "    return list(Channel.objects.annotate(n=Count('checks')))\n")
    assert "Check:read" in _mini_repo(tmp_path, HELPER_MODELS, view)
    import shutil
    shutil.rmtree(tmp_path / "app")
    plain = ("from django.db.models import Count\nfrom .models import Channel\ndef v(request):\n"
             "    return list(Channel.objects.annotate(n=Count('name')))\n")
    assert "Check:read" not in _mini_repo(tmp_path, HELPER_MODELS, plain)      # not a relation: nothing invented


PROP_MODELS = ("from django.db import models\n"
               "class Profile(models.Model):\n"
               "    def num_checks_used(self):\n        return Check.objects.filter(owner=self).count()\n"
               "    def num_available(self):\n        return 10 - self.num_checks_used()\n"
               "class Project(models.Model):\n"
               "    owner = models.ForeignKey('Profile', on_delete=models.CASCADE)\n"
               "    @property\n    def owner_profile(self) -> Profile:\n        return Profile.objects.get(id=self.owner_id)\n"
               "    def available(self):\n        return self.owner_profile.num_available()\n"
               "class Channel(models.Model):\n    project = models.ForeignKey(Project, on_delete=models.CASCADE)\n"
               "class Check(models.Model):\n    owner = models.ForeignKey(Profile, on_delete=models.CASCADE)\n")


def test_reading_a_property_runs_its_body(tmp_path):
    # `channel.project.owner_profile` is an attribute read that queries the profile table
    view = ("from .models import Channel\ndef v(request):\n    channel = Channel.objects.get(id=1)\n"
            "    return channel.project.owner_profile\n")
    assert "Profile:read" in _mini_repo(tmp_path, PROP_MODELS, view)


def test_method_on_a_property_result_is_followed_through_the_chain(tmp_path):
    # self.owner_profile → Profile (declared `-> Profile`) → .num_available() → num_checks_used() → Check
    view = ("from .models import Project\ndef v(request):\n    project = Project.objects.get(id=1)\n"
            "    return project.available()\n")
    facts = _mini_repo(tmp_path, PROP_MODELS, view)
    assert {"Project:read", "Profile:read", "Check:read"} <= facts


def test_plain_attribute_reads_and_unannotated_properties_invent_nothing(tmp_path):
    models = ("from django.db import models\n"
              "class Thing(models.Model):\n"
              "    name = models.CharField(max_length=5)\n"
              "    @property\n    def label(self):\n        return self.name.upper()\n"
              "class Other(models.Model):\n    pass\n")
    view = ("from .models import Thing\ndef v(request):\n    t = Thing.objects.get(id=1)\n"
            "    return t.name + t.label\n")
    assert _mini_repo(tmp_path, models, view) == {"Thing:read"}


REL_MODELS = ("from django.db import models\n"
              "from django.contrib.auth.models import User\n"
              "class Credential(models.Model):\n"
              "    user = models.ForeignKey(User, models.CASCADE, related_name='credentials')\n"
              "class Profile(models.Model):\n"
              "    user = models.OneToOneField(User, models.CASCADE)\n"
              "    token = models.CharField(max_length=9)\n"
              "class Hidden(models.Model):\n"
              "    user = models.ForeignKey(User, models.CASCADE, related_name='+')\n"
              "class Note(models.Model):\n"
              "    user = models.ForeignKey(User, models.CASCADE)\n")


def test_reverse_manager_with_a_related_name_reads_the_source_model(tmp_path):
    view = ("from django.contrib.auth.models import User\ndef v(request):\n"
            "    user = User.objects.get(id=1)\n    return list(user.credentials.values_list('id'))\n")
    assert "Credential:read" in _mini_repo(tmp_path, REL_MODELS, view)


def test_default_reverse_manager_name_still_resolves_for_a_typed_owner(tmp_path):
    view = ("from django.contrib.auth.models import User\ndef v(request):\n"
            "    user = User.objects.get(id=1)\n    return user.note_set.count()\n")
    assert "Note:read" in _mini_repo(tmp_path, REL_MODELS, view)


def test_reverse_one_to_one_accessor_reads_and_types_the_source_model(tmp_path):
    view = ("from django.contrib.auth.models import User\ndef v(request):\n"
            "    user = User.objects.get(id=1)\n    user.profile.token = ''\n    user.profile.save()\n")
    facts = _mini_repo(tmp_path, REL_MODELS, view)
    assert {"Profile:read", "Profile:write"} <= facts
    assert not any(f.startswith("<instance>") for f in facts)


def test_a_hidden_or_unknown_reverse_accessor_invents_nothing(tmp_path):
    view = ("from django.contrib.auth.models import User\ndef v(request):\n"
            "    user = User.objects.get(id=1)\n    a = user.hidden_set\n    b = user.hidden\n    c = user.nothing.filter(x=1)\n")
    assert _mini_repo(tmp_path, REL_MODELS, view) == {"User:read"}


def test_self_save_in_a_model_method_names_the_model_but_not_a_service_class(tmp_path):
    models = ("from django.db import models\n"
              "class Profile(models.Model):\n    token = models.CharField(max_length=9)\n"
              "    def prepare(self):\n        self.token = 'x'\n        self.save()\n"
              "class Service:\n    def save(self):\n        return 1\n    def run(self):\n        self.save()\n")
    facts = _mini_repo(tmp_path, models, "from .models import Profile, Service\ndef v(request):\n"
                       "    p = Profile.objects.get(id=1)\n    p.prepare()\n    Service().run()\n")
    assert "Profile:write" in facts
    assert "Service:write" not in facts                      # a service's own save() is a method call, not a table
