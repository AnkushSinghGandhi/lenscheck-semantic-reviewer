# `lenscheck crossrepo` — cross-service data-consistency

Most consistency bugs live *between* services, not inside one. `crossrepo` takes two or
more repos that share a database and finds every table they share — down to
`route → handler → file:line`, across routed views **and background jobs (celery tasks,
management commands, signal receivers)** — and flags where **two services write the same
table** (the cross-service race / constraint conflict). It also surfaces **HTTP coupling**:
where one service's endpoint calls a route another service serves, and **Celery coupling**: where one
service dispatches a task another service defines. Deterministic, no LLM, free, and it all runs on your machine.

## What it reports
Per shared table: which repos **write** vs **read** it, down to `route → handler → file:line`,
classified as:
- 🔴 **multi-writer** — 2+ services write it → coordinate schema & write logic.
- 🟡 **producer → consumer** — one writes, others read → the writer owns the shape.
- **read-only** — nobody writes it (shared lookup) → usually safe.

With `--schema` it adds the precise column-level rule:
*"coordinate on unique key (form_id, unit_id, email) — both writers must agree."*

Below the tables it lists **cross-repo API calls** — `repo A → call /path → repo B route` —
and **cross-repo Celery tasks** — `repo A → send_task('billing.charge') / fn.delay() → repo B task` —
so you see every way two services couple: the database, HTTP, and task queues.

## Full flow

### 1. (optional) generate the DB-schema input
Tables whose unique index lives only in the DB (`managed = False` / manual migrations) are
invisible to source parsing. Dump them so crossrepo can emit column-level rules:
```python
# inside your project's env, with an open DB-API cursor (MySQL/MariaDB)
import schema_dump
schema_dump.dump(["signup_leads", "campaign_lead_staging"], cursor, "schema.json")
```

### 2. run it
```bash
lenscheck crossrepo ./cms ./billing --schema schema.json --writes-only --html out.html
# N repos; PATH@BRANCH analyses a branch in a throwaway worktree (your checkout untouched)
```
Services on **different databases**? Say which database each repo uses, so same-named tables in
different databases are never joined:
```bash
lenscheck crossrepo ./orders ./billing ./audit --db orders=main --db billing=main --db audit=logs
```
Repos not listed share one default database (the old behaviour). A name that isn't one of the repos is
an error, so a typo can't silently fall back.

Or in the UI: `lenscheck serve` → the **Cross-repo** tab → paste repo paths (+ optional
schema path) → Analyze. Live search, kind filters, collapsible rows. Pick a **changed repo** (and
optionally a **since** ref) → **Trace impact** lists the endpoints the change reaches in the other repos
(the same walk as `--changed` below) and draws a **graph** of just those paths: one column of endpoints
per repo, shared tables in the middle, edges for writes / reads / calls / dispatches (dashed = `tail`,
verify). The whole graph isn't drawn: on real repos it is thousands of edges.

## Change impact
`--changed` (blast radius) and `lenscheck review --crossrepo` both walk a **cross-repo dependency graph**
built from the shared tables, API links and task links, starting at the endpoints declared in the
changed files:

| Hit | Means | Walked further? |
|---|---|---|
| **data** | gets the changed data: reads a table the change writes, or is called / dispatched by something that does | yes, up to 3 hops |
| **contract** | depends on a changed route or task (its callers / dispatchers) | no |
| **co-writer** | also writes a table the change writes | no |

```bash
lenscheck crossrepo ./orders ./billing ./notify --changed ./orders --since main --out out.json
# out.json → "impact": {"affected": [{repo, handler, loc, depth, kind, confidence, path}], "repos": {...}}
lenscheck review ./orders --base main --head feature --crossrepo ../billing ../notify
# the review gets a "🔗 Cross-repo impact" section and review JSON gets "crossrepo_impact"
```

Every hit carries the **path** that explains it (`orders sync_orders → writes orders → billing report →
calls /api/notify/ → notify send`). A path through any `tail` link is `verify`, never `exact`. The report
also records each repo's **commit** and **dirty** flag (`repo_meta`), so you know which code it describes.

## Reading it
Start at the 🔴 multi-writer tables with a unique key — that's where a write in one service
can violate the other's constraint (the duplicate-entry class). The per-repo endpoint list
tells you exactly which route/handler in each service to coordinate.

## Known limits
- **API links: `exact` vs `tail`.** Full routes are rebuilt by chaining `include()` prefixes, so a link
  is **exact** when the outbound call path IS the callee's full route (trustworthy). It's **tail**
  (verify) when the callee only has a leaf route or the call carries an extra prefix (e.g. a proxy
  mount). A prefix that can't be folded statically — e.g. legacy's `'{}'.format(settings.URL_PREFIX)`
  mount — yields no full route, so that repo's links stay `tail` rather than claiming false precision.
- **HTTP verbs.** When both sides know the verb and they differ, there's no link: a `requests.get` never
  links to a POST-only class view at the same path. A Django function view serves any verb, so its links
  stay. The link records the call's `method`.
- **Celery links: `exact` vs `tail`.** A `send_task('billing.charge')` is **exact** when the string is the
  task's explicit `@task(name=...)` or its full default name (`module.path.func`). `fn.delay()` only
  names a function, so it's **tail** (verify). A dynamically built task name is skipped, not guessed.
  A repo that defines a same-named task itself is assumed to dispatch its own.
- **Database identity:** without `--db`, tables join on name alone. Two services that each have a
  `users` table in *separate* databases will look shared until you pass `--db`. Access is table-level:
  which columns each service reads or writes is not tracked yet.
- **db_table residue:** background-job writes are now attributed, but a `managed=False` model with
  *no* explicit `db_table` still falls back to the `applabel_model` convention — if two repos name
  that model differently and the real table isn't the convention, the join can miss it.
- **Impact depth:** the graph only knows cross-repo facts. A chain through a table that just one repo
  touches, isn't seen. Seeds are endpoints whose declaring file changed; `review --crossrepo` also seeds
  every endpoint the semantic diff flagged, so a change inside a helper counts when it changes a fact
  (a new write, call or table). `--changed` has no semantic diff and uses the changed files only.
- **Caching:** per-repo provenance is cached by git HEAD + an engine fingerprint (a tool upgrade
  busts it), clean trees only — a dirty working tree always re-extracts. `--no-cache` forces it.
