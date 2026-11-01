/* Lenscheck glossary — one source of truth for every term the UI shows, the hover tooltip, and the
   📖 A–Z list. A page marks an element with data-g="key" (plus optional data-gx="this instance's
   context"); hover or keyboard focus shows the entry. Plain text only — always rendered with textContent. */
(function () {
const G = window.LC_GLOSSARY = {
  // ---- severity (diff_pr.endpoint_risk / _job_risk / the PR diff) ----
  "severity": {t: "Severity", d: "How risky a finding is: 🔴 critical, 🟠 high, 🟡 medium, 🟢 low. It comes from fixed rules over the facts (auth, DB writes, PII, external calls), never from a model's opinion. Read 🔴 first."},
  "sev-crit": {t: "🔴 Critical", d: "Fix or justify before merging. Examples: an endpoint anyone can call (open auth) that writes to the DB or exposes personal data, auth weakened from protected to open, or a new PII signal. Background jobs are never 🔴 — nothing outside can call them."},
  "sev-high": {t: "🟠 High", d: "Serious, review carefully: a money/payment path, a new external call on a payment route, or a job that sends PII off-platform or writes on a money path."},
  "sev-med": {t: "🟡 Medium", d: "Worth a look: a DB write, an external call, a new async dispatch, or a handler that couldn't be resolved in the repo."},
  "sev-low": {t: "🟢 Low", d: "Low risk: read-only, a cache change, or a refactor with no semantic difference."},
  "sev-tally": {t: "Severity tally", d: "How many findings this review produced at each severity."},
  "worst-first": {t: "Worst-first", d: "Lists are sorted by severity, so the riskiest items are always at the top."},

  // ---- change kinds (diff_pr.diff) ----
  "ch-new": {t: "🆕 New endpoint", d: "This PR adds an entrypoint that didn't exist before. Rated by what it does: auth, writes, PII, external calls."},
  "ch-changed": {t: "✳ Changed", d: "The entrypoint existed before, but its behaviour changed: different auth, or new DB / external / async / cache / PII facts."},
  "ch-removed": {t: "➖ Removed endpoint", d: "This PR deletes an entrypoint. Check that nothing (another service, a frontend, a cron) still calls it."},
  "ch-refactor": {t: "♻ Refactor", d: "The handler was renamed or moved, but every semantic fact (auth, tables, calls) is the same."},
  "change": {t: "Change kind", d: "What happened to the entrypoint in this PR: new, changed, removed, or a pure refactor."},

  // ---- entrypoints ----
  "entrypoint": {t: "Entrypoint", d: "Any place code starts running: an HTTP API endpoint, a celery task, a management command, or a signal receiver. All four are analyzed the same way."},
  "ent-http": {t: "🌐 API (HTTP endpoint)", d: "A URL route a client can call. Its auth matters: an open route is reachable by anyone."},
  "ent-task": {t: "⏱ Task (celery)", d: "A background job run by a worker. Not reachable over HTTP, so auth doesn't apply; what matters is what it writes and where its data goes."},
  "ent-command": {t: "⌘ Command", d: "A Django management command (`manage.py <name>`), usually a cron or one-off script. Not reachable over HTTP."},
  "ent-signal": {t: "⚡ Signal receiver", d: "Code Django runs automatically when something happens (e.g. after a model is saved). It runs inside whatever endpoint triggered the save."},
  "trigger": {t: "↻ Trigger", d: "What starts this job: celery (a worker picks it up), manage.py (run as a command / cron), or a signal like post_save:Order (fires when an Order is saved)."},
  "entrypoints-count": {t: "Entrypoints found", d: "Every HTTP endpoint, task, command and signal receiver discovered in the repo, by kind."},

  // ---- touches / tags (diff_pr.ops_of, map_repo.tags) ----
  "touches": {t: "Touches", d: "Filter by what the code does. Picking several shows items that do ANY of them; none picked = no filter."},
  "op-read": {t: "📖 Read", d: "Reads from one or more database tables."},
  "op-write": {t: "✏ Write", d: "Writes to the database (create / update / delete / save). Writes are where data bugs and duplicate rows come from."},
  "op-external": {t: "🌐 External", d: "Calls an outside service over the network (requests, an SDK, an S3 client…). Data leaves your system here."},
  "op-async": {t: "⏱ Async", d: "Hands work to a background job (celery .delay / apply_async, a thread). That job's writes happen later, outside this request."},
  "op-cache": {t: "🗄 Cache", d: "Reads or writes a cache (cache.get / set / delete). A write can serve stale or wrong data if keys collide."},
  "tag-pii": {t: "pii", d: "Touches personal data (email, phone, name, address…). Check who can reach it and where it goes."},
  "tag-open": {t: "open", d: "Reachable without authentication: AllowAny, an empty permission list, or no auth we could resolve."},

  // ---- repo map ----
  "map-all": {t: "All", d: "Show every entrypoint (clears the chip filter)."},
  "map-enqueues": {t: "→ Enqueues task", d: "Endpoints that queue a background task. The task's writes are part of this endpoint's blast radius even though they run later."},
  "enqueues": {t: "Enqueues (async)", d: "The background tasks this endpoint queues, and the tables those tasks write."},
  "app": {t: "App", d: "A Django app / folder of the repo. Entrypoints are grouped by the app their code lives in; the dot is the app's worst severity."},
  "post-crit": {t: "Critical (posture)", d: "Entrypoints rated 🔴 across the whole repo. Click to show only them."},
  "post-high": {t: "High (posture)", d: "Entrypoints rated 🟠 across the whole repo. Click to show only them."},
  "post-unauth-write": {t: "Unauth-write", d: "Open (no auth) endpoints that write to the database — anyone can change data through them. Click to list them."},
  "post-pii-egress": {t: "PII-egress", d: "Endpoints that touch personal data AND call an external service — a path for personal data to leave. Click to list them."},
  "post-public": {t: "Public", d: "Endpoints reachable without authentication. Not always wrong (a login page is public), but each one should be intentional."},
  "post-external": {t: "External (posture)", d: "Endpoints that call an outside service."},
  "inv-seed": {t: "⚖ Invariant seed", d: "Downloads a starter list of rules this repo already follows (e.g. 'auth before any DB write'), with today's exceptions frozen. Confirm the real ones, then every PR is checked against them."},
  "branch-pick": {t: "Branch", d: "Analyze a branch instead of the files on disk. It's read from a temporary snapshot — your checkout is never touched. Empty = the working tree (current branch). Use the origin/… name for branches you don't have locally."},
  "search": {t: "Search", d: "Filters by route, handler, table, host or explanation text."},

  // ---- the 7-edge lens ----
  "lens": {t: "The 7-edge lens", d: "Every entrypoint is traced through 7 fixed questions — route, auth, DB, external calls, async, PII, cache — each answered with exact file:line evidence."},
  "e1": {t: "e1 — Route → handler", d: "Which function or class actually handles this URL / job."},
  "e2": {t: "e2 — Auth / permissions", d: "Who is allowed to call it: permission classes, decorators, or the DRF default."},
  "e3": {t: "e3 — DB tables", d: "Which tables it reads and writes, including inside the helpers it calls."},
  "e4": {t: "e4 — External calls", d: "Network calls to other services (HTTP clients, SDKs)."},
  "e5": {t: "e5 — Async tasks", d: "Background work it dispatches (celery tasks, threads)."},
  "e6": {t: "e6 — PII egress", d: "Personal data it reads and whether that data can leave (a response, an external call, a log)."},
  "e7": {t: "e7 — Cache", d: "Cache keys it reads or invalidates."},
  "st-verified": {t: "✓ Verified", d: "Found directly in the code, with a file:line you can click."},
  "st-potential": {t: "△ / ⚠ Potential", d: "Likely but indirect — e.g. found only inside a helper, or matched by a heuristic. Worth a quick check."},
  "st-unknown": {t: "? Unknown", d: "We couldn't resolve it statically (dynamic code, an unresolved constant). Shown so a gap is never mistaken for 'safe'."},
  "st-na": {t: "n/a", d: "Doesn't apply — e.g. auth for a background job that nothing outside can call."},

  // ---- auth (diff_pr.auth_str) ----
  "auth": {t: "Auth", d: "Who can call this entrypoint. 'open (AllowAny)' or 'open (no permission classes)' = anyone; 'unspecified' = no auth we could find (treated as open); DEFAULT_PERMISSION_CLASSES = the DRF settings default; '<X> unresolved' = set from a constant we couldn't read; 'n/a' = a job, not reachable over HTTP."},

  // ---- flow graph (diff_pr.build_graph) ----
  "graph": {t: "Flow graph", d: "Route → handler → everything it touches (tables, external calls, async jobs, cache). Click any block to see its source line."},
  "node-route": {t: "Route", d: "The URL (or task / command / signal name) where execution starts."},
  "node-handler": {t: "Handler", d: "The view / function / command that runs. Hover shows its auth."},
  "node-table": {t: "Table (read)", d: "A model / table this code reads."},
  "node-table-write": {t: "Table (write)", d: "A model / table this code writes — highlighted because writes carry the most risk."},
  "node-dbtable": {t: "SQL table", d: "The real database table behind a model. Green = declared in Meta.db_table; muted = guessed from Django's naming convention (app_model)."},
  "node-external": {t: "External call", d: "A call that leaves your system to another service."},
  "node-async": {t: "Async job", d: "A background task or thread this code dispatches."},
  "node-cache": {t: "Cache", d: "A cache read or write, with the key when we can see it."},
  "pii-dot": {t: "PII source", d: "The red dot marks a block where personal data is read."},
  "edge-label": {t: "Edge label", d: "How two blocks connect: HTTP methods (GET, POST…), read / write, '(via)' = happens inside a called helper, calls = network call, dispatches = async job, table / table (convention) = model → SQL table."},
  "diff-legend": {t: "Before → after", d: "+ green = added by this PR, − red dashed = removed, dimmed = unchanged."},
  "zoom": {t: "Zoom", d: "Make the flow graph bigger or smaller."},

  // ---- finding detail ----
  "changes-list": {t: "Semantic changes", d: "Every entrypoint whose behaviour this PR changes, worst-first. Click one to see its flow graph and where to look."},
  "why": {t: "Why", d: "The rule that produced this severity, in one line."},
  "flow": {t: "How it flows", d: "The chain from route to handler to everything it touches (tables, external calls, async jobs, cache), as text."},
  "where": {t: "Where to look", d: "Every fact behind the finding with its exact file:line — the places a reviewer should open."},
  "unknowns": {t: "Unknowns", d: "What we couldn't fully resolve (⚠ potential / ? unknown facts), and 'depth ⚠' when the call graph was too deep to follow completely — facts below it may be missing."},
  "instance": {t: "<instance> / <unresolved>", d: "A read or write whose table we couldn't name (e.g. `obj.save()` on an untyped variable). Kept visible on purpose: a hidden writer could hide a real problem."},
  "tabs-graph": {t: "Flow graph tab", d: "The picture: what this entrypoint touches, before vs after the PR."},
  "tabs-where": {t: "Where to look tab", d: "The list: every fact with its file:line."},

  // ---- review panels ----
  "contra": {t: "🎭 Intent vs. behavior", d: "The PR says one thing (e.g. 'refactor', 'no behavior change') but the facts show another (new writes, new external calls). Every flag comes from a verified fact, not a guess."},
  "deps": {t: "📦 Dependency capability changes", d: "A bumped package can now do something the old version couldn't: network, subprocess, native code, dynamic exec, filesystem writes, env access. ⚠ = a version we couldn't fetch or parse — not 'safe'."},
  "inv-alerts": {t: "🛡 Invariant alerts", d: "A rule you confirmed for this repo is affected by this PR."},
  "inv-new-violation": {t: "NEW VIOLATION", d: "Code this PR added or changed now breaks a confirmed rule, and it isn't one of the known baseline exceptions."},
  "inv-weakening": {t: "WEAKENING", d: "The definition of 'acceptable' got wider — a new egress destination or a newly-permitted exception. Shown more prominently than the code change itself, so rules can't quietly erode."},
  "blast": {t: "🧨 Blast radius", d: "Tables, services or jobs touched by 2+ of the changed endpoints. The bigger the count, the more of this PR breaks if that one thing goes wrong."},
  "blast-count": {t: "× N endpoints", d: "How many changed endpoints reach this resource (its fan-in)."},
  "egress": {t: "🌐 Shared egress", d: "One outside destination reached by several endpoints — verify it once instead of per route."},
  "egress-partial": {t: "⚠ Partial destination", d: "The host has a runtime placeholder (e.g. from settings or env), so only part of the URL is known."},
  "egress-unresolved": {t: "? Destination unresolved", d: "We saw a network call but couldn't work out where it goes."},

  // ---- invariants ----
  "invariants": {t: "⚖ Invariants", d: "Rules this codebase already follows (e.g. 'authenticated access before any DB write'), discovered from git history. Confirm the intentional ones and every review enforces them."},
  "inv-kind-universal": {t: "universal", d: "The rule holds for every subject (and there are at least 6) — a strong candidate to confirm."},
  "inv-kind-near-miss": {t: "near-miss", d: "The rule holds for 70–99% of subjects with only a few exceptions — either a real rule with bugs, or exceptions to accept."},
  "inv-holds": {t: "Holds x / y", d: "How many of the subjects the rule applies to satisfy it right now."},
  "inv-exceptions": {t: "Exceptions (baseline)", d: "Today's violators. Confirming freezes them as known exceptions (a ratchet): only a NEW break is flagged later."},
  "inv-confirm": {t: "Confirm", d: "Accept this rule as intentional. From now on every review checks it."},
  "inv-confirmed": {t: "✓ Confirmed", d: "Already accepted — enforced on every review."},
  "inv-readonly": {t: "Read-only", d: "This server can't save confirmations. Start `lenscheck serve --invariants <path>` to enable Confirm."},
  "inv-blame": {t: "🩸 Blame", d: "Walks git history to find the commit that first broke this rule."},

  // ---- review sidebar ----
  "repo-path": {t: "Repo path", d: "A local git checkout (or a git URL) to analyze."},
  "run-mode": {t: "Run mode", d: "What to compare: a merged PR, two branches, or two commits."},
  "mode-pr": {t: "PR", d: "Review a merged pull request (its merge commit vs its parent)."},
  "mode-branch": {t: "Branch", d: "Compare a branch against a base branch, like an open PR."},
  "mode-commit": {t: "Commit", d: "Compare two specific commits."},
  "ref-base": {t: "Base", d: "The 'before' side of the comparison."},
  "ref-compare": {t: "Compare", d: "The 'after' side — the code being reviewed."},
  "run-review": {t: "Run review", d: "Analyze both sides and list every semantic change, worst-first."},
  "branch-vs-default": {t: "Branch vs default", d: "Review the current branch against the repo's default branch."},
  "filters": {t: "Filters", d: "Narrow the list by severity, change kind, entrypoint kind and what the code touches. Filters are remembered (and kept in the URL)."},
  "lines-changed": {t: "Lines changed", d: "Size of the raw diff — compare it with how few things actually matter."},
  "things-matter": {t: "Things that matter", d: "Semantic changes found — the part of the diff that changes behaviour."},
  "raw-diff": {t: "Raw diff", d: "The plain git diff this review was computed from."},

  // ---- cross-repo ----
  "x-multi-writer": {t: "🔴 Multi-writer", d: "Two or more repos WRITE this table. Their writes can collide (duplicate rows, lost updates), so schema and write logic must be coordinated across teams."},
  "x-producer-consumer": {t: "🟠 Producer → consumer", d: "One repo writes, the others only read. The writer owns the shape: a schema change there can break the readers."},
  "x-read-only": {t: "Read-only shared", d: "Several repos read it, none write it. Usually safe."},
  "x-writers": {t: "Writers", d: "The repos that write this table."},
  "x-writes": {t: "WRITES", d: "Entrypoints in this repo that write the table (route · handler · file:line)."},
  "x-reads": {t: "READS", d: "Entrypoints in this repo that read the table."},
  "x-not-touched": {t: "Not touched", d: "This repo doesn't read or write this table."},
  "x-unique-key": {t: "Unique key", d: "A DB unique constraint on this table (from your schema.json). Writers must agree on it or inserts fail / duplicate."},
  "x-coordinate": {t: "⚠ Coordinate on unique key", d: "A multi-writer table with a unique key: both repos insert, so a race or different dedupe logic means duplicate-key errors or duplicate data."},
  "x-schema": {t: "schema.json", d: "Optional dump of the DB's unique keys (lenscheck schema-dump). Turns 'multi-writer' into column-level coordination rules."},
  "x-writes-only": {t: "Writes-only", d: "Hide read-only shared tables to focus on the ones someone writes."},
  "x-api-calls": {t: "Cross-repo API calls", d: "Code in one repo calling an endpoint defined in another, matched by URL path."},
  "x-exact": {t: "exact", d: "The call path equals the endpoint's full route (include() prefixes folded in) — trustworthy."},
  "x-tail": {t: "tail · verify", d: "Matched only on the route's last part, or with an extra prefix (e.g. a proxy mount). Usually right, but check it."},
  "x-written-by": {t: "Written by", d: "Only tables this repo writes."},
  "x-has-uk": {t: "⚠ Has unique key", d: "Only tables with a unique key from schema.json — the ones where concurrent writers can collide."},
  "x-repos": {t: "Repos", d: "One repo per line. Add @branch to analyze a branch (checked out in a throwaway worktree; your checkout is untouched)."},
  "x-expand": {t: "Expand / collapse", d: "Open or close every shown table card."},

  // ---- navigation ----
  "nav-review": {t: "☑ Review", d: "Review a PR, branch or commit: every semantic change, ranked."},
  "nav-map": {t: "🗺 Repo Map", d: "Every entrypoint in one repo, grouped by app, worst-first."},
  "nav-crossrepo": {t: "🔀 Cross-repo", d: "Tables several repos read or write — finds cross-service multi-writers."},
  "nav-theme": {t: "Theme", d: "Switch between light and dark."},
  "nav-glossary": {t: "📖 Glossary", d: "Every term this tool uses, A–Z, with search."}
};

// ---- styles (the pages' own CSS vars, so light + dark both work) ----
const css = `
.lc-tip{position:fixed;z-index:9999;display:none;max-width:340px;padding:9px 11px;border-radius:9px;
  background:var(--card,#161b22);color:var(--fg,#e6edf3);border:1px solid var(--acc,#7c74f0);
  box-shadow:0 8px 24px rgba(0,0,0,.35);font:12.5px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;pointer-events:none}
.lc-tip b{display:block;margin-bottom:3px;font-size:13px}
.lc-tip .lc-tip-x{margin-top:6px;padding-top:6px;border-top:1px solid var(--line,#30363d);color:var(--mut,#8b949e);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;word-break:break-word}
[data-g]{cursor:help}
a[data-g],button[data-g],summary[data-g],[onclick][data-g],label[data-g],select[data-g],input[data-g]{cursor:pointer}
input[type=search][data-g],input[type=text][data-g],input:not([type])[data-g],textarea[data-g]{cursor:text}
.lc-gl{position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.45);display:flex;align-items:flex-start;justify-content:center;padding:40px 16px}
.lc-gl-box{width:min(760px,100%);max-height:calc(100vh - 80px);display:flex;flex-direction:column;background:var(--panel,#0d1117);
  color:var(--fg,#e6edf3);border:1px solid var(--line,#30363d);border-radius:12px;box-shadow:0 16px 48px rgba(0,0,0,.45)}
.lc-gl-head{display:flex;gap:10px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line,#30363d)}
.lc-gl-head h3{margin:0;font-size:15px;flex:1}
.lc-gl-head input{flex:1.4;min-width:0;padding:6px 10px;border-radius:8px;border:1px solid var(--line,#30363d);background:var(--card,#161b22);color:var(--fg,#e6edf3)}
.lc-gl-head button{border:1px solid var(--line,#30363d);background:transparent;color:var(--fg,#e6edf3);border-radius:8px;padding:5px 10px;cursor:pointer}
.lc-gl-list{overflow:auto;padding:6px 16px 16px}
.lc-gl-letter{margin:14px 0 4px;color:var(--acc,#7c74f0);font-weight:800;font-size:13px}
.lc-gl-item{padding:6px 0;border-bottom:1px solid var(--line,#30363d)}
.lc-gl-item b{font-size:13px}
.lc-gl-item div{color:var(--mut,#8b949e);font-size:12.5px;line-height:1.45;margin-top:2px}
.lc-gl-none{color:var(--mut,#8b949e);padding:12px 0}
@media (max-width:600px){.lc-gl{padding:12px}.lc-gl-head{flex-wrap:wrap}}`;
const st = document.createElement("style");
st.textContent = css;
document.head.appendChild(st);

// ---- the tooltip: one element, delegated listeners → works for content rendered later (lists, SVG) ----
let tip = null, timer = null, cur = null;
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function show(node) {
  const e = G[node.getAttribute("data-g")];
  if (!e) return;
  if (!tip) { tip = el("div", "lc-tip"); tip.id = "lc-tip"; tip.setAttribute("role", "tooltip"); document.body.appendChild(tip); }
  tip.replaceChildren(el("b", "", e.t), el("div", "", e.d));
  const x = node.getAttribute("data-gx");
  if (x) tip.appendChild(el("div", "lc-tip-x", x));
  tip.style.display = "block";
  const r = node.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 8);      // no room below → above
  tip.style.left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8) + "px";
  tip.style.top = top + "px";
  node.setAttribute("aria-describedby", "lc-tip");
}
function hide() {
  clearTimeout(timer);
  if (tip) tip.style.display = "none";
  if (cur) cur.removeAttribute("aria-describedby");
  cur = null;
}
function enter(ev) {
  const node = ev.target && ev.target.closest ? ev.target.closest("[data-g]") : null;
  if (node === cur) return;
  hide();
  if (!node) return;
  cur = node;
  timer = setTimeout(() => show(node), 120);
}
document.addEventListener("mouseover", enter);
document.addEventListener("focusin", enter);
document.addEventListener("mouseout", ev => { if (cur && !cur.contains(ev.relatedTarget)) hide(); });
document.addEventListener("focusout", hide);
document.addEventListener("scroll", hide, true);
document.addEventListener("keydown", ev => { if (ev.key === "Escape") { hide(); closeList(); } });

// ---- 📖 the A–Z list ----
const sortKey = t => t.replace(/^[^A-Za-z0-9<]+/, "").replace(/^</, "").toLowerCase();
function closeList() { const m = document.getElementById("lc-gl"); if (m) m.remove(); }
function fill(list, q) {
  q = (q || "").trim().toLowerCase();
  const rows = Object.values(G).filter(e => !q || (e.t + " " + e.d).toLowerCase().includes(q))
    .sort((a, b) => sortKey(a.t).localeCompare(sortKey(b.t)));
  list.replaceChildren();
  let letter = "";
  rows.forEach(e => {
    const L = (sortKey(e.t)[0] || "#").toUpperCase();
    if (L !== letter) { letter = L; list.appendChild(el("div", "lc-gl-letter", L)); }
    const it = el("div", "lc-gl-item");
    it.append(el("b", "", e.t), el("div", "", e.d));
    list.appendChild(it);
  });
  if (!rows.length) list.appendChild(el("div", "lc-gl-none", "No term matches."));
}
window.lcGlossaryOpen = function () {
  if (document.getElementById("lc-gl")) return closeList();
  hide();
  const m = el("div", "lc-gl"); m.id = "lc-gl";
  const box = el("div", "lc-gl-box"), head = el("div", "lc-gl-head"), list = el("div", "lc-gl-list");
  const q = el("input"); q.type = "search"; q.placeholder = "🔎 search terms…";
  const x = el("button", "", "✕"); x.title = "Close (Esc)"; x.onclick = closeList;
  head.append(el("h3", "", "📖 Glossary — " + Object.keys(G).length + " terms, A–Z"), q, x);
  box.append(head, list); m.appendChild(box);
  m.onclick = ev => { if (ev.target === m) closeList(); };
  q.oninput = () => fill(list, q.value);
  fill(list, "");
  document.body.appendChild(m);
  q.focus();
};
})();
