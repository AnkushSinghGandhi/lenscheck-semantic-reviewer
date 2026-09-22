# Contributing to Lenscheck

Thank you for helping make Lenscheck better. Here's how contribution works across the project — it
differs by repo because the licences differ.

## 🐛 Bug reports & feedback — always welcome, everywhere

You do **not** need to sign anything to report a bug, request a feature, or point out a rough edge.
This is genuinely the most valuable thing you can do during the beta.

- **[Open an issue](https://github.com/AnkushSinghGandhi/lenscheck-semantic-reviewer/issues/new)** —
  a clear repro, the command you ran, and what you expected.
- Everyone who files a valid bug report or actionable suggestion gets their **name + GitHub on the
  Wall of Fame** at [lenscheck.dev](https://lenscheck.dev). Land a **merged fix** and we'll send you
  **stickers**. 🎉

## 💻 Code contributions

This repository (`lenscheck-semantic-reviewer`) is **source-available under the Elastic License 2.0**,
and Lenscheck is offered commercially to teams. To keep that possible, outside **code** contributions
here require a one-time **[Contributor License Agreement](CLA.md)** — the CLA-assistant bot will walk
you through signing on your first pull request (you sign once, it covers everything after).

**Prefer to write code without a CLA?** The sibling library
**[`lenscheck-contract`](https://github.com/AnkushSinghGandhi/lenscheck-contract)** is **MIT-licensed**
and takes pull requests with **no agreement needed** — new framework probes, ORM support, docs, and
examples are all great places to start.

| Repo | Licence | Code PRs |
|---|---|---|
| **lenscheck-semantic-reviewer** (this repo) | Elastic-2.0 | Welcome, after signing the [CLA](CLA.md) |
| **lenscheck-contract** | MIT | Welcome, no CLA |
| **lenscheck-cloud** (the hosted service) | private | Issues & feedback only |

## Development

```bash
pip install -e ".[dev]"      # from the repo root
pytest                        # run the test suite
```

Keep changes focused, match the surrounding style, and add a test for anything that changes behaviour.

## Beta access

Lenscheck is invite-only during the beta. To get a key, email
**ankushsinghgandhi@gmail.com** (your GitHub + what you'll use it on) or request one at
[lenscheck.dev](https://lenscheck.dev).
