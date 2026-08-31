# Guest suggestions

`hospes suggest-guests` turns the tracked public Unlicensed Therapy episode
archive into a deterministic editorial-review queue. It does not discover
contact data, send correspondence, or assert that a route is verified.

```bash
hospes suggest-guests \
  --min-gap-years 2 \
  --max-social-cost 3 \
  --relationship-class C2 \
  --limit 20
```

CSV is the only stdout output, so the result can be reviewed directly or piped
into the scoped importer:

```bash
hospes suggest-guests --min-gap-years 2 --max-social-cost 3 \
  | hospes import-candidates - \
      --tenant private_pilot \
      --network example_network \
      --show flagship_private_pilot \
      --actor example_operator \
      --role producer
```

The first eight columns are the public review contract:
`source_key`, `name`, `relationship_class`, `last_appearance`,
`episodes_since`, `estimated_social_cost`, `suggested_route`, and `notes`.
The remaining columns form the versioned `hospes-suggestions-v1` import
envelope. The importer discards the display-only `notes` field, preserves
archive provenance, and creates a tenant/show-scoped editorial opportunity.

Every inferred route enters the store with `route_usable=false`. The dashboard
shows the suggestion and public provenance as unverified. A human operator must
verify the actual private route through the ordinary authenticated workflow
before any invitation can become eligible; suggestion generation itself never
authorizes outreach.

## Public tour context

Repeat `--tour PATH` to add public timing context. Each source is either JSON
(a list of objects) or CSV with these allowlisted fields:

- `guest` or `name` (required)
- `date` as `YYYY-MM-DD` (required)
- `city` (required)
- `source` and/or `source_url` (at least one is required)
- optional `title` and `venue` fields, which are validated but not persisted

`source_url` must be public HTTPS without credentials, query parameters, or a
fragment. Contact-, address-, message-, token-, secret-, or private-named
fields are rejected as a whole batch. Past events are ignored; the earliest
future event for a matching archive guest supplies timing context and public
provenance. No venue address or contact value is emitted.

For reproducible review, `--as-of YYYY-MM-DD` fixes the archive cutoff and must
not be later than the current UTC date. Social cost is bounded from 1–5: C2
alumni are cost 3 during the first four completed gap years and cost 2
afterward, so more recent appearances are intentionally ranked as higher cost.
Route labels are `Host direct` for C1, `Producer warm intro` for C2, and
`Producer cold` for C3.

Errors and validation details go only to stderr and contain no rejected value.
The canonical acceptance command is:

```bash
python -m pytest tests/issue_predicates/test_issue_19.py -q
```
