"""The hover glossary (web/glossary.js): every key a page uses must exist (a typo = a silent missing
tooltip), every entry must say something, and the file must reach both the live server and the
self-contained .html exports (opened from file://, no server to fetch it from)."""
import os
import re
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import diff_pr
import map_repo
import serve

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
PAGES = ["app.html", "map.html", "crossrepo.html", "viewer.template.html"]
# key families used by the pages — a quoted string with one of these prefixes must be a glossary key
FAMILIES = ("sev-", "ch-", "ent-", "op-", "tag-", "post-", "map-", "x-", "inv-", "node-", "nav-", "st-",
            "egress-", "tabs-", "mode-", "ref-")
NOT_KEYS = {"inv-discovery"}            # a panel id that happens to share a family prefix


def _glossary():
    src = open(os.path.join(WEB, "glossary.js"), encoding="utf-8").read()
    return {k: (t, d) for k, t, d in re.findall(r'^\s+"([a-z0-9-]+)": \{t: "(.*?)", d: "(.+?)"\}', src, re.M)}


def test_every_entry_has_a_title_and_a_meaning():
    g = _glossary()
    assert len(g) > 100
    assert all(t.strip() and d.strip() for t, d in g.values())


def test_every_key_a_page_uses_exists():
    g = _glossary()
    missing = []
    for page in PAGES:
        html = open(os.path.join(WEB, page), encoding="utf-8").read()
        used = set(re.findall(r'data-g="([a-z0-9-]+)"', html))                       # literal attributes
        used |= set(re.findall(r'(?:dataset\.g=|lbl\([^,]+,)"([a-z0-9-]+)"', html))  # set from JS
        # key-map values / arrays — not a class/id attribute, not a dict KEY (`"x":`)
        used |= {s for s in re.findall(r'(?<!class=)(?<!id=)"([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"(?!\s*:)', html)
                 if s.startswith(FAMILIES) and s not in NOT_KEYS}
        missing += [f"{page}: {k}" for k in sorted(used - set(g))]
        assert 'src="glossary.js"' in html, page
    assert not missing, missing


def test_glossary_js_is_served():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(
        {"repo": "", "invariants": "", "token": "", "allowed": [], "base_path": "", "preload": {},
         "demos": {}, "demo_dir": ""}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = HTTPConnection(*httpd.server_address, timeout=10)
        c.request("GET", "/glossary.js")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200 and "javascript" in r.getheader("Content-Type") and "LC_GLOSSARY" in body
    finally:
        httpd.shutdown()


def test_html_exports_inline_the_glossary(tmp_path):
    assert "LC_GLOSSARY" in diff_pr.inline_glossary('<script src="glossary.js"></script>')
    out = tmp_path / "map.html"
    map_repo.write_html({"repo": "x", "total": 0, "shown": 0, "apps": [], "kinds": {}}, str(out))
    html = out.read_text(encoding="utf-8")
    assert "LC_GLOSSARY" in html and 'src="glossary.js"' not in html
