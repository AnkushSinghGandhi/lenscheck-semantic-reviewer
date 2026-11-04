"""factindex: the content-hash fact cache serves an unchanged repo without parsing, rebuilds on any change,
keeps with_calls variants apart, and provenance.build parses the repo only once."""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import factindex  # noqa: E402
import provenance  # noqa: E402

BLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "blog")


def _repo(tmp_path, monkeypatch):
    repo = str(tmp_path / "repo")
    shutil.copytree(BLOG, repo)
    monkeypatch.setenv("LENSCHECK_INDEX_DIR", str(tmp_path / "idx"))
    return repo


def _boom(*a, **k):
    raise AssertionError("rebuilt")


def test_unchanged_repo_is_served_without_rebuilding(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    a = factindex.load(repo, True)
    monkeypatch.setattr(factindex, "RepoIndex", lambda *x, **k: (_ for _ in ()).throw(AssertionError("rebuilt")))
    b = factindex.load(repo, True)
    assert a == b


def test_change_triggers_rebuild_and_matches_fresh(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    factindex.load(repo, True)
    # a same-LENGTH edit (create→filter, i.e. write→read) that still changes the facts — so the stale
    # vs fresh result differs only if the content-hash check fires; a size check alone wouldn't catch it
    views = os.path.join(repo, "views.py")
    with open(views, encoding="utf-8") as f:
        src = f.read()
    assert "Order.objects.create(total=0)" in src
    with open(views, "w", encoding="utf-8") as f:
        f.write(src.replace("Order.objects.create(total=0)", "Order.objects.filter(total=0)"))
    got = factindex.load(repo, True)
    assert got == provenance.build(repo, with_calls=True)


def test_with_calls_variants_do_not_share_cache(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    first = factindex.load(repo, False)
    second = factindex.load(repo, True)
    assert "endpoints" in second
    assert "endpoints" not in first


def test_no_double_parse(monkeypatch):
    real, count = provenance.RepoIndex, []

    def counting(*a, **k):
        count.append(1)
        return real(*a, **k)

    monkeypatch.setattr(provenance, "RepoIndex", counting)
    provenance.build(BLOG, with_calls=False)
    assert len(count) == 1


# edge cases beyond the spec's four: touch-only, add/delete, corrupt cache

def test_touched_but_identical_file_is_served_from_cache(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    a = factindex.load(repo, True)
    svc = os.path.join(repo, "services.py")
    st = os.stat(svc)
    os.utime(svc, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))    # new mtime, same bytes
    monkeypatch.setattr(factindex, "RepoIndex", _boom)
    assert factindex.load(repo, True) == a


def _assert_rebuilds(repo, monkeypatch):
    monkeypatch.setattr(factindex, "RepoIndex", _boom)
    try:
        factindex.load(repo, True)
    except AssertionError as e:
        assert str(e) == "rebuilt"
    else:
        raise AssertionError("served from cache after the file set changed")


def test_added_file_rebuilds(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    factindex.load(repo, True)
    with open(os.path.join(repo, "extra.py"), "w", encoding="utf-8") as f:
        f.write("X = 1\n")
    _assert_rebuilds(repo, monkeypatch)


def test_deleted_file_rebuilds(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    factindex.load(repo, True)
    os.remove(os.path.join(repo, "settings.py"))
    _assert_rebuilds(repo, monkeypatch)


def test_corrupt_cache_rebuilds(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    a = factindex.load(repo, True)
    d = factindex._cache_dir(repo)
    with open(os.path.join(d, "facts.json"), "w") as f:
        f.write("{not json")
    assert factindex.load(repo, True) == a
