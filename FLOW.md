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

## Known limits

- **Order inside one statement is approximate**: facts come before the statement's calls, and an outer
  call is ordered before the calls in its arguments.
- **Nested defs are inlined** into their enclosing step; they are not separate callable targets.
- **Only analyzer-resolved calls are followed** — an unresolved dynamic call is not traced.
- **Cold first run builds the fact cache** (the same cache `provenance`/`crossrepo` use); on c360-sized
  repos that is roughly a minute, then it is cached.
- **Cross-repo hops are single-depth** — the links a hop's own endpoint makes are not followed.
- **HTML/serve output, front-end parsing and any natural-language resolver are later milestones** —
  this command is static.
