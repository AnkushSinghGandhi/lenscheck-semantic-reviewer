"""factindex.py — on-disk fact cache keyed by file content.

`load(repo)` returns exactly what `provenance.build(repo)` returns. If no .py file changed, added or
removed since the last run it serves the saved facts without parsing anything; otherwise it does a full
rebuild (always correct — no partial re-analysis). Any cache trouble just falls back to a rebuild."""
import hashlib
import json
import os
import tempfile

import provenance                                                       # noqa: E402
from extractor.analyzer import RepoIndex, analyze_repo, SKIP_DIRS      # noqa: E402

SCHEMA = 1


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _engine_fp():
    import crossrepo          # lazy: crossrepo imports factindex, so a top-level import would be circular
    return crossrepo._engine_fingerprint()


def _cache_dir(repo):
    base = os.environ.get("LENSCHECK_INDEX_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "lenscheck", "index")
    return os.path.join(base, hashlib.blake2b(os.path.abspath(repo).encode(), digest_size=8).hexdigest())


def _hash(path):
    with open(path, "rb") as f:
        return hashlib.blake2b(f.read(), digest_size=16).hexdigest()


def _write(d, name, obj):
    """Atomic write (temp file in the same dir, then replace). True on success; a failure is swallowed."""
    tmp = None
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, os.path.join(d, name))
        return True
    except (OSError, TypeError, ValueError):
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _snapshot(repo):
    """{rel: (mtime_ns, size)} for every indexed .py file; a file that can't be stat'd is skipped."""
    cur = {}
    for p in _iter_py(repo):
        try:
            st = os.stat(p)
        except OSError:
            continue
        cur[os.path.relpath(p, repo)] = (st.st_mtime_ns, st.st_size)
    return cur


def _cached(d, key, cur, repo):
    """The saved facts if nothing changed since they were written, else None."""
    try:
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        with open(os.path.join(d, "facts.json"), encoding="utf-8") as f:
            facts = json.load(f)
        if manifest["key"] != key:
            return None
        files = manifest["files"]
        if set(files) != set(cur):          # a file was added or removed
            return None
        touched = False
        for rel, (mtime, size) in cur.items():
            old = files[rel]
            if (mtime, size) == tuple(old[:2]):
                continue
            if _hash(os.path.join(repo, rel)) != old[2]:      # content really changed
                return None
            files[rel] = [mtime, size, old[2]]                # same bytes, just touched: refresh stat
            touched = True
        if touched:
            _write(d, "manifest.json", manifest)
        return facts["provenance"]
    except Exception:
        return None


def load(repo, with_calls=False):
    key = f"{SCHEMA}|{provenance.SCHEMA}|{_engine_fp()}|{int(bool(with_calls))}"
    d = _cache_dir(repo)
    cur = _snapshot(repo)
    hit = _cached(d, key, cur, repo)
    if hit is not None:
        return hit

    # Hash before building so an edit made mid-build is seen as a change next run, never cached as fresh.
    files = {}
    for rel, (mtime, size) in cur.items():
        try:
            files[rel] = [mtime, size, _hash(os.path.join(repo, rel))]
        except OSError:
            pass                            # unreadable now → absent from the manifest → rebuilds next time
    index = RepoIndex(repo)
    eps = analyze_repo(repo, index=index)
    prov = provenance.build(repo, with_calls=with_calls, index=index, eps=eps)
    # the manifest is the commit point: drop the old one first and write it last, only if facts landed,
    # so a half-failed save can never pair new facts with an old manifest (or the reverse).
    try:
        os.remove(os.path.join(d, "manifest.json"))
    except OSError:
        pass
    if _write(d, "facts.json", {"provenance": prov}):
        _write(d, "manifest.json", {"key": key, "files": files})
    return prov
