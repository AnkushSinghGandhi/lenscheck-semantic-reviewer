# Lenscheck Semantic Reviewer

**Git tells you what *lines* changed. Lenscheck tells you what those changes *mean*.**

Lenscheck reads a Python web codebase and, for any pull request, turns a huge diff into a short,
ranked list of what actually matters: new endpoints, new database writes, new external calls, and
data that now leaves the system — with the exact `file:line` to read. It cuts through refactor noise
and is honest about what it couldn't resolve. **No LLM. Deterministic.**

## How it works — your source never leaves your machine

The `lenscheck` CLI is a **thin client**. It extracts an abstract *facts graph* from your code
**locally** (Python's AST — it never runs your code) and sends only that graph — **not your source**
— to the Lenscheck cloud, which ranks it and returns the review.

## Install

```bash
pip install lenscheck-semantic-reviewer
```

Zero third-party dependencies. Needs Python 3.8+ and `git`.

Lenscheck is **invite-only during the beta** — you need an access key:

```bash
export LENSCHECK_API_KEY=sk_live_...     # request access below
```

**Request beta access (free during the beta):** email ankushsinghgandhi@gmail.com.

## Commands

```bash
lenscheck review . --base main --pr 481   # semantic diff of a PR / range
lenscheck map .                           # every endpoint, worst-first
lenscheck usage                           # your plan + reviews used this month
lenscheck init                            # add the GitHub Action to this repo
```

Save outputs for CI: `lenscheck review . --base main --out review.md --json review.json`, then
`lenscheck post <pr> review.md --json review.json --inline --label`.

## What it detects (without running your code)

- **API routes** (new / modified / removed) and their **auth level**
- **Database reads & writes** — resolved to the real SQL table name
- **External API calls** (Stripe, Twilio, …)
- **Async dispatches** (Celery, threads, signals) · **Cache** reads/invalidations
- **PII egress** — personal data leaving the system

Each fact is `✓ verified` / `⚠ potential` / `? unknown` — it never reports "safe" for something it
didn't actually trace.

## GitHub Action

```yaml
# .github/workflows/lenscheck.yml   (or: lenscheck init)
name: Lenscheck
on: { pull_request: {} }
permissions: { contents: read, pull-requests: write, issues: write }
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v2
        with:
          api_key: ${{ secrets.LENSCHECK_API_KEY }}
```

Inputs: `api_key` (required), `api_url` (self-hosting only), `github_token`, `comment`, `inline`,
`label`. Full docs: [GitHub repository](https://github.com/AnkushSinghGandhi/lenscheck-semantic-reviewer).

## License

Elastic License 2.0 — free to use, self-host, and modify; not to resell or offer as a hosted service.
