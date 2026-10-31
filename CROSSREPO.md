# `lenscheck crossrepo` — cross-service data-consistency

Most consistency bugs live *between* services, not inside one. `crossrepo` takes two or
more repos that share a database and finds every table they share — down to
`route → handler → file:line`, across routed views **and background jobs (celery tasks,
management commands, signal receivers)** — and flags where **two services write the same
table** (the cross-service race / constraint conflict). It also surfaces **HTTP coupling**:
where one service's endpoint calls a route another service serves. Deterministic, no LLM.

## What it reports
Per shared table: which repos **write** vs **read** it, down to `route → handler → file:line`,
classified as:
- 🔴 **multi-writer** — 2+ services write it → coordinate schema & write logic.
- 🟡 **producer → consumer** — one writes, others read → the writer owns the shape.
- **read-only** — nobody writes it (shared lookup) → usually safe.

With `--schema` it adds the precise column-level rule:
*"coordinate on unique key (form_id, unit_id, email) — both writers must agree."*

Below the tables it lists **cross-repo API calls** — `repo A → call /path → repo B route` —
so you see both ways two services couple: through the database and over HTTP.

## Where it lives (the three repos)
| repo | role |
|---|---|
| **lenscheck-semantic-reviewer2** | local engine + `serve` UI tab — the free, on-prem path |
| **lenscheck-semantic-reviewer** (prism / PyPI) | thin client — extracts locally, uploads to the cloud |
| **lenscheck-cloud** | the paid ranking — risk-scores the shared tables worst-first |

Extraction stays client-side; the **risk-ranking is the paid cloud differentiator**.

## Full flow

### 1. (optional) generate the DB-schema input
Tables whose unique index lives only in the DB (`managed = False` / manual migrations) are
invisible to source parsing. Dump them so crossrepo can emit column-level rules:
```python
# inside your project's env, with an open DB-API cursor (MySQL/MariaDB)
import schema_dump
schema_dump.dump(["signup_leads", "campaign_lead_staging"], cursor, "schema.json")
```

### 2a. local (free)
```bash
lenscheck crossrepo ./cms ./billing --schema schema.json --writes-only --html out.html
# N repos; PATH@BRANCH analyses a branch in a throwaway worktree (your checkout untouched)
```
Or in the UI: `lenscheck serve` → the **Cross-repo** tab → paste repo paths (+ optional
schema path) → Analyze. Live search, kind filters, collapsible rows.

### 2b. cloud (paid, ranked)
```bash
lenscheck crossrepo ./cms ./billing --schema schema.json   # needs LENSCHECK_API_KEY
```
The client extracts per-repo provenance locally and uploads it; the cloud merges + ranks
the shared tables worst-first (multi-writer, writing-endpoint count, PII-to-client,
PII-off-platform, unique-key coordinate) and returns the triage.

## Reading it
Start at the 🔴 multi-writer tables with a unique key — that's where a write in one service
can violate the other's constraint (the duplicate-entry class). The per-repo endpoint list
tells you exactly which route/handler in each service to coordinate.

## Known limits (see lenscheck-plan for the roadmap)
- **API links: `exact` vs `tail`.** Full routes are rebuilt by chaining `include()` prefixes, so a link
  is **exact** when the outbound call path IS the callee's full route (trustworthy). It's **tail**
  (verify) when the callee only has a leaf route or the call carries an extra prefix (e.g. a proxy
  mount). A prefix that can't be folded statically — e.g. legacy's `'{}'.format(settings.URL_PREFIX)`
  mount — yields no full route, so that repo's links stay `tail` rather than claiming false precision.
- **db_table residue:** background-job writes are now attributed, but a `managed=False` model with
  *no* explicit `db_table` still falls back to the `applabel_model` convention — if two repos name
  that model differently and the real table isn't the convention, the join can miss it.
- **Caching:** per-repo provenance is cached by git HEAD + an engine fingerprint (a tool upgrade
  busts it), clean trees only — a dirty working tree always re-extracts. `--no-cache` forces it.
