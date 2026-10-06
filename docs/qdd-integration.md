# Consuming `lenscheck provenance` (subprocess contract)

`lenscheck provenance` answers the one question a database tool can't: **which code wrote this data?**
It reads an application's source (static, read-only, never executes it) and prints JSON describing, per
table, the endpoints that read/write it — plus candidate data-consistency rules derived from the code's
foreign keys. Designed to be shelled out to (e.g. from QA Data Desk); it bundles only the free extractor.

## Call it

```bash
lenscheck provenance /path/to/app-repo              # indented JSON on stdout
lenscheck provenance /path/to/app-repo --compact    # single-line JSON
lenscheck provenance /path/to/app-repo --yaml rules.yaml   # also write a QDD consistency_rules.yaml
```

- **stdout** is *pure JSON* — nothing else is ever written there, so `json.loads(stdout)` is always safe.
- **stderr** carries a one-line human summary and any error — ignore it for parsing.
- **exit codes:** `0` success · `2` bad usage · `3` repo unreadable / analysis failed.

No API key, no network, no database connection. Licensed ELv2 (free to use/self-host; call it as an
external command — your code's license is unaffected).

## Output shape (`schema: 1`)

```jsonc
{
  "schema": 1,                       // pin on this; refuse an unknown value
  "repo": "/abs/path",
  "summary": {"endpoints": 35, "tables": 54, "fk_edges": 71, "orphan_rules": 70, "anomalies": 0},
  "tables": {
    "payments": {
      "models": ["Payment"],
      "written_by": [{"route": "/checkout", "handler": "Checkout", "methods": ["POST"], "loc": "x.py:12"}],
      "read_by":    [{"route": "/dashboard", "handler": "Dashboard", "methods": ["GET"], "loc": "y.py:40"}]
    }
  },
  "consistency_rules": [
    {
      "id": "fk-orphan::payments.lead_id->leads",
      "kind": "referential_integrity",     // or "co_occurrence_anomaly"
      "confidence": "high",
      "statement": "every `payments.lead_id` must reference an existing `leads.id`",
      "source_fact": "Payment.lead_id ForeignKey -> Lead",
      "qdd": {                              // drops straight into QDD's consistency rules
        "name": "payments references a valid leads",
        "source": {"table": "payments", "key": "lead_id"},
        "target": {"table": "leads", "key": "id"},
        "expect": "exists"
      }
    }
  ]
}
```

## Suggested QDD usage

1. **Investigate record / Find data** — for the table on screen, show `tables[<table>].written_by` /
   `read_by`: *"this table is written by Checkout, Refund, Webhook"* → click through to the code.
2. **Data consistency** — offer "Import rules from code": take every `kind == "referential_integrity"`
   entry's `qdd` block into `local/consistency_rules.yaml`, then run them as usual.

`co_occurrence_anomaly` entries are lower-confidence leads ("writes A but never touches its FK target B")
— surface as hints, not assertions.
