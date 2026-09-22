#!/usr/bin/env bash
# Pre-bake the demo reviews once, so the hosted UI can serve them instantly (no cloning, no key).
# Needs LENSCHECK_API_KEY set (same as the CLI). Re-run whenever you want to refresh the examples.
#
# The three PRs are curated to light up the lens: a new external call (Slack), dashboard auth/endpoint
# churn, and DRF api + model changes — across FastAPI, Django, and Django/DRF.
set -euo pipefail
cd "$(dirname "$0")"

lenscheck review https://github.com/Netflix/dispatch                 --pr 6205  --json dispatch-6205.json
lenscheck review https://github.com/django-oscar/django-oscar        --pr 4570  --json oscar-4570.json
lenscheck review https://github.com/netbox-community/netbox          --pr 23207 --json netbox-23207.json

echo
echo "baked $(ls -1 *-*.json | wc -l) reviews. serve them with:"
echo "    lenscheck serve --demo-dir demo               # pre-baked only (no key needed)"
echo "    lenscheck serve --demo-dir demo --public      # + let visitors paste a public repo"
