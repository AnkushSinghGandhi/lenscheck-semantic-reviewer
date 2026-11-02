# What Lenscheck sees, what it doesn't, and how accurate it is

Lenscheck is deterministic static analysis: it parses your code and never runs it. When it can't tell,
it says so (⚠ potential, ? unknown, or an unresolved `<instance>` write) instead of guessing. Unknown is
never shown as safe.

## What it sees

| Edge | Covered |
|---|---|
| **Routes** | Django `urls.py` (`path`, `re_path`, `url`) with `include()` chains and regex fragments; function views and class views (`View`, DRF `APIView` / ViewSets, per-method handlers); Flask `@app.route` / blueprints; FastAPI `@app.get` / routers with prefixes |
| **Background jobs** | Celery `@shared_task` / `@task` / `@periodic_task`, management command `handle()`, signal receivers (`@receiver`, `post_save.connect`) — each analyzed like an endpoint |
| **DB reads / writes** | Django ORM managers and querysets (`get`, `filter`, `values`, `create`, `update`, `save`, `delete`, `bulk_*`, `get_or_create`, …); `filter().update()` counted as one write; FK traversal through `field__related` (not through lookups like `__in`); reverse-FK managers (`check.flip_set`); many-to-many writes to the join table (Django's default `<table>_<field>` or the `through=` model), from either side; methods called on a fetched model instance (`check = Check.objects.get(...); check.create_flip()`), through an FK field (`check.project.update_next_nag_dates()`), through a typed request attribute (`request.project.…`, declared as `project: Project` on the request class) and through a `@property` annotated `-> Model` (`self.owner_profile.…`); reading a `@property` runs its body; reverse accessors by `related_name` (`user.credentials`) and the reverse OneToOne accessor (`user.profile`); `annotate(Count("rel"))` reading the related table; `serializer.save()` / `form.save()` → their `Meta.model`; `get_object_or_404`; custom manager methods; raw SQL (`cursor.execute`, `.raw()`) table names; SQLAlchemy models via `__tablename__`, including `session.execute(select(...))` |
| **External calls** | any call on `requests`, `httpx`, `aiohttp`, `urllib`, `stripe`, `razorpay`, `boto3`, `sendgrid`, `smtplib`; the destination host / path when it's a literal, f-string or resolvable settings constant; the HTTP verb |
| **Async** | Celery `.delay()` / `.apply_async()` / `send_task('name')`, `threading.Thread(target=…)` |
| **Cache** | Django cache reads vs writes / invalidations |
| **Auth** | DRF `permission_classes` / `authentication_classes` (including module constants and `DEFAULT_PERMISSION_CLASSES`), Flask auth decorators (`login_required`, `jwt_required`, …), FastAPI `Depends` / `Security` |
| **PII** | sensitive fields (email, phone, password, address, date of birth, card, national IDs, …) traced to where they go: an external API, a log or a task queue (✓ leaves the system) or back to the client (⚠) |
| **Call following** | helpers in your repo: `self.`/`cls.` methods, `Class.method()`, module functions, sibling-module imports. What a helper returns comes from its `-> Model` / `-> Model | None` / `-> QuerySet[Model]` / `-> tuple[Model, bool]` annotation, else from its `return` statements (including tuple-unpacking `check, rw = helper(...)`). `form.is_valid()` runs the form's `clean()` / `clean_<field>()` (and a serializer's `validate*`). Depth adapts: a branch stops after 3 hops that add nothing new (hard cap 8, 250 functions per endpoint) |

## What it doesn't see (yet)

Measured on real code (see below), these are the biggest gaps, most common first:

1. **Helpers it can't type**: a helper with no return annotation whose `return` is opaque (a registry lookup, a
   dict, a dynamically built object). Its result's methods aren't followed, and a `save()` on it is recorded as
   an unresolved `<instance>` write (flagged, but without the table name).
2. **Work your code triggers implicitly**: signal handlers fired by an ORM write, middleware, `save()` / `delete()`
   overrides reached through a model manager, and `ForeignKey(settings.AUTH_USER_MODEL)` relations (the target
   isn't resolved, so reverse accessors on the user model aren't linked to them).
3. **Foreign-key dereferences**: `check.project` fetches the project (a lazy query, or a join if you
   `select_related`), but a plain dereference isn't recorded as a read. Calling a method on it is followed.
4. **Dynamic dispatch**: `getattr(obj, name)()`, handlers chosen at runtime, plugin registries,
   dependency-injection containers.
5. **Runtime-built queries**: raw SQL whose table name comes from a variable; a model passed in from a
   call site it can't resolve.
6. **Outside the known lists**: HTTP or SDK clients other than the ones above (e.g. a Twilio or Kafka
   client), network calls made through `subprocess`.
7. **Column level**: access is tracked per table, not per column. Read vs write labels aren't scored below:
   a `channel_q = Channel.objects.filter(...)` later `.update()`-d is labelled a read and a write.
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
| DB tables | 72 / 72 found, 0 invented (precision 100%, recall 100%) |
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
app. 15 HTTP endpoints with DB facts, picked at random, checked by hand against the code (following
helpers, model methods, properties and form code). Table level, like the fixtures.

| | Count |
|---|---|
| Tables the endpoints really touch | 57 |
| Named correctly | 55 (96%) |
| Named wrongly (invented) | **0** |
| Missed | 2, both in one endpoint: reverse accessors (`user.credentials` by `related_name`, `user.profile` OneToOne) |
| Endpoint resolved to the wrong handler | 0 |

Four of the 15 are near-identical "add an integration" views. Without them: 37 tables, 35 named correctly
(95%), still 0 invented.

**These numbers are from before the two misses were fixed:** the sample was drawn, scored by hand, and only
then were the misses fixed (reverse accessors now resolve, and re-running the endpoint finds all four of its
tables). The fix isn't counted in the score.

**How the sample was kept honest, and where it stops:** this is the sixth random sample from this one repo.
The first five (75 endpoints) were used to find and fix bugs: a URL resolving to a same-named model method;
reverse-FK managers; methods on fetched instances, FK fields, request attributes and properties; many-to-many
join tables; default table names; helper return annotations and tuple returns; form validation; aggregate
joins. Each fix has a regression test that fails without it. This sample excludes every endpoint seen before
and was drawn from the 38 that were left, so it is smaller and more homogeneous than the first. After 75
endpoints of tuning on one codebase, expect the numbers to be **optimistic for yours**: a second, unrelated
repo is the check that would show by how much.

**What this means for you:** when Lenscheck names a table, it's right. Treat ⚠ and `<instance>` as "look
here", and expect misses where a helper's result can't be typed or effects hide in signals and middleware.

Issues with a small example of a miss are the fastest way to close the gaps above.

## Reading the markers

| Marker | Means |
|---|---|
| ✓ | verified: resolved directly in the code |
| ⚠ | potential: found through a helper, a heuristic, or with part of it unresolved — look at it |
| ? | unknown: Lenscheck couldn't tell (e.g. no auth marker it recognizes) |
| `<instance>` | a write whose table it couldn't name |
| n/a | nothing of that kind found |
