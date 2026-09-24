# Interprocedural follow fix — bare-call / same-name collision

Tracking doc for the extractor DB-read tracing fix. Plain-language, kept current.

> ## ⏩ RESUME HERE (handoff — read this first)
>
> **Status: fix DONE + fully audited + CPModels FP fixed. Loss audit: 107/114 CORRECT_FP, 7
> pre-existing gaps (biggest, `get_object_or_404`, fixed). Gain audit + code review COMPLETE:
> gains 198 CORRECT_RECALL / 12 NEW_FP (94%), code review = SHIP (1 nit, fixed). Of the 12 FPs,
> the 4 `CPModels` alias phantoms are now FIXED (Edit 5, `assigned_names` guard) — deterministic
> pre/post diff = exactly those 4 removals, zero collateral. The other 8 (lead-push over-credit)
> are a static-analysis precision limit, tracked. Extractor NONDETERMINISM also FIXED (Edit 6,
> `sorted()` the follow loops — c360 now byte-identical across hash seeds). Tests: prism 11/11,
> reviewer2 127/127. UNCOMMITTED (prism + reviewer2, byte-identical). 6 edits total in analyzer.py.**
>
> - `extractor/analyzer.py` has **6 edits** now (follow-fix ×3 → get_object_or_404 read → CPModels
>   alias guard → determinism `sorted()`). Details in "Fix v2" below. Mirrored in reviewer2.
> - **11/11 unit tests pass** (`cd prism && python3 -m pytest tests/ -q`) — +2 get_object_or_404 tests.
> - c360 before/after (**clean, deterministic** — authoritative): **152 endpoints gain** real reads
>   (211 tables), **90 lose** false-positive reads (410 tables). (Old noisy run: 158/101.)
>   Audit: **107/114 loss verdicts = CORRECT_FP** (noise removal). The 7 REGRESSIONs were all
>   the extractor never actually *seeing* a real read (untraced path) that HEAD's noise happened
>   to cover — not my fix dropping a followable path.
> - **Airtight proof** the follow-fix is right: `get_degrees_registration_push` → HEAD reported
>   14 tables (12 bogus AdBuddy reads via bare `list()`→DRF `def list`), fix reports the correct 2.
>   HEAD's `find_func("get")` had **246** colliding class-method defs.
>
> **Next steps:** see the **📋 Progress tracker** section right below this box — it's the single
> source of truth for what's done and what's open (commit, re-baseline, controllers.py, the 8
> lead-push FPs, older recall gaps, parse-error visibility).
>
> **Re-run the validation harness (all in /tmp, may be wiped):**
> ```
> # HEAD snapshot: mkdir -p /tmp/head_extractor && cd prism
> #   git show HEAD:extractor/analyzer.py > /tmp/head_extractor/analyzer.py
> #   cp extractor/frameworks.py extractor/__init__.py /tmp/head_extractor/
> # diff:  EXTRACTOR_DIR=<dir> python3 /tmp/diff_harness.py <repo-root> > /tmp/lc_wip/<name>.<ver>.json
> # show:  python3 /tmp/show_diff.py <name>   (reads /tmp/lc_wip/<name>.{head,fix}.json)
> # roots: c360=/home/ankush/Downloads/cnext/c360 ,
> #   cnext=/home/ankush/Downloads/cnext/cnext/cnext_backend , cnext_cms=/home/ankush/Downloads/cnext/cnext_cms
> # Re-run the audit workflow: script saved at
> #   ~/.claude/projects/-home-ankush-Downloads-sir-prism/.../workflows/scripts/verify-follow-fix-wf_8db51676-cab.js
> ```

---

## 📋 Progress tracker (single source of truth)

Plain-language status of everything. Check = done. Each open item says **what**, **why it
matters**, **effort**, and **who/what it's waiting on**.

### ✅ Done and verified

- [x] **Core follow-fix** — a bare call `foo()` no longer wrongly jumps into a class method
  `def foo(self)`; same-name facades (`Helper.load()` → module `load()`) are followed. *(edits 1–3)*
- [x] **`get_object_or_404(Model)` counts as a read** — Django shortcut that runs a real query. *(edit 4)*
- [x] **`CPModels` phantom fixed** — `get_object_or_404(<local-alias-var>)` no longer invents a fake
  table. Proven surgical: deterministic diff = exactly the 4 removals, nothing else. *(edit 5)*
- [x] **Determinism** — the 3 follow loops now `sorted()`, so results don't wobble run-to-run.
  Proven: c360 byte-identical across two hash seeds. *(edit 6)*
- [x] **Full audit done** — losses 107/114 correct, gains **198/210 (94%)**, code review = **ship**.
- [x] **reviewer2 synced** byte-identical + tests copied in + committed + pushed.
- [x] **Tests green** — prism **11/11**, reviewer2 **127/127**.

### ⬜ Open — needs a decision or work

- [x] **1. Commit the change — DONE.** 2 commits in both prism and reviewer2, pushed.
  - `b9335fc` / `adb59d3` — `extractor fix bare-call precision and same-name facade recall`
  - `8b52bcd` / `98e8baf` — `extractor detect get_object_or_404 reads skip local aliases and sort follow loops`
- [x] **2. Clean re-baseline of the audit numbers — DONE.** Backported `sorted()` into the HEAD
  snapshot too and re-ran before/after with a fixed seed (both sides deterministic; clean HEAD
  verified identical across two seeds). **Authoritative numbers: GAINS 152 eps / 211 tables,
  LOSSES 90 eps / 410 tables** (old noisy run was 158/208 and 101/507 — the wobble was almost all
  on the loss side). Sanity: smoking-gun endpoint = 2 tables, 0 `CPModels` phantoms. The gain
  audit ran on the old set (208 tables) but the clean set (211) is materially the same, so the
  94%-recall / 8-lead-push-FP findings carry over unchanged.
- [ ] **3. Fix `cnext_cms` `controllers.py`.** *Why:* `class ClientEntitySearchView(APIView):` has an
  **empty body** → whole file won't parse → those endpoints silently vanish; blocks the original
  entity-search validation. *Effort:* small edit, but it's **company source** and the real view body
  is missing in this checkout. *Waiting on:* **you** — only you can restore it from the real source.
- [ ] **4. The 8 lead-push false reads.** *Why:* the tool credits AdBuddy tables that are only read
  inside an `if` branch that never runs (e.g. `if campaign_ids is None:`). *Effort:* **BIG** — needs
  value/branch-reachability analysis, a different class of analysis than call-following. *Decision:*
  **accept + track** (recommended — it's 8 FPs vs ~470 removed) **or** invest later.
- [ ] **5. Older recall gaps (un-masked by this fix).** Model reads the tool still can't follow:
  `@cached_property` model methods; `var = Mod.Class(); var.method()`; `Model.objects.using(...)`.
  *Why:* minor missed reads. *Effort:* medium each. *Status:* tracked, not blocking.
- [ ] **6. Parse-error visibility.** *Why:* a single syntax error makes a whole file's endpoints read
  as empty and look "clean" (exactly what hid case 3). *Fix:* surface a ⚠ instead of silently
  dropping. *Effort:* small–medium. *Status:* tracked.

**Legend:** *Waiting on: your go* = ready, just needs the word. *Waiting on: you* = needs something
only you can provide. No owner = a future task, not urgent.

---

## The problem (from handoff)

The reviewer under- **and** over-reports DB tables when reads happen in helper
functions called by the handler. Two failure modes:

1. **False negative (recall):** a read reached through a *same-name facade* was
   lost. `Helper.load()` delegating to a module-level `load()` resolved back to
   `Helper.load` itself, so the module `load()`'s DB read was never walked.
   (Real case: cnext_cms `clients/entity-search` missed `colleges`; regression
   test `test_same_name_facade_is_followed` in `tests/test_follow.py`.)

2. **False positive (precision):** discovered while validating the fix — a bare
   call like `list(...)`, `get(...)`, `str(...)` was followed into an unrelated
   **class method** of the same name. In c360, `get` has **246** defs (mostly DRF
   `def get(self, request)` ViewSet methods); a bare builtin `list(degrees)` was
   followed into a random `def list(self, request)` that transitively reads the
   whole AdBuddy model set. Endpoints were credited with tables they never touch.

Both are dangerous for a review tool. FN hides real behaviour; FP floods the
report with noise and erodes trust.

## Root cause

`RepoIndex._index_defs` indexed **class methods** into `self.funcs` under their
bare name, alongside module-level functions. `find_func(name)` then couldn't tell
them apart:

- A bare `foo()` call **cannot** invoke a class method in Python (needs
  `self.`/`Cls.`), yet `find_func` happily bound bare calls to same-named methods.
- With 246 `get` methods, `_pick` returned some arbitrary one → garbage follow.
- For the facade case, `Helper.load` (a method) and module `load` collided;
  `_pick` returned the method (index order) → self-loop, real read lost.

Confirmed empirically:
```
FUNC load -> [(8, app.py Helper.load), (10, app.py module load)]
find_func('load') picks line 8   # the method — wrong
```

## Fix v1 (WRONG — reverted in place)

First attempt: stop indexing class methods in `self.funcs` entirely (only
top-level funcs). Made the unit test pass, **but broke handler resolution**:
Django routes to classmethod views by name (`path(..., Helper.search)`).
`analyze_handler` finds the handler via `find_func` after `find_class` misses — so
it *needs* class methods in the index. Result: 162 c360 endpoints (e.g.
`search_provider`, a `@classmethod` reading `College`/`CollegeCourse` directly)
resolved to "handler not found" → **zero tables**. A real regression.

Lesson: `find_func` serves two masters — **handler resolution** (needs methods)
and **bare-call following** (must exclude methods).

## Fix v2 (CURRENT) — surgical

Keep indexing both, tag each `funcs` entry with a `toplevel` flag; let the caller
choose. Three edits in `extractor/analyzer.py`:

1. `_index_defs(node, path, toplevel=True)` — recurse into a `ClassDef` with
   `toplevel=False`; store `(node, path, toplevel)`. Also fold
   `ast.AsyncFunctionDef` into the same branch (previously async defs fell through
   to `else` and had their bodies wrongly recursed while the def itself was never
   indexed — so async module helpers / async classmethod views weren't followable).

2. `find_func(name, near="", toplevel_only=False)` — when `toplevel_only`, drop
   class-method entries before `_pick`; always returns a 2-tuple `(node, path)` so
   callers are unchanged.

3. `_walk_follow` bare-call loop uses `find_func(fname, near, toplevel_only=True)`.
   Handler resolution in `analyze_handler` stays `toplevel_only=False` (finds
   classmethod views).

Net: bare `foo()` binds only to a module-level `foo` (or nothing); classmethod
views still resolve as handlers; same-name facades follow correctly.

**Edit 4 (added after the audit — see below):** `visit_Call` now records
`get_object_or_404(Model, …)` / `get_list_or_404(Model, …)` as a **read**. Django's
shortcut runs a real `Model.objects.get/filter` under the hood; the ORM matcher
missed it (no literal `.objects`). Previously masked by the spurious follow noise;
un-masking it via the follow-fix made this gap visible, so it's fixed here too.

**Edit 5 (added after the gain audit):** the Edit-4 read is now **guarded against
local model aliases**. `FactCollector` gains an `assigned_names` set (populated in
`visit_Assign` from every simple `Name` assignment target); the `get_object_or_404`
read is skipped when its first-arg name is in that set. This kills the `CPModels`
phantom — `CPModels = apps.get_model(…)` / `CPModels = RealModel` then
`get_object_or_404(CPModels, pk=pk)` was crediting a nonexistent table literally
named `CPModels`. Safe because a real model is imported, never locally rebound; and
because using a name before assigning it is a Python `NameError`, the assign always
precedes the call, so the in-walk-order set is provably complete. Verified by a
deterministic (fixed-hash-seed) pre/post diff on c360 = exactly the 4 `CPModels:read`
removals, nothing else. +2 regression tests in `tests/test_follow.py`.

**Edit 6 (determinism):** `_walk_follow`'s three follow loops now iterate
`sorted(set(...))` instead of bare `set(...)`. Set order depends on `PYTHONHASHSEED`,
and under `FOLLOW_BUDGET = 60` that made *which* calls got followed — and thus the
per-endpoint table set — vary run-to-run. Now c360 extracted under two different hash
seeds is byte-identical. See "Open robustness notes".

## Audit results (workflow `verify-follow-fix`, run `wf_8db51676-cab`)

17 agents adversarially audited the c360 diff. It ran partially before hitting the
**API session limit** (gain-audit + code-review agents died; loss agents mostly
finished). Loss verdicts: **107 CORRECT_FP, 7 REGRESSION, 0 uncertain**.

The 7 REGRESSIONs were investigated by hand. **None is the follow-fix wrongly
dropping a followable path.** Each is the extractor never *tracing* a genuine read
that HEAD's over-broad noise happened to cover. Root causes:

1. **`get_object_or_404(Model)` not detected as a read** — `get_form_detail_view`
   (`AdBuddyForm`), `APILeadSyncFormView.get` via module helper `get_form_object`
   (`AdBuddyForm`). → **FIXED** (Edit 4). Recovered across many endpoints; c360
   gains rose 38 → 158.
2. **`@cached_property` model methods not traced** — `form.colleges` /
   `form.exams` (attribute access) run `College/Exam.objects.filter(...)` inside a
   property. Needs property-follow. → **residual gap**, tracked.
3. **`var = Mod.Class(); var.method()` not followed** — case 7: module `l()` does
   `obj = CommonMethods(); obj.l(...)` reading `UrlAlias`; extractor only follows
   inline `Cls().m()` / `Cls.m()`, and `_model_of` doesn't type `obj` from a
   `mod.ClassName()` attribute-call. → **residual gap**, tracked.

Because gain-audit + code-review didn't finish, the 158 gains and the code change
have NOT been independently agent-verified — but the gains are dominated by the
Edit-4 recovery (real reads) and the same-name-facade recall, and the code was
hand-reviewed. Re-run the workflow after the limit resets for full coverage.

## Audit results — re-run (gains + code review), 2026-09-23

The limit reset. Rebuilt the audit inputs against the CURRENT post-Edit-4 diff
(`c360_gains.json` now 158 eps / 208 tables; stale 38-ep version saved as
`c360_gains.stale38.json`). Ran **1 code-review agent + 8 gain-audit agents** (all
completed — budget held this session).

**Code review = SHIP.** Adversarially verified: arity of the new 3-tuple `funcs`
storage is fully contained (only `find_func` consumes it, always repacks to a
2-tuple); handler resolution still finds classmethod views; the `AsyncFunctionDef`
fold neither loses nor double-counts; the `toplevel` flag is correct through
control-flow nesting; Edit 4's `get_object_or_404` guard has no double-count
(deduped by `model:kind`) and no false positive on lowercase args; no
infinite-loop / budget-exhaustion risk. **One nit (fixed):** the inline comment on
`self.funcs` still said `[(node, file)]` — corrected to `[(node, file, toplevel)]`.
9/9 prism tests + 125/125 reviewer2 tests pass.

**Gain audit: 198 CORRECT_RECALL / 12 NEW_FP / 0 uncertain (~210 pairs → 94%).**
Dominant legit mechanisms: (b) `get_object_or_404(Model, …)` recovery, and (a)
bare-call → module-level function / same-name-facade follow (incl. the textbook
`get_exam_level(exam_id=…)` binding to the module fn, not the same-name
`def get_exam_level(self, request)`). The 12 NEW_FPs split into 2 root causes:

1. **`CPModels` alias phantom (4 FP) — cleanly fixable, a real Edit-4 defect.**
   `add_edit_term` and `add_edit_field_mapping` do `CPModels = apps.get_model(…)` /
   `CPModels = CPPredictorFieldsMapping` then `get_object_or_404(CPModels, pk=pk)`.
   Edit 4 keys on the first-arg *identifier* (`CPModels`) and credits a table by
   that name — but no `class CPModels` exists; it's a local alias for different
   models. Fix: skip `get_object_or_404`/`get_list_or_404` when the first-arg Name
   is a **local assignment target** in the function (real models are imported, never
   locally rebound). `visit_Assign` already exists — add an `assigned_names` set and
   gate on it. (Note: this phantom-name risk is systemic to the Capitalized-name
   heuristic — `CPModels.objects.get()` would mis-credit too — but Edit 4 newly
   exposed these 4 instances.)
2. **Lead-push / apply-flow over-credit (8 FP) — static-analysis precision limit,
   arguably accept + track.** `ebook_download`, `DashboardCollegeLeadPushCall`,
   `get_exam_update` now follow the `apply_from_form` / lead-push chain and pick up
   sibling AdBuddy tables (`AdBuddyCampaignOrder`, `AdbuddyCampaignEntities`,
   `AdBuddyFormCollege`, `AdBuddyFormStreamEducationLevel`, `AdBuddySpreadsheet`)
   that are read only in **branch-unreachable** paths (e.g. `resolve_du`'s
   `if campaign_ids is None:` branch, never taken since callers pass a concrete id)
   or in adjacent non-reached functions. `AdBuddyCampaignOrder` is a hard FP (no
   reachable read at all); the others are branch-sensitive. Following the chain is
   otherwise correct — sibling tables on the same flow
   (`AdbuddyCampaignsDeliveryUnit`, `AdBuddyLeadDeliveryLog`, `AuditLog`, `States`,
   `AdBuddyUsersInteraction`) are legitimately recalled. Killing these needs
   intra-procedural branch/reachability analysis — beyond a caller-following tool.

**Net precision verdict:** the fix REMOVED ~470 false-positive reads (107/114 losses
= CORRECT_FP) and RECOVERED 198 genuine reads, at the cost of 12 new FPs (4 cleanly
fixable). A large net precision + recall win.

## Validation (before/after diff on real repos)

Harness: `/tmp/diff_harness.py` runs `analyze_repo` and fingerprints each
endpoint's E3 `model:kind` set. Compared HEAD (pre-fix) vs working tree (fix).
HEAD extractor snapshot at `/tmp/head_extractor`.

| repo      | endpoints | notes |
|-----------|-----------|-------|
| c360      | ~1600     | **clean re-baseline (both sides deterministic):** gained on **152** eps (211 tables), lost on **90** eps (410 tables). Losses = FP removals (bare `list`/`get`/`str` → DRF method); gains = real recall + `get_object_or_404` reads. (Old *noisy* run was 158/101; wobble was almost all on the loss side. Follow-fix alone was ~38 gain / ~94 loss.) |
| cnext     | 407       | **zero change** — no bare-call→classmethod collisions in this tree |
| cnext_cms | 448       | **zero change** — EXPLAINED: `adbuddy/clients/api/controllers.py` still fails to parse, so those endpoints (the entity-search set) are dropped in BOTH versions → nothing to diff. **Exact cause found:** line 404 `class ClientEntitySearchView(APIView):` has an empty body (SyntaxError: expected an indented block). Reads like dead leftover after the search moved to `ClientDBSearchView` (line 407). The one parseable entity endpoint (`<int:cid>/entities/`) already resolves `College`/`Client`/`AdbuddyCampaignsDeliveryUnit` correctly. Real validation blocked on the user resolving that dead class. |
| toolshub  | 145       | gained on 1 ep, lost on 0 — pure recall |
| animelist | 4         | no change |
| dashboard | 60        | no change |

**Mechanism proof (the smoking gun):** in HEAD, `find_func("get")` had **246** candidate
defs — almost all DRF `def get(self, request)` ViewSet methods. `find_func("list")` picked a DRF
`def list` in `list_api.py`. So a bare builtin `list(degrees)` in `get_degrees_registration_push`
followed into an unrelated ViewSet and dragged in 12 AdBuddy tables. The fix's `toplevel_only=True`
resolves bare calls only to module-level funcs → the spurious follow vanishes. `JsonResponse`/`dict`
had 0 repo defs (correctly unresolved in both).

Spot-check that nails the mechanism: `dj-api/get-degrees-registration-push`
(`get_degrees_registration_push`, a module func reading only `DegreeDomain` +
`Degrees`, no bare helper calls) —
- HEAD: **14 tables** (12 spurious AdBuddy reads "via call", from bare `list(...)`
  → `list_api.py:13` DRF method → helper chain).
- FIX: **2 tables** (`DegreeDomain`, `Degrees`) — correct.

Unit tests: **9/9 pass**, including `test_same_name_facade_is_followed` and
`test_delegator_and_closure_collision`.

## Where we are (historical log — for OPEN items see the 📋 Progress tracker at the top)

- [x] Root cause pinned (class methods shadow module funcs in `self.funcs`).
- [x] Surgical fix applied in `extractor/analyzer.py` (3 edits) — **uncommitted**.
- [x] Unit suite green (9/9).
- [x] c360 before/after diff: losses confirmed as false-positive removals via the
      `get_degrees_registration_push` trace.
- [x] Explained cnext / cnext_cms zero-change (controllers.py unparseable → dropped
      in both; no collisions in cnext). Real entity-search validation still blocked
      on the user fixing controllers.py.
- [x] Ran the diff on remaining Django repos (animelist, toolshub, dashboard) — see
      table. Only recall gains, no losses.
- [x] Adversarial audit (workflow `verify-follow-fix`, run `wf_8db51676-cab`) — ran
      partially (API session limit). 107/114 loss verdicts CORRECT_FP; 7 REGRESSIONs
      all traced to pre-existing un-masked gaps (not fix bugs). See "Audit results".
- [x] Fixed the dominant un-masked gap: `get_object_or_404` read detection (Edit 4).
- [x] Re-ran the audit (gains + code review) after the limit reset — COMPLETE. Gains
      198 CORRECT_RECALL / 12 NEW_FP (94%); code review = SHIP (1 nit, fixed). See
      "Audit results — re-run". New: 2 FP root causes (CPModels phantom = fixable;
      lead-push over-credit = precision limit).
- [x] `CPModels` alias phantom (4 FP): FIXED (Edit 5 — `assigned_names` guard). Deterministic
      pre/post diff (fixed PYTHONHASHSEED) = exactly the 4 `CPModels:read` removals, zero
      collateral. +2 regression tests (`test_get_object_or_404_reads_real_model`,
      `test_get_object_or_404_skips_local_model_alias`).
- [x] Fixed the `CPModels` alias phantom (Edit 5) + determinism `sorted()` (Edit 6).
- [x] Propagated all edits to reviewer2 (byte-identical); reviewer2 suite 127 pass.
- [x] Re-ran gain audit + code review after the limit reset (198/210 = 94%, ship).

**→ Remaining OPEN items (commit, re-baseline, controllers.py, the 8 lead-push FPs, older
recall gaps, parse-error visibility) all live in the 📋 Progress tracker at the top of this doc.**

## Open robustness notes (separate from this fix)

- Extractor **silently drops** all endpoints in an unparseable file. Should surface
  a ⚠ parse-error instead (a syntax error = whole app reads empty, looks "clean").
- `var = Model.objects.using(...)` then `var.get()` — `.using()` fold may defeat
  Model detection. Not addressed here.
- ~~Extractor is NONDETERMINISTIC under the follow budget.~~ **FIXED (Edit 6).**
  `_walk_follow`'s three follow loops iterated bare `set(...)` (`self_calls`,
  `func_calls`, `typed_calls`) while `FOLLOW_BUDGET = 60` caps functions followed per
  endpoint; set iteration order depends on `PYTHONHASHSEED`, so for endpoints whose
  call graph exceeds the budget, *which* functions got followed (and thus the table
  set) varied run-to-run. Found while verifying Edit 5: two separate `fix` extractions
  differed on ~16 endpoints with zero code change. **Fix:** all three loops now iterate
  `sorted(set(...))`. **Proof:** c360 extracted under `PYTHONHASHSEED=1` and `=42` is
  now byte-identical (was ~16 eps different). **Clean re-baseline now done** (backported
  `sorted()` into the HEAD snapshot too, both sides extracted with a fixed seed): authoritative
  counts are **152 eps / 211 tables gained, 90 eps / 410 tables lost** (old noisy run: 158/208 and
  101/507 — the wobble was almost entirely on the loss side). Mechanism-level audit findings
  unchanged. Clean artifacts: `/tmp/lc_wip/c360.head.clean.json`, `c360.fix.clean.json`.

## Key files

- Fix: `extractor/analyzer.py` — `_index_defs`, `find_func`, `_walk_follow`.
- Test: `tests/test_follow.py`.
- Diff harness: `/tmp/diff_harness.py`, `/tmp/show_diff.py`; HEAD snapshot
  `/tmp/head_extractor`; outputs `/tmp/lc_wip/*.json`.
</content>
