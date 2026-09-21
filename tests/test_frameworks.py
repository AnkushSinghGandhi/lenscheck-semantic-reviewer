"""Flask & FastAPI extraction — the decorator-routed path (analyzer + frameworks).

Verifies routes, methods, prefixes, auth, SQLAlchemy DB (via __tablename__), external calls and
traced PII egress, plus a Django regression so the shared engine still behaves.
Run: `cd prism && python3 -m pytest tests/test_frameworks.py -q`
"""
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor import analyze_repo, to_dict  # noqa: E402


def _write(root, rel, src):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(textwrap.dedent(src))


def _by_route(root):
    return {ep.route: to_dict(ep) for ep in analyze_repo(root)}


def test_fastapi(tmp_path):
    root = str(tmp_path)
    _write(root, "models.py", """
        from sqlalchemy.orm import declarative_base
        Base = declarative_base()
        class User(Base):
            __tablename__ = "auth_users"
    """)
    _write(root, "main.py", """
        from fastapi import FastAPI, APIRouter, Depends
        from .models import User
        app = FastAPI()
        router = APIRouter(prefix="/users")
        def current_user(): ...
        @router.get("/{uid}")
        def get_user(uid: int, user=Depends(current_user)):
            return session.query(User).get(uid)
        @router.post("/pay")
        async def pay(uid: int):
            u = session.query(User).get(uid)
            import requests
            requests.post("https://paytm.example.com/charge", json={"email": u.email})
        app.include_router(router)
    """)
    eps = _by_route(root)
    assert "/users/{uid}" in eps and "/users/pay" in eps        # method-shortcut + router prefix
    get = eps["/users/{uid}"]
    assert get["e2_auth"]["status"] == "✓"                      # Depends() → auth seen
    assert get["e3_db_tables"]["status"] == "✓"                 # SQLAlchemy session.query(User)
    pay = eps["/users/pay"]
    assert "POST" in pay["methods"]
    assert pay["e2_auth"]["status"] == "?"                      # honest: no auth on the pay route
    assert pay["e4_external"]["status"] == "✓"                  # requests.post
    assert pay["e6_pii"]["status"] == "✓"                       # traced: email → requests.post


def test_flask(tmp_path):
    root = str(tmp_path)
    _write(root, "app.py", """
        from flask import Blueprint, Flask
        app = Flask(__name__)
        bp = Blueprint("api", __name__, url_prefix="/api")
        def login_required(f): return f
        class Order:
            __tablename__ = "orders"
            query = None
        @bp.route("/orders", methods=["GET", "POST"])
        @login_required
        def orders():
            return Order.query.all()
        @bp.route("/leak")
        def leak():
            import requests
            email = request.args.get("email")
            requests.post("https://ext.example.com", data={"email": email})
        app.register_blueprint(bp)
    """)
    eps = _by_route(root)
    assert "/api/orders" in eps                                 # blueprint url_prefix applied
    orders = eps["/api/orders"]
    assert set(orders["methods"]) == {"GET", "POST"}            # methods= kwarg
    assert orders["e2_auth"]["status"] == "✓"                   # @login_required
    assert orders["e3_db_tables"]["status"] == "✓"              # Model.query → read
    leak = eps["/api/leak"]
    assert leak["e2_auth"]["status"] == "?"                     # honest unknown
    assert leak["e6_pii"]["status"] == "✓"                      # request arg → requests.post traced


def test_django_regression(tmp_path):
    root = str(tmp_path)
    _write(root, "apps/orders/models.py", """
        from django.db import models
        class Order(models.Model):
            class Meta:
                db_table = "orders_tbl"
    """)
    _write(root, "apps/orders/views.py", """
        from rest_framework.views import APIView
        from rest_framework.permissions import IsAuthenticated
        from .models import Order
        class OrderView(APIView):
            permission_classes = [IsAuthenticated]
            def get(self, request):
                return Order.objects.filter(active=True)
    """)
    _write(root, "apps/orders/urls.py", """
        from django.urls import path
        from .views import OrderView
        urlpatterns = [path("api/orders/", OrderView.as_view())]
    """)
    eps = _by_route(root)
    assert "api/orders/" in eps
    ov = eps["api/orders/"]
    assert ov["e2_auth"]["status"] == "✓"                       # DRF permission_classes
    assert ov["e3_db_tables"]["status"] == "✓"                  # Django ORM still works
    assert ov["tables"]["Order"]["table"] == "orders_tbl"       # Meta.db_table resolved
