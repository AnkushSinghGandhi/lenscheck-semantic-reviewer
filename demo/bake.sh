#!/usr/bin/env bash
# Pre-bake the demo reviews once, so the hosted UI can serve them instantly (no cloning, no key).
# Needs LENSCHECK_API_KEY set (same as the CLI) and `gh` authed to resolve PR refs.
#
# IMPORTANT: `--pr N` is only a *label* — it does NOT set the diff. A review compares --base..--head,
# so we resolve the PR's real base/head SHAs and clone with history. And pick PRs that actually change
# the *semantic surface* (a new route, an auth change, a new external call, a model/field change) —
# a big line-count refactor with no surface change reviews as "no changes".
set -euo pipefail
cd "$(dirname "$0")"

bake() {                      # bake <owner/repo> <pr-number> <demo-id>
  local repo="$1" pr="$2" id="$3" dir base head
  dir="$(mktemp -d)"
  echo "→ $repo #$pr  ($id)"
  base="$(gh api "repos/$repo/pulls/$pr" --jq .base.sha)"
  head="$(gh api "repos/$repo/pulls/$pr" --jq .head.sha)"
  git clone --quiet --filter=blob:none "https://github.com/$repo" "$dir"
  git -C "$dir" fetch --quiet origin "$head" || true
  lenscheck review "$dir" --base "$base" --head "$head" --pr "$pr" --json "$id.json"
  rm -rf "$dir"
}

# Edit these to your chosen PRs (see manifest.json). Keep only reviews that surface real findings.
bake Netflix/dispatch          6205  dispatch-6205
bake django-oscar/django-oscar 4570  oscar-4570
bake netbox-community/netbox   23207 netbox-23207

echo
echo "baked $(ls -1 *-*.json 2>/dev/null | wc -l) reviews. serve them with:"
echo "    lenscheck serve --demo-dir demo               # pre-baked only (no key needed)"
echo "    lenscheck serve --demo-dir demo --public      # + let visitors paste a public repo"
