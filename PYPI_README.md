# Lenscheck Semantic Reviewer

**Git tells you what *lines* changed. Lenscheck tells you what those changes *mean*.**

Lenscheck reads a Python web codebase and, for any pull request, commit, or branch, turns a huge diff
into a short, ranked list of the things that actually matter: new endpoints, new database writes,
new external calls, and data that now leaves the system. It cuts through refactor noise and points
you at the exact lines that matter — and it's honest about what it couldn't resolve.

Also: **traced PII leaks** (the exact field → where it leaves), **dependency bumps that gain new
powers** (network/subprocess/native), PR **descriptions that contradict the code**, and the **rules
your codebase follows** (discover, confirm, enforce, and blame the commit that broke one). Outputs to
the terminal, a web UI, JSON, HTML, **SARIF**, inline PR comments, a triage label, and a Mermaid
diagram. Full walkthrough: **The Complete Guide** in the repo (`blog/lenscheck-complete-guide.md`).

## Install

```bash
pip install lenscheck-semantic-reviewer
```

Zero third-party dependencies. Needs Python 3.8+, plus `git` and `tar` on the PATH.

## Commands

The repo argument can be a **local path** or a **GitHub URL** (URLs are cloned & cached).

### `lenscheck review` — review a PR, commit, or range

```bash
lenscheck review https://github.com/owner/repo --pr 481      # a GitHub PR by number
lenscheck review /path/to/repo --commit 9cca6d24             # a single commit (vs its parent)
lenscheck review /path/to/repo --merge <merge-sha>           # a merge commit
lenscheck review /path/to/repo --base master --head feature  # any two refs / commits
```

Output formats and CI gating:

```bash
lenscheck review <repo> --pr 481 \
    --out review.md \                     # Markdown (default; used by CI to post a PR comment)
    --json review.json --html review.html # structured JSON + a self-contained clickable report

lenscheck review <repo> --pr 481 \
    --invariants lenscheck-invariants.json \  # enforce your confirmed rules
    --fail-on violation                   # exit non-zero on a new violation (or: crit)
```

Set `GITHUB_TOKEN` for private repos / to avoid GitHub API rate limits.

### `lenscheck post` — put a review on a GitHub PR

```bash
lenscheck post 481 review.md --json review.json --inline --label
```

Posts a sticky summary comment, inline comments pinned to the exact changed lines, and one `lenscheck:*`
triage label. Needs `GITHUB_TOKEN` + the `gh` CLI (present on GitHub runners).

### `lenscheck serve` — interactive web UI (with graph view)

```bash
cd /path/to/repo && lenscheck serve      # auto-selects the current git repo, opens a browser
```

In the UI: paste a **GitHub PR link**, or type a **PR #**, **commit sha**, or **base + head**, or
**browse** the repo's PRs. Each change gets an interactive **flow graph**
(`route → handler → tables / external / jobs`) and a list of exact `file:line` locations.

Preload a review straight from the command line:

```bash
lenscheck serve --pr 481          # open a PR on load
lenscheck serve --commit <sha>    # open a commit's diff on load
lenscheck serve --base A --head B # open a range
```

Options: `--repo <path-or-url>` · `--port 8765` · `--invariants <file>` · `--no-open`.

### `lenscheck invariants` — discover rules from git history

```bash
lenscheck invariants /path/to/repo --snapshots 10
```

Ranks properties by how long they've held across history, writing `invariants_report.md` and
`invariants.discovered.json`. Confirm the ones you want and feed them to `lenscheck review --invariants`.

Or **bootstrap every repo at once**: `lenscheck invariants <repo> --bootstrap --baseline org-baseline.json`
auto-confirms the safe rules and freezes today's state, so it only flags *new* violations — turning
the moat on across many repos with no hand-confirming.

### `lenscheck digest` — org-wide roll-up for leadership

```bash
lenscheck digest --org my-org --slack "$SLACK_WEBHOOK"
```

Counts the **open** PRs across a whole org by the `lenscheck:*` triage label Lenscheck already applied —
🔴 security / 🟠 money / 🟡 new write. No re-analysis, and **no GitHub Advanced Security** needed.
Needs a `GITHUB_TOKEN` with org read access.

## What it detects (without running your code)

- **API routes** (new / modified / removed) and their **auth level** (e.g. AllowAny vs Authenticated)
- **Database reads & writes** — resolved to the real **SQL table** name
- **External API calls** (Stripe, Twilio, PayTM, …)
- **Async task dispatches** (Celery, threads, signals)
- **Cache reads & invalidations** (Django cache: get / set / delete)
- **PII egress** — personal data leaving the system

Each fact is marked `✓ verified` / `⚠ potential` / `? unknown` — it never reports "safe" for
something it didn't actually trace.

## GitHub Action

```yaml
# .github/workflows/lenscheck.yml
name: Lenscheck
on: { pull_request: {} }
permissions: { contents: read, pull-requests: write }
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v1
        with:
          fail_on: violation   # violation | crit | none
```

Inputs include `fail_on`, `invariants`, `comment`, `inline`, `label`, `scan_deps`, and
`upload_sarif` (set `false` on private repos without GitHub Advanced Security so the SARIF upload is
skipped and the check stays green).

Full documentation, the graph UI, and architecture details:
[GitHub repository](https://github.com/AnkushSinghGandhi/lenscheck-semantic-reviewer).

## License

Elastic License 2.0 — free to use, self-host, and modify; not to resell or offer as a hosted service.
