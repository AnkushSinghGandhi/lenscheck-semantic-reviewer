"""provenance.py — the QDD bridge (prototype).

Turns a repo's statically-extracted facts into the two things QDD can't compute on its own (it sees the
database, never the code):

  1. CODE PROVENANCE  — table -> which endpoints write it / read it ("what code created this data?").
  2. CONSISTENCY RULES — candidate data-integrity rules the code implies, in QDD's native format:
       * FK orphan checks   (every payments.lead_id must exist in leads.id)      — high confidence
       * write-without-relation anomalies (an endpoint writes A but never touches its FK target B)

Consumes only the free stdlib extractor. Output: a rich JSON on stdout, and a QDD-ready
`consistency_rules.yaml` snippet (--yaml FILE). Run:  python3 provenance.py <repo> [--yaml rules.yaml]
"""
import argparse
import ast
import json
import os
import sys
import warnings

# keep stdout pure JSON: the extractor parses the target repo and may emit SyntaxWarnings — silence them
# (progress/errors go to stderr) so a tool shelling out to us can `json.loads(stdout)` unconditionally.
warnings.filterwarnings("ignore", category=SyntaxWarning)

from extractor import analyze_repo          # noqa: E402
from extractor.analyzer import RepoIndex    # noqa: E402

# Stable subprocess contract. Bump only on a breaking change to the JSON shape; consumers (e.g. QDD)
# check `schema` and refuse an unknown major. Exit codes: 0 ok · 2 usage · 3 repo unreadable.
SCHEMA = 1

_FK_CALLS = {"ForeignKey", "OneToOneField"}


def _fk_target(call):
    """The related model of a `models.ForeignKey(Target, ...)` field — a Name, a 'app.Model' string, or
    'self'. Returns the bare model name (or None if it can't be read statically)."""
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Name):
        return a.id
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        s = a.value
        if s in ("self",):
            return "self"
        return s.split(".")[-1]          # 'app_label.Model' -> 'Model'
    if isinstance(a, ast.Attribute):     # module.Model
        return a.attr
    return None


def _fk_edges(index):
    """Scan every model class for ForeignKey/OneToOneField fields → (SrcModel, column, TargetModel)."""
    edges = []
    for name, entries in index.classes.items():
        for node, _file in entries:
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                    continue
                f = stmt.value.func
                fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if fname not in _FK_CALLS:
                    continue
                if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                    continue
                field = stmt.targets[0].id
                target = _fk_target(stmt.value)
                if target == "self":
                    target = name
                if not target:
                    continue
                # Django's column for `lead = ForeignKey(Lead)` is `lead_id`, unless db_column= overrides
                col = field + "_id"
                for kw in stmt.value.keywords:
                    if kw.arg == "db_column" and isinstance(kw.value, ast.Constant):
                        col = kw.value.value
                edges.append((name, col, target))
    return edges


def _item_model_kind(item):
    """'Payment:write (via call) @ f:1' -> ('Payment','write')."""
    left = item.split(" @ ")[0].replace(" (via call)", "").strip()
    model, _, kind = left.rpartition(":")
    return (model or left), (kind or "read")


def build(repo):
    eps = analyze_repo(repo)
    index = RepoIndex(repo)

    # model -> db_table, merged from the index (explicit Meta.db_table / __tablename__) and what the
    # endpoints resolved. Gives FK targets a real table name to point a rule at.
    model_table = dict(index.model_tables)
    for e in eps:
        for m, info in (e.tables or {}).items():
            model_table.setdefault(m, info.get("table"))

    def table_of(name):
        return model_table.get(name, name)     # a raw-SQL name is already a table

    # 1) provenance: table -> {written_by, read_by}
    tables = {}
    for e in eps:
        ep_ref = {"route": e.route, "handler": e.handler, "methods": e.methods,
                  "loc": f"{e.file}:{e.line}"}
        for it in (e.e3_db_tables.items or []):
            model, kind = _item_model_kind(it)
            if model in ("<instance>",):
                continue
            tbl = table_of(model)
            rec = tables.setdefault(tbl, {"models": set(), "written_by": [], "read_by": []})
            if model != tbl:
                rec["models"].add(model)
            bucket = "written_by" if kind.startswith("write") else "read_by"
            if ep_ref not in rec[bucket]:
                rec[bucket].append(ep_ref)
    for rec in tables.values():
        rec["models"] = sorted(rec["models"])

    # 2) consistency rules
    rules = []
    fk_edges = _fk_edges(index)
    seen = set()
    for src_model, col, tgt_model in fk_edges:
        src_t, tgt_t = table_of(src_model), table_of(tgt_model)
        if not src_t or not tgt_t or src_t == tgt_t:
            continue
        key = (src_t, col, tgt_t)
        if key in seen:
            continue
        seen.add(key)
        rules.append({
            "id": f"fk-orphan::{src_t}.{col}->{tgt_t}",
            "kind": "referential_integrity",
            "confidence": "high",
            "statement": f"every `{src_t}.{col}` must reference an existing `{tgt_t}.id`",
            "source_fact": f"{src_model}.{col} ForeignKey -> {tgt_model}",
            "qdd": {"name": f"{src_t} references a valid {tgt_t}",
                    "source": {"table": src_t, "key": col},
                    "target": {"table": tgt_t, "key": "id"},
                    "expect": "exists"},
        })

    # 3) write-without-relation anomaly: an endpoint writes A but never touches A's FK target B
    def touched(ep, tbl):
        for it in (ep.e3_db_tables.items or []):
            m, _ = _item_model_kind(it)
            if table_of(m) == tbl:
                return True
        return False

    anomalies = []
    for src_model, col, tgt_model in fk_edges:
        src_t, tgt_t = table_of(src_model), table_of(tgt_model)
        if not src_t or not tgt_t or src_t == tgt_t:
            continue
        writers = [e for e in eps if any(
            table_of(_item_model_kind(it)[0]) == src_t and _item_model_kind(it)[1].startswith("write")
            for it in (e.e3_db_tables.items or []))]
        for e in writers:
            if not touched(e, tgt_t):
                anomalies.append({
                    "id": f"write-without-relation::{e.handler}::{src_t}-no-{tgt_t}",
                    "kind": "co_occurrence_anomaly",
                    "confidence": "medium",
                    "statement": (f"`{e.handler}` ({e.route}) writes `{src_t}` but never reads/writes its "
                                  f"FK target `{tgt_t}` — verify the link is set"),
                    "endpoint": {"route": e.route, "handler": e.handler, "loc": f"{e.file}:{e.line}"},
                })
    # de-dup anomalies by id
    seen_a, uniq = set(), []
    for a in anomalies:
        if a["id"] not in seen_a:
            seen_a.add(a["id"]); uniq.append(a)

    return {
        "schema": SCHEMA,
        "repo": os.path.abspath(repo),
        "generated_by": "lenscheck provenance",
        "summary": {"endpoints": len(eps), "tables": len(tables),
                    "fk_edges": len(fk_edges), "orphan_rules": len(rules),
                    "anomalies": len(uniq)},
        "tables": {t: {"models": r["models"],
                       "written_by": r["written_by"], "read_by": r["read_by"]}
                   for t, r in sorted(tables.items())},
        "consistency_rules": rules + uniq,
    }


def _to_yaml(rules):
    """Emit the FK orphan rules as a QDD-ready consistency_rules.yaml (no yaml dep — hand-rendered)."""
    out = ["# generated by lenscheck provenance — drop into QDD's local/consistency_rules.yaml", ""]
    for r in rules:
        if r["kind"] != "referential_integrity":
            continue
        q = r["qdd"]
        out += [f"- name: {q['name']!r}",
                f"  source: {{table: {q['source']['table']}, key: {q['source']['key']}}}",
                f"  target: {{table: {q['target']['table']}, key: {q['target']['key']}}}",
                f"  expect: {q['expect']}", ""]
    return "\n".join(out)


def main(argv=None):
    """Subprocess contract for tools that shell out to us (e.g. QDD): pure JSON on stdout, human text on
    stderr, exit 0 on success. A consumer can safely `json.loads(subprocess stdout)`."""
    ap = argparse.ArgumentParser(
        prog="lenscheck provenance",
        description="Emit table→endpoint code provenance + candidate data-consistency rules as JSON.")
    ap.add_argument("repo", help="path to the application's source repo (read-only, never executed)")
    ap.add_argument("--yaml", metavar="FILE", help="also write the FK rules as a QDD consistency_rules.yaml")
    ap.add_argument("--compact", action="store_true", help="single-line JSON (default is indented)")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.repo):
        print(f"lenscheck provenance: not a directory: {a.repo}", file=sys.stderr)
        return 3
    try:
        data = build(a.repo)
    except Exception as e:                       # never leak a traceback onto stdout the consumer parses
        print(f"lenscheck provenance: failed to analyze {a.repo}: {e}", file=sys.stderr)
        return 3

    if a.yaml:
        try:
            with open(a.yaml, "w", encoding="utf-8") as f:
                f.write(_to_yaml(data["consistency_rules"]))
        except OSError as e:
            print(f"lenscheck provenance: could not write {a.yaml}: {e}", file=sys.stderr)
            return 3

    s = data["summary"]
    print(f"lenscheck provenance: {s['endpoints']} endpoints · {s['tables']} tables · "
          f"{s['orphan_rules']} orphan rules · {s['anomalies']} anomalies", file=sys.stderr)
    print(json.dumps(data, separators=(",", ":")) if a.compact else json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
