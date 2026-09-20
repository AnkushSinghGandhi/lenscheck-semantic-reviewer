# ✦ Lenscheck — Semantic PR Review

**Git tells you what *lines* changed. Lenscheck tells you what those changes *mean* — and where to look.**

Lenscheck reads a Django/Python codebase and, for any pull request, turns a huge diff into a short,
ranked list of the things that actually matter: new endpoints, new database writes, new external
calls, data that now leaves the system, and anything that breaks a rule your team cares about — with
the exact `file:line` to read, and honest about what it couldn't figure out. **No LLM. Deterministic.**

> One line: it makes it impossible for an important change to hide inside a large PR.

## How it works — your source never leaves your machine

The `lenscheck` CLI is a **thin client**. It extracts an abstract *facts graph* from your code
**locally** (Python's own AST — it never executes your code), and sends only that graph — **not your
source** — to the Lenscheck cloud, which ranks it and returns the review. Same PR in, same review out,
every time.

---

## Install

```bash
pip install lenscheck-semantic-reviewer      # gives you the `lenscheck` command
```

Zero third-party dependencies (pure Python standard library). Needs **Python 3.8+** and **git**.

Lenscheck is **invite-only during the beta**, so you need an access key:

```bash
export LENSCHECK_API_KEY=sk_live_...          # request access (below)
# self-hosting the cloud? also point at it:
export LENSCHECK_API_URL=https://your-host
```

**Request beta access:** email **ankushsinghgandhi@gmail.com** (tell me your GitHub + what you'll
use it on) — it's free during the beta.

---

## Commands

| Command | What it does |
|---------|--------------|
| `lenscheck review` | semantic diff of a PR / commit / branch range |
| `lenscheck map` | every endpoint in the repo, grouped by app, worst-first |
| `lenscheck post` | post a review to a GitHub PR: sticky comment, inline comments, triage label |
| `lenscheck usage` | your plan and reviews used this month |
| `lenscheck init` | drop the GitHub Action into the current repo (one-command setup) |

The **repo** can be a **local path** or a **GitHub URL**.

### `lenscheck review`

```bash
lenscheck review . --base main --pr 481      # a PR (base..HEAD)
lenscheck review . --base A --head B          # any two refs
lenscheck review . --out review.md --json review.json   # save for CI / `lenscheck post`
```

You get a ranked review — **🔴 security/data-flow · 🟠 money/payment · 🟡 new write/external ·
🟢 read-only or refactor** — and for each item: *why it matters*, the *flow*, the exact *file:line*
locations, and any *unknowns*. A rename with no behavior change shows green ("refactor, nothing to
see") — facts are matched on the URL route, not class names, so refactors don't create noise.

### `lenscheck map`

```bash
lenscheck map .            # the whole repo, worst-first
lenscheck map . --risky    # only the ones that matter
```

---

## GitHub Action

Automatic review comment on every PR:

```yaml
# .github/workflows/lenscheck.yml   (or run: lenscheck init)
name: Lenscheck
on: { pull_request: {} }
permissions:
  contents: read
  pull-requests: write        # to post the comment
  issues: write               # to apply the lenscheck:* label
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v1
        with:
          api_key: ${{ secrets.LENSCHECK_API_KEY }}   # add your beta token as a repo secret
```

Inputs: `api_key` (required), `api_url` (self-hosting only), `github_token`, `comment`, `inline`,
`label`. The Action checks out the repo, extracts facts, gets the review from the cloud, and posts a
sticky PR comment (+ inline notes and a triage label).

---

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

**It never pretends to be sure.** Every fact is `✓ verified` / `⚠ potential` / `? unknown` / `n/a`.
It will **never** say "no PII leaves here" when it just didn't see it — absence is shown as
*unknown*, not *safe*.

## What it does **not** see (on purpose)

- **Logic inside a function** — ordering, off-by-one, a wrong condition (tests catch those).
- **Very dynamic Python** — runtime-generated behavior, string-dispatched tasks — shown as ⚠/?.
- **Cross-process data flow** — once it goes through a queue/another service, static reading stops.

---

## Plans

**Free during the beta.** **Team** adds private repos, the automatic PR bot, your team's rules
learned & enforced across every repo, an org dashboard + risk roll-up, and merge gating.
**Enterprise** adds SSO, audit logs, on-prem, and SLA. Request access: **ankushsinghgandhi@gmail.com**.

## What's in this repo

```
cli.py         # the `lenscheck` entry point (thin client)
cloud.py       # extract facts locally → send to the cloud → print the review
extractor/     # reads code → facts (the 7-edge lens); runs locally, never leaves your machine
gitutil.py     # git helpers (URL clone/cache, repo detection)
ci/            # post_review.py (sticky PR comment), reusable workflow
action.yml     # the published GitHub Action
```

The ranking / diff / invariant engine runs in the **Lenscheck cloud**, not in this package.

## License

[Elastic License 2.0](LICENSE) — free to use, self-host internally, and modify; you may **not**
resell it or offer it as a hosted service.
