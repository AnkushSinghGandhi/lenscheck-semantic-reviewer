# shop-demo — the purpose-built hero demo

A tiny Django REST app whose PR #128 ("add customer analytics + export") sneaks in **three red flags**:
auth weakened (`IsAuthenticated → AllowAny`), a new public endpoint exposing PII, and a new external
call to an analytics vendor with PII (email) egress. It reviews to the ideal money-shot.

Reproduce / re-bake:

```bash
git clone demo/src/shop-demo.bundle /tmp/shop && cd /tmp/shop
BASE=$(git rev-list --max-parents=0 HEAD) HEAD=$(git rev-parse HEAD)
lenscheck review /tmp/shop --base "$BASE" --head "$HEAD" --pr 128 --json shop-scary-128.json
# then set summary=title and tidy base/head labels (see how demo/shop-scary-128.json was made)
```
