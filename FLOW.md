# `lenscheck flow` — one entrypoint, step by step

`provenance` and `crossrepo` answer *which* code touches a table. `flow` answers *in what order one
route runs*: DB reads/writes, external calls, async dispatches, helper calls (nested) and early
exits — each with the `if`/`for`/`try` conditions guarding it, and literal column values where the
source spells them out (`where {process: 0}`, `set {process: 1}`). Deterministic, stdlib only, no LLM.

## Usage

```bash
lenscheck flow ./svc --list                      # every route / command / task / signal
lenscheck flow ./svc "lead upload"               # resolve a query and print the tree
lenscheck flow ./svc /lead/upload --all --out flow.json
```

A query matches a handler, a route, a full `include()`-chained route, or `command:<name>` /
`task:<name>`. If several candidates fit, flow prints the ranked picks and exits 2; choose one with
`--pick N`. `--all` keeps otherwise-pruned bare helpers; `--out` also writes JSON.

## What a step means

| marker | step |
|---|---|
| `R` / `W` | DB read / write on a model, with `where{…}` and `set{…}` literal values when known |
| `→` | external call (its destination where resolvable) |
| `⇢` | async dispatch (celery / thread), with a same-repo celery task expanded underneath |
| `$` | cache read / mutate |
| `call` | a resolved helper call; its body is nested below it |
| `⛔` | `return` / `raise` / `continue` / `break` |

Guards are printed as `? <condition>` lines when they change between sibling steps. `(see sN)` marks a
call already expanded earlier in this trace; `═══ celery ═══` marks a task body reached through a
dispatch. `✂ not followed` marks a call cut by the depth/follow cap — it is never pruned, so a gap is
always visible; a call cut deep and reached again shallower is expanded there instead of ref'd.

## JSON shape

```json
{"schema": 1, "generated_by": "lenscheck flow", "repo": "/abs/path",
 "repo_meta": {"commit": "...", "dirty": false},
 "entry": {"route": "/lead/upload", "handler": "lead_upload", "kind": "http", "loc": "app/views.py:12"},
 "steps": [{"id": "s1", "kind": "read", "text": "Lead", "model": "Lead", "where": {"process": 0},
            "guards": [], "loc": "app/views.py:14", "children": []}],
 "tables": {"Lead": "app_lead"},
 "stats": {"calls_followed": 3, "truncated": false}}
```

## Cross-repo hops

With `--crossrepo PATH [PATH ...]` (each path may be `path@branch`) the flow follows what leaves the
repo, and `--follow REPO:ROUTE` can force the reader a table hop expands:

```bash
lenscheck flow ./svc /push --crossrepo ../other
lenscheck flow ./svc /push --crossrepo ../other --follow svcB:command:read_items
```

| hop | trigger | what expands |
|---|---|---|
| `http` | the entry makes a call whose path matches another repo's route | that repo's serving endpoint |
| `celery` | the entry dispatches a task another repo defines | that repo's task |
| `table` | the entry writes a table another repo reads | the best-ranked reader of that table |

Table hops rank readers by whether they **use a column only this repo is seen writing** (the
distinctive column, e.g. `job_id`), then by how many written columns they share; a reader that
**filters a column to a different literal** than this flow writes is contradicted and ranked last.
The winner expands when it is unambiguous (or `--follow` names it); otherwise every candidate is
listed with `--follow` hints. Hop steps are namespaced (`h1.s3`) so they never collide with the
origin tree. Table hops are printed after the origin tree; `http`/`celery` hops sit under their
dispatch line.

Hops are single-depth (a hop's own links are not followed), and nothing from the other repo is
written back into this repo's cache.

## HTML

```bash
lenscheck flow ./svc /push --html flow.html
lenscheck flow ./svc /push --crossrepo ../other --html flow.html --screen s3=shot.png
```

`--html FILE` writes one self-contained page (no server, no libraries — it opens from `file://`):

- **Story** — one lane per repo and a card per phase (entry, celery, http / celery / table hops),
  with labelled arrows, the guards in effect (`when`), table and external chips, `✂` cuts and any
  attached screenshots. Click a card to jump to its steps.
- **Tables** — a touch timeline: every read/write grouped by table in execution order across repos,
  with `where` / `not` / `set` values; click a chip to reveal its step. "Only written tables" is on
  by default.
- **Steps** — the full step tree per phase, collapsible, with markers, guard lines, `(see sN)` links
  and `✂ not followed` badges. Search filters by text, table or file; click a step for its source
  snippet (with a `vscode://` link when the repo path is known).

`--screen [STEP=]PATH` attaches a screenshot (png / jpg / jpeg / gif / webp, ≤ 5 MB) to a step's
card, or to the first phase when no step is given; it is embedded as a data URI. Bad files are
skipped with a warning. The ASCII tree and `--out` JSON are unchanged.

## Overview and `--explain`

The HTML opens on an **Overview**: one band per system, numbered business stages with icons, `→`
hand-offs (`⑂ or` for alternatives), then supporting modules, important notes, table lifecycles, key
files, a legend and a one-line summary. Click any line to open the steps and code behind it; the
technical views (lanes, timeline, step tree) sit under **Details**.

```bash
lenscheck flow ./cms leads/upload/ --crossrepo ../c360 --html flow.html --explain
```

- Without `--explain` the overview is built from the facts alone (no LLM, offline).
- With `--explain` the `claude` CLI (`claude -p --json-schema`, your Claude Code login, no API key) reads a
  text digest of the flow — steps, guards, tables, docstrings and a few lines of code around each DB /
  external step — and answers in a fixed JSON schema. Claude never writes HTML.
- **Grounding:** every stage, item, note and table must cite step ids. Ids that are not in the flow are
  dropped; a line left without evidence is shown dashed with ⚠ (unverified). Files and tables Claude
  names must exist in the flow.
- The answer is cached by digest + prompt + schema + model (`--refresh-explain` to redo it);
  `--explain-model` picks the model (default `claude-opus-5-5`). If `claude` is missing or fails, the
  page falls back to the facts-only overview and says why on stderr.

## Catalog — what flows exist

```bash
lenscheck flow ./cms --catalog                                  # text, grouped by app
lenscheck flow ./cms --catalog --journeys ~/flows/cms --html ~/.cache/lenscheck/flows/cms/   # → index.html
```

Every entrypoint (APIs, commands, tasks, signals) grouped by Django app, with a one-line summary
(`@extend_schema(summary=…)` per HTTP method, else the docstring; a command's `help`) and the tables it
writes. Comes from the cached fact index, so it is instant after the first run. The HTML index lists the
saved journeys on top, has a search box, and links every flow page already generated next to it.

## Journeys — several API calls in user order

A wizard or any multi-step feature is a **journey**: one YAML (or JSON) file naming the steps and the
entrypoints each step calls.

```yaml
name: campaign-creation
title: Campaign creation (5-step wizard)
source: CampaignHelper.get_step_progress (campaigns/api/helper.py:1050)   # why this order
crossrepo: [../c360]                 # optional, relative to the repo
steps:
  - name: Campaign, Entity & DU Setup
    entries: [CampaignListView.post, CampaignDUListView.post]
  - name: Targeting Setup
    entries: [CampaignTargetingView.put]
```

```bash
lenscheck flow ./cms --journey campaign-creation.yaml --html ~/.cache/lenscheck/flows/cms/ --explain
```

- An entry is `Handler`, `Handler.method` (one HTTP method of a class view), `Handler@file-fragment` when
  two definitions share a name, or `command:name` / `task:name`.
- All entries are traced with one parsed index and joined into one flow: each journey step is a phase
  (`Step k · name`), so the overview gets one band per step and the table timeline runs across steps.
- `--html DIR/` names the page after the flow (`campaign-creation.html`), which is what the catalog links.

## In the browser — `lenscheck serve` → 🧭 Flow

```bash
lenscheck serve --repo ./cms          # then open the Flow tab (http://127.0.0.1:8765/flow)
```

A form instead of the command line: repo, an entrypoint (suggestions from the repo) **or** a saved journey
(from `<repo>/.flows`, or any folder you type), optional cross-repo paths, and "Explain with Claude". Several
matches come back as pick buttons. The result is the same page as `--html` (overview, details, code panel),
and the URL keeps the query, so a reload or a shared link re-runs it (`/flow?repo=…&journey=…&explain=1`).

API: `GET /api/flow/candidates?repo=&journeys=` (entrypoints + journeys) and `GET /api/flow?repo=&q=|journey=
&pick=&crossrepo=a,b&follow=x&explain=1&refresh=1` (409 + `ranked` when ambiguous). Same trust boundary as the
other endpoints: `LENSCHECK_TOKEN`, and every repo / cross-repo / journey path must pass
`LENSCHECK_ALLOWED_REPOS`; a journey must be a `.yaml`/`.json` file. An explanation request runs `claude -p`
on the server (minutes when fresh, cached after).

## Accuracy

- **One of.** When the model is picked at runtime from a known set — a helper returning
  `A if x else B`, or `MAP[key].objects…` over a module-level dict of models — the step reads
  `a_tbl | b_tbl (one of)` (`one_of` in JSON, `W?` chips in the HTML timeline). This lives only in
  the flow: provenance and crossrepo facts are unchanged (a wrong table is worse than a miss).
- **Implicit else.** Code after `if X: return …` / `continue` / `break` runs under `else (not: X)`.
  `raise` does not add a guard — the error path is not a business branch.
- **Alternatives.** Phases on opposite sides of the same condition are drawn as `⑂ or`, not in sequence.
- **Not DB writes.** `.save()` on a non-ORM object (`Workbook`, `BytesIO`, `Image`, … — an explicit
  list) and `@mock.patch('pkg.Model.objects')` decorators (not a `/` route) are ignored.

## Known limits

- **Order inside one statement is approximate**: facts come before the statement's calls, and an outer
  call is ordered before the calls in its arguments.
- **Nested defs are inlined** into their enclosing step; they are not separate callable targets.
- **Only analyzer-resolved calls are followed** — an unresolved dynamic call is not traced.
- **Cold first run builds the fact cache** (the same cache `provenance`/`crossrepo` use); on c360-sized
  repos that is roughly a minute, then it is cached.
- **Cross-repo hops are single-depth** — the links a hop's own endpoint makes are not followed.
- **HTML/serve output, front-end parsing and any natural-language resolver are later milestones** —
  the `flow` command itself is static (`--html` is a static export of that same output).
