# ✦ Lenscheck — Semantic PR Review

**Git tells you what *lines* changed. Lenscheck tells you what those changes *mean* — and where to look.**

Lenscheck reads a Python web backend (Django first; Flask and FastAPI too) and, for any pull request, branch, or commit, turns a huge
diff into a short, ranked list of the things that actually matter: new endpoints, new database
writes, new external calls, data that now leaves the system, and anything that breaks a rule your
team cares about. It points you at the exact lines to read — and it's honest about what it
couldn't figure out.

> One line: it makes it impossible for an important change to hide inside a large PR.

**Free.** No account, no API key, no paid tier, and nothing is uploaded: everything runs on your machine or in your CI.
**New in 3.2:** the `serve` Cross-repo tab draws the graph and traces a change's impact, reviews add runtime evidence (`--runtime`), and the extractor follows model methods, properties, forms and reverse relations. [Cross-repo guide](CROSSREPO.md).

**How accurate is it?** When it names a table it's right (0 invented in a real-world spot check); it doesn't see everything yet. Measured numbers, and exactly what it can and can't see: [COVERAGE.md](COVERAGE.md).

**📖 New here? Read [The Complete Guide](blog/lenscheck-complete-guide.md)** — every feature, command,
flag, and concept explained in plain English, with screenshots.

**🏢 Want it on all your repos?** The guide's
[§10 — Set up Lenscheck for all your repos](blog/lenscheck-complete-guide.md#10-set-up-lenscheck-for-all-your-repos)
is the step-by-step playbook — one central workflow, observe mode, the moat, and gating — for a
personal account, a team, or a whole org.

**What Lenscheck finds:** new/changed endpoints & auth · new DB writes · external calls · **PII that
actually leaves** (traced, not guessed) · **dependency bumps that gain new powers** (network /
subprocess / native code) · PR **descriptions that contradict the code** · rules your codebase has
always followed (and the commit that broke one). Outputs to the terminal, a web UI, JSON, HTML,
**SARIF** (GitHub Security tab), inline PR comments, a triage label, and a Mermaid flow diagram.

---

## Install

```bash
pip install lenscheck-semantic-reviewer      # gives you the `lenscheck` command
```

Zero third-party dependencies (pure Python standard library). Needs **Python 3.9+**, plus **git**
and **tar** on the PATH.

*From source instead?* `git clone https://github.com/AnkushSinghGandhi/lenscheck-semantic-reviewer` and use
`python3 diff_pr.py …` / `python3 serve.py …` / `python3 invariants.py …` — the exact same
commands as `lenscheck review` / `lenscheck serve` / `lenscheck invariants`.

### Framework support

| | Django | Flask / FastAPI |
|---|---|---|
| Routes, methods, auth | ✓ `urls.py` + `include()` chains, DRF | ✓ decorators, blueprint / router prefixes, auth dependencies |
| DB reads / writes | ✓ Django ORM, raw SQL | ✓ SQLAlchemy models (`__tablename__`) |
| External calls, PII leaving the system | ✓ | ✓ |
| Background jobs | ✓ Celery tasks, management commands, signal receivers | ✓ Celery tasks |
| Test depth | most of the suite | focused fixture tests (`tests/test_frameworks.py`) |

Django is the most battle-tested path. Flask and FastAPI share the same engine; if a route or write
is missed there, please open an issue with a small example.

---

## Commands

Lenscheck has nine commands:

| Command | What it does |
|---------|--------------|
| `lenscheck review` | semantic diff of a PR / commit / branch range (for the terminal & CI) |
| `lenscheck post` | post a review to a GitHub PR: sticky comment, inline comments, triage label |
| `lenscheck serve`  | the interactive web UI (graph view), auto-selects your current repo |
| `lenscheck invariants` | learn, **confirm**, enforce, and **blame** the rules your codebase follows |
| `lenscheck digest` | org-wide roll-up: open PRs by `lenscheck:*` label across every repo (for leadership) |
| `lenscheck crossrepo` | **new:** shared DB tables across 2+ repos: who writes, who reads, cross-service API calls |
| `lenscheck map` | every endpoint in a repo, grouped by app, worst-first |
| `lenscheck provenance` | table → endpoint map + data-consistency rules as JSON (facts only, shareable) |
| `lenscheck init` | add the GitHub Action to a repo in one command |

Run `lenscheck review` with **no arguments** inside a repo to review the current branch vs. the default
branch. Full flag-by-flag reference: **[the Complete Guide](blog/lenscheck-complete-guide.md)**.

The **repo** can be a **local path** *or* a **GitHub URL** (URLs are cloned into a cache at
`~/.cache/lenscheck` and reused).

### `lenscheck crossrepo` — shared tables across services (new in 3.0)

```bash
lenscheck crossrepo ./orders ./billing --html crossrepo.html     # 2+ repos, PATH or PATH@BRANCH
lenscheck crossrepo ./orders ./billing --writes-only --schema schema.json
lenscheck crossrepo ./orders ./billing --changed ./orders --since main   # only what this change touches
```

Give it two or more repos that share a database. For every shared table it lists who **writes** and who
**reads**, down to `route → handler → file:line`, across views **and background jobs** (Celery tasks,
management commands, signal receivers), grouped worst-first:

- 🔴 **multi-writer**: 2+ services write it → they must agree on schema and write logic
- 🟡 **producer → consumer**: one writes, the others read → the writer owns the shape
- **read-only**: a shared lookup, usually safe

It also lists **cross-repo API calls** (service A calls a route service B serves). `--schema` adds a
column-level rule ("coordinate on unique key (order_id, user_id)"). Results are cached per repo by git
HEAD. Also in the UI: `lenscheck serve` → **Cross-repo** tab (with a graph and a change-impact trace). Details: [CROSSREPO.md](CROSSREPO.md).

### `lenscheck map` — every endpoint, worst-first

```bash
lenscheck map .                      # whole repo, grouped by app
lenscheck map . --risky --html map.html
```

### `lenscheck provenance` — table → endpoint map as JSON

```bash
lenscheck provenance . --out facts.json   # facts only (table/endpoint names, no source) — safe to share
```

### `lenscheck init` — one-command GitHub Action setup

```bash
lenscheck init            # writes .github/workflows/lenscheck.yml in advisory mode
lenscheck init --gate     # block merges on a confirmed-rule violation
```

### `lenscheck review`

```bash
# a GitHub Pull Request by number (works on a URL repo)
lenscheck review https://github.com/owner/repo --pr 481

# a single commit (its diff vs its parent)
lenscheck review /path/to/repo --commit 9cca6d24

# a merge commit (base = ^1, head = ^2)
lenscheck review /path/to/repo --merge <merge-sha>

# any two refs — branches or commits
lenscheck review /path/to/repo --base master --head feature-branch
lenscheck review /path/to/repo --base 9cca6d24 --head 34e8237b
```

You get a ranked review — **🔴 security/data-flow · 🟠 money/payment · 🟡 new write/external ·
🟢 read-only or refactor** — and for each item: *why it matters*, the *flow*, the exact
*file:line* locations to inspect, and any *unknowns*. A rename with no behavior change shows as a
green "refactor, nothing to see" — facts are matched on the URL route, not class names, so
refactors don't create noise.

**Output formats** (Markdown is written by default, for CI comments):

```bash
lenscheck review <repo> --pr 481 \
    --out review.md \        # Markdown (used by CI to post a sticky PR comment)
    --json review.json \     # structured data (for any other UI)
    --html review.html \     # a self-contained clickable report — open it in a browser
    --sarif review.sarif     # GitHub code scanning — findings on the changed line + Security tab
```

**See how far a change reaches into your other services** (shared database, HTTP calls, Celery
tasks):

```bash
lenscheck review ./orders --base main --head feature --crossrepo ../billing ../notifications
```

The review gains a **🔗 Cross-repo impact** section: every endpoint in the other repos that gets the
changed data (readers of a table the change writes, and whatever they call or dispatch, up to 3 hops),
depends on a changed route or task, or also writes the same table — each with its file:line and the
path that connects it. A path through an unverified (`tail`) match is marked ⚠ verify. See
[CROSSREPO.md](CROSSREPO.md#change-impact).

**Add runtime evidence** from [lenscheck-contract](https://github.com/AnkushSinghGandhi/lenscheck-contract)'s record mode (run your tests with the guard in `record`, save `guard.suggestions()` as JSON):

```bash
lenscheck review ./orders --base main --head feature --runtime observed.json
```

On the endpoints the PR changed, runtime confirms a static ⚠ (→ ✓) or adds what only runtime saw (e.g. the host of a call whose URL is built at runtime), in a **🔬 Runtime evidence** section and `runtime` in the JSON. It never removes a static fact: a path your tests didn't run is still kept.

**Enforce your rules & gate CI:**

```bash
lenscheck review <repo> --pr 481 \
    --invariants lenscheck-invariants.json \   # confirmed rules to check
    --fail-on violation                    # exit non-zero on a new violation (or: crit)
```

With `--invariants`, the review *opens* with a 🛡 panel that separates violations this PR
**introduced** from pre-existing ones, and flags any **weakening** of a rule (e.g. a new allowed
external destination) as its own event.

Flags: `--pr` · `--commit` · `--merge` · `--base/--head` · `--out` · `--json` · `--html` ·
`--sarif` · `--invariants` · `--fail-on {violation,crit}`.
Set `GITHUB_TOKEN` for private repos / to avoid GitHub API rate limits.

### `lenscheck serve` — the web UI

```bash
# run it inside a git repo → that repo is auto-selected, browser opens
cd /path/to/repo
lenscheck serve
```

Three ways to open a review in the UI:

- **paste a GitHub PR link** (`https://github.com/owner/repo/pull/123`) and hit **Review PR**;
- **browse** a repo's PRs (Load PRs → pick one), or type a **PR #**, **commit sha**, or **base +
  head**;
- **preload from the command line** — the UI opens straight into it:

```bash
lenscheck serve --pr 481             # open a PR on load
lenscheck serve --commit 9cca6d24    # open a commit's diff on load
lenscheck serve --merge <sha>        # open a merge commit
lenscheck serve --base A --head B     # open a range
```

Each change shows an **interactive flow graph** (`route → handler → tables / external services /
background jobs`, colored by kind, PII flagged) plus a **Where-to-look** tab with exact
`file:line` locations.

Options: `--repo <path-or-url>` (default: the git repo of the current directory) · `--port 8765`
· `--invariants <file>` · `--no-open` (don't launch a browser).

### `lenscheck invariants` — discover rules from history

```bash
lenscheck invariants /path/to/repo --snapshots 8
```

Ranks properties by how long they've survived in git history (a rule that held for 800 commits
was almost certainly on purpose). Writes `invariants_report.md` (readable) and
`invariants.discovered.json` (a corpus with each rule, its history, and current exceptions). Set
`"confirmed": true` on the ones you want, then pass that file to `lenscheck review --invariants`.

**Turn the moat on across many repos without hand-confirming each** —
`lenscheck invariants <repo> --bootstrap --baseline org-baseline.json` auto-confirms the safe rules and
**freezes today's state as a baseline** (a ratchet — only *new* violations fire, existing debt is
grandfathered). Seed it with a shared org baseline (see
[`org-baseline.sample.json`](org-baseline.sample.json)) so every repo inherits the same universal
rules; each repo's own exceptions are filled in automatically. Auto-confirmed rules are tagged
`owner: auto-bootstrap` for later human review.

> Example finding: **"Authenticated access before any DB write"** held at 100% for most of the
> project's history, then dropped to 80% — here are the 4 endpoints that broke it, with locations.

### `lenscheck digest` — org-wide roll-up for leadership

```bash
lenscheck digest --org my-org --slack "$SLACK_WEBHOOK"
```

Turns per-PR reviews into one number a leader can read. It counts the **open** PRs across a whole
org that still carry each `lenscheck:*` triage label — no re-analysis, and **no GitHub Advanced
Security** needed (it reads labels Lenscheck already applied):

```
🔴  4  security / PII / unauth write
🟠  9  money path
🟡 12  new DB write / external
```

A tier it couldn't fetch shows `?`, never a fake `0`. Needs a `GITHUB_TOKEN` with org read
access. Add `--repos repos.txt` (one `owner/repo` per line) for an adoption line — how many repos
have the workflow live and a confirmed invariants file. Run it on a schedule and pipe it to Slack.

---

## GitHub Action

Get an automatic semantic review comment (and an optional merge gate) on every PR:

```yaml
# .github/workflows/lenscheck.yml
name: Lenscheck
on: { pull_request: {} }
permissions:
  contents: read
  pull-requests: write        # needed to post the comment
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v3
        with:
          fail_on: violation                 # violation | crit | none (default: none)
          invariants: lenscheck-invariants.json  # optional: enforce your confirmed rules
          comment: 'true'                     # post/update a sticky PR comment (default: true)
```

Inputs: `github_token` (default `${{ github.token }}`), `fail_on`, `invariants`, `comment`,
`inline`, `label`, `scan_deps`, `upload_sarif` (set `false` on private repos without GitHub
Advanced Security so the code-scanning upload is skipped and the check stays green). The Action
installs Lenscheck from its own checkout (so `@v3` always runs the code that tag points to; pin a full commit SHA for fully reproducible builds), reviews the PR, posts a sticky
comment, and (with `fail_on`) fails the check when the PR introduces a violation. With `inline`
(default `true`) it also pins each finding as an inline review comment on the exact changed line —
findings whose location isn't in the PR's diff stay in the sticky comment.

---

## Why this exists

AI writes a lot of code now. A single PR can be 60 files and 18,000 lines. A normal line-diff
hides the one important change inside thousands of mechanical ones. You don't need to read all of
it — you need to know **what changed** in behavior, **how it flows**, **where to look**, and
**whether it broke a promise** ("user data never leaves without auth"). Lenscheck doesn't replace your
code or ask you to maintain diagrams; it re-reads the real code every time, so its view can't go
stale.

## What it looks at (the "lens")

For every HTTP endpoint it follows seven **edges**:

| Edge | Question |
|------|----------|
| **route → handler** | which URL maps to which view? |
| **auth** | does it require login/permission? |
| **db tables** | which tables does it read and write? (resolved to the real SQL table name) |
| **external calls** | does it call another service (payment, email, …)? |
| **async** | does it kick off a background job / thread / signal? |
| **cache** | does it read or invalidate a cache key? |
| **PII** | does personal data travel toward something that leaves the app? |

**It never pretends to be sure.** Every fact is `✓ verified` / `⚠ potential` (found via a call,
or destination behind config) / `? unknown` / `n/a`. It will **never** say "no PII leaves here"
when it just didn't see it — absence is shown as *unknown*, not *safe*.

## The three layers

```
        WHAT IS                    WHAT SHOULD BE
     (derived facts)   ── gap ──   (rules you confirmed)
```

1. **Observation** — read the code, derive the facts (no upkeep).
2. **Attention** — for a PR, show what changed, how it flows, where to look.
3. **Intent** — learn the rules your code has always followed, confirm them, enforce them.

The product is the **gap** between what the code does and what you said it should do.

## Reading the output

- **Severity:** 🔴 look first (security/data-flow) · 🟠 money path · 🟡 new write/external · 🟢 safe/refactor.
- **Status:** ✓ sure · ⚠ probably (follow the location) · ? couldn't tell.
- **"via call":** the fact was found one hop from the handler (e.g. a write hidden in a service or
  model method) — still real; the location points at the actual line.

## What it does **not** see (on purpose)

- **Logic inside a function** — ordering, off-by-one, a wrong condition (tests catch those).
- **Very dynamic Python** — runtime-generated behavior, string-dispatched tasks — shown as ⚠/?.
- **Cross-process data flow** — once it goes through a queue/another service, static reading stops.
- **Object-level data flow** — a whole `user` object passed around, a field read deep inside → PII
  flagged as *potential*, never proven.

## What's in the repo

```
diff_pr.py     # lenscheck review — the semantic diff engine (+ enforce + outputs)
serve.py       # lenscheck serve  — the web UI / API
invariants.py  # lenscheck invariants — discover rules from git history
digest.py      # lenscheck digest — org-wide roll-up from lenscheck:* labels (no GHAS needed)
crossrepo.py   # lenscheck crossrepo — shared tables + API calls across repos
map_repo.py    # lenscheck map — whole-repo endpoint map
provenance.py  # lenscheck provenance — table → endpoint facts as JSON
schema_dump.py # dump DB unique keys for crossrepo --schema
enforce.py     # check a PR against confirmed rules
deps.py        # dependency capability-delta scan (bumped deps that gain new powers)
extractor/     # reads code → facts (the 7-edge lens)
gitutil.py     # git helpers (URL clone/cache, repo detection)
cli.py         # the `lenscheck` entry point
web/           # app.html (live UI + graph), map.html, crossrepo.html, glossary.js, viewer.template.html
docs/          # the landing site (index, features, compare, contract, docs)
ci/            # post_review.py (sticky PR comment), reusable workflow
action.yml     # the published GitHub Action
```

## License

[Elastic License 2.0](LICENSE) — free to use, self-host internally, and modify; you may **not**
resell it or offer it as a hosted service. There is no paid tier.
