#!/usr/bin/env bash
# Bake a review of a real PR into a demo JSON. Needs LENSCHECK_API_KEY + `gh`.
#
# `--pr N` is only a *label* — a review compares --base..--head, so we resolve the PR's real base/head
# SHAs and clone with history. And only PRs that change the SEMANTIC SURFACE the extractor can see
# (routes, DRF permission_classes / Flask decorators / FastAPI Depends, external calls, models) produce
# findings — a big refactor, or auth via a framework the extractor doesn't recognize, reviews as empty.
#
# The shipped hero demo (demo/shop-scary-128.json) is purpose-built and always lights up; rebuild it
# from demo/src/shop-demo.bundle (see demo/src/README.md).
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
  python3 -c "import json;print('  findings:',len(json.load(open('$id.json'))['changes']))"
}

# Add famous PRs here once you find ones that produce findings, then list them in manifest.json.
# (Tip: pick PRs that add a route / change permission_classes / add a requests/stripe/httpx call.)
# bake owner/repo <pr> <id>
