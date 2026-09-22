# Hosted demo — pre-baked reviews

This folder powers the public "try it in your browser" demo (Phase 1 of the plan). Instead of cloning
and reviewing a repo on every visit, the server serves **pre-baked review JSON** from here — instant,
free, and impossible to abuse.

## Bake the reviews (one time)

```bash
export LENSCHECK_API_KEY=sk_live_...    # same key as the CLI
bash demo/bake.sh                       # writes dispatch-6205.json, oscar-4570.json, netbox-23207.json
```

Commit the resulting `*-*.json` files — they *are* the demo data.

## Serve it

```bash
lenscheck serve --demo-dir demo             # pre-baked examples only — no key, no cloning
lenscheck serve --demo-dir demo --public    # also let visitors paste a public repo URL
```

- **`--demo-dir`** exposes `GET /api/demos` (the list) and `GET /api/review?demo=<id>` (a baked review).
  A pure `--demo-dir` deploy needs **no API key** and never clones anything.
- **`--public`** additionally lets a visitor review a pasted **public** repo. Each repo is shallow-cloned
  into a small, delete-after-use cache (bounded to a few repos, reaped by TTL), so disk stays tiny.
  Restrict hosts with `--allow github.com/your-org` or `LENSCHECK_ALLOWED_REPOS=` (comma-separated).

## The manifest

`manifest.json` lists the examples: `id`, `title`, `repo`, `pr`, and the `file` to serve. Add or swap
examples by editing it and re-baking. The three defaults are curated to show the lens at its best —
a new external Slack call (FastAPI), dashboard auth/endpoint churn (Django), and DRF api + model
changes (Django/DRF).
