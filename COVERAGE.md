# What Lenscheck sees, what it doesn't, and how accurate it is

Lenscheck is deterministic static analysis: it parses your code and never runs it. When it can't tell,
it says so (⚠ potential, ? unknown, or an unresolved `<instance>` write) instead of guessing. Unknown is
never shown as safe.

## What it sees

| Edge | Covered |
|---|---|
| **Routes** | Django `urls.py` (`path`, `re_path`, `url`) with `include()` chains and regex fragments; function views and class views (`View`, DRF `APIView` / ViewSets, per-method handlers); Flask `@app.route` / blueprints; FastAPI `@app.get` / routers with prefixes |
| **Background jobs** | Celery `@shared_task` / `@task` / `@periodic_task`, management command `handle()`, signal receivers (`@receiver`, `post_save.connect`) — each analyzed like an endpoint |
| **DB reads / writes** | Django ORM managers and querysets (`get`, `filter`, `values`, `create`, `update`, `save`, `delete`, `bulk_*`, `get_or_create`, …); `filter().update()` counted as one write; FK traversal through `field__related` (not through lookups like `__in`); reverse-FK managers (`check.flip_set`); `serializer.save()` / `form.save()` → their `Meta.model`; `get_object_or_404`; custom manager methods; raw SQL (`cursor.execute`, `.raw()`) table names; SQLAlchemy models via `__tablename__`, including `session.execute(select(...))` |
| **External calls** | any call on `requests`, `httpx`, `aiohttp`, `urllib`, `stripe`, `razorpay`, `boto3`, `sendgrid`, `smtplib`; the destination host / path when it's a literal, f-string or resolvable settings constant; the HTTP verb |
| **Async** | Celery `.delay()` / `.apply_async()` / `send_task('name')`, `threading.Thread(target=…)` |
| **Cache** | Django cache reads vs writes / invalidations |
| **Auth** | DRF `permission_classes` / `authentication_classes` (including module constants and `DEFAULT_PERMISSION_CLASSES`), Flask auth decorators (`login_required`, `jwt_required`, …), FastAPI `Depends` / `Security` |
| **PII** | sensitive fields (email, phone, password, address, date of birth, card, national IDs, …) traced to where they go: an external API, a log or a task queue (✓ leaves the system) or back to the client (⚠) |
| **Call following** | helpers in your repo: `self.`/`cls.` methods, `Class.method()`, module functions, sibling-module imports, return types of helpers. Depth adapts: a branch stops after 3 hops that add nothing new (hard cap 8, 250 functions per endpoint) |

## What it doesn't see (yet)

Measured on real code (see below), these are the biggest gaps, most common first:

1. **Work inside model instance methods called on a variable**: `check.create_flip()`,
   `project.update_next_nag_dates()`, `check.ping()`. The variable's type is often known, but the
   method body isn't followed yet.
2. **Many-to-many join-table writes**: `channel.checks.add(*checks)` writes the join table; it isn't
   recorded.
3. **Instances from helpers or the request**: `request.profile.save()`, or `check.save()` on an object a
   helper returned, is recorded as an unresolved `<instance>` write: flagged, but without the table name.
4. **Dynamic dispatch**: `getattr(obj, name)()`, handlers chosen at runtime, plugin registries,
   dependency-injection containers.
5. **Runtime-built queries**: raw SQL whose table name comes from a variable; a model passed in from a
   call site it can't resolve.
6. **Outside the known lists**: HTTP or SDK clients other than the ones above (e.g. a Twilio or Kafka
   client), middleware side effects, network calls made through `subprocess`.
7. **Column level**: access is tracked per table, not per column.
8. **Installed packages**: only your repo's code is analyzed. Dependency bumps are scanned separately
   (`scan_deps`).

## How accurate is it?

### 1. Labelled fixture corpus (regression suite)

57 endpoints across Django + DRF, Flask and FastAPI with Django ORM and SQLAlchemy, including deliberate
traps ([lenscheck-extractor-tests](https://github.com/AnkushSinghGandhi/lenscheck-extractor-tests),
`megashop` + `torture`). Ground truth was written by hand from what each endpoint does.

| Edge | Result |
|---|---|
| Routes found | 57 / 57 |
| DB tables | 71 / 71 found, 0 invented (precision 100%, recall 100%) |
| External calls | 11 / 11 |
| Async dispatch | 7 / 7 |
| Cache | 7 / 7 |
| Auth (open / closed / unknown) | 29 / 29 |
| PII | 16 / 16 |

**Read this with care:** the fixtures were written by the same author as the tool, and every miss they
ever exposed was fixed. They prove it doesn't regress on known patterns; they don't predict accuracy on
your code. That's what the next check is for.

### 2. Real-world spot check

[healthchecks](https://github.com/healthchecks/healthchecks) at `ab4b5c4` (2026-11-01), a public Django
app: 164 endpoints found (147 HTTP), 84 HTTP endpoints with DB facts. 15 of those, picked at random,
were checked by hand against the code, following helpers. Table level, like the fixtures.

| | Count |
|---|---|
| Tables the endpoints really touch | 58 |
| Named correctly | 38 (66%) |
| Named wrongly (invented) | **0** |
| Flagged as an unresolved `<instance>` write | 1 |
| Missed | 19 — 12 inside model instance methods, 7 many-to-many join-table writes (gaps 1 and 2 above) |
| Endpoint resolved to the wrong handler | 0 |

An earlier random sample of 15, used while improving the tool, found two bugs that were fixed before the
sample above was drawn: a URL `views.ping` resolved to a same-named model method, and reverse-FK managers
(`check.flip_set`) weren't read. So the numbers above come from a fresh sample, not the one the fixes were
made against.

**What this means for you:** when Lenscheck names a table, it's right. It doesn't yet see everything:
budget for roughly a third of DB effects being missed in code that leans on model methods and
many-to-many helpers, and treat ⚠ / `<instance>` as "look here".

One repo and 15 endpoints is a spot check, not a benchmark. Issues with a small example of a miss are
the fastest way to close the gaps above.

## Reading the markers

| Marker | Means |
|---|---|
| ✓ | verified: resolved directly in the code |
| ⚠ | potential: found through a helper, a heuristic, or with part of it unresolved — look at it |
| ? | unknown: Lenscheck couldn't tell (e.g. no auth marker it recognizes) |
| `<instance>` | a write whose table it couldn't name |
| n/a | nothing of that kind found |
