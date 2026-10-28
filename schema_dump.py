"""Optional DB-schema input for `lenscheck crossrepo`. managed=False / ORM-less tables keep their
unique indexes in the DB (manual migrations), invisible to source parsing. Dump them to a
schema.json so crossrepo can emit column-level "coordinate on unique key (a, b)" rules — the
precise form of the cross-service consistency hazard. Client-side (needs DB access), MySQL/MariaDB.

Format: {table: [[col, ...], ...]}  — a list of unique-key column-tuples per table.
"""
import json


def uniques_from_show_index(rows, colnames):
    """`SHOW INDEX FROM t` rows -> list of unique column-tuples (excludes PRIMARY)."""
    ki, ci, si, ni = (colnames.index(c) for c in
                      ("Key_name", "Column_name", "Seq_in_index", "Non_unique"))
    by_key = {}
    for r in rows:
        if int(r[ni]) != 0 or r[ki] == "PRIMARY":
            continue
        by_key.setdefault(r[ki], []).append((int(r[si]), r[ci]))
    return [tuple(c for _, c in sorted(cols)) for cols in by_key.values()]


def dump(tables, cursor, out_path=None):
    """Run `SHOW INDEX` per table through an open DB-API cursor; return/write {table: [[cols]]}.
    For non-MySQL engines, emit the same shape from information_schema yourself."""
    out = {}
    for t in tables:
        try:
            cursor.execute("SHOW INDEX FROM " + t)
        except Exception:
            continue                                   # table absent on this env — skip
        cols = [d[0] for d in cursor.description]
        keys = uniques_from_show_index(cursor.fetchall(), cols)
        if keys:
            out[t] = [list(k) for k in keys]
    if out_path:
        json.dump(out, open(out_path, "w", encoding="utf-8"), indent=2)
    return out


def load(path):
    """schema.json -> {table: [tuple(cols), ...]}. {} if the path is empty/missing."""
    if not path:
        return {}
    raw = json.load(open(path, encoding="utf-8"))
    return {t: [tuple(k) for k in keys] for t, keys in raw.items()}
