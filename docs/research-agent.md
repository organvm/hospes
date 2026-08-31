# Bounded research

Producers spend hours reading about a guest before a recording. HOSPES will do
the reading, and it will do it on a leash: one cost-capped query, one named
source, citations only from an allowlist the operator wrote in advance, and a
brief that is worth nothing downstream until a human has verified a claim,
written a counterargument, approved the review, and locked it.

Nothing about that sequence is decorative. A research assistant is the single
place in this system where invention is cheapest and most expensive — cheapest
to produce, most expensive to publish — so every step below exists to make a
fabricated or unattributable claim structurally unable to reach an episode.

## Sources

Four retrieval sources and one manual mode are registered in
`config/runtime.yaml` under the `research` capability:

| Source | Kind | Default cost (minor units) |
|--------|------|-----------------------------|
| `brave` | Brave Web Search API | 5 |
| `serpapi` | SerpAPI Google results | 25 |
| `searxng` | self-hosted SearXNG instance | 1 |
| `allowlisted_crawler` | direct fetch of named allowlisted documents | 2 per document |
| `manual_citations` | operator supplies the citations; HOSPES retrieves nothing | 0 |

Each is a transport-injected adapter in `hospes/integration_adapters.py`. An
adapter shapes a request and parses a response; it never opens a socket. The
default transport is `UnconfiguredResearchTransport`, which refuses to execute
and produces a `blocked` provider receipt, so an estate with no credential wall
records a visible blocked state instead of an unbounded live fetch.
`FixtureResearchTransport` replays a recorded synthetic corpus from disk, which
is how the tests and the demo exercise every provider with **no live network**.

## Source policy

`config/research.yaml` owns `allowed_sources`. A URL becomes a citation only if
it survives `normalize_source_url` **and** matches an allowlisted prefix:

- `https` only — a plaintext mirror of an allowed article is still refused;
- no embedded credentials (`https://user:pw@host/`);
- no explicit port;
- no loopback name and no IP-literal host, so the crawler cannot be aimed at a
  private network;
- host and path-prefix match against the allowlist, so `en.wikipedia.org.evil.test`
  is not `en.wikipedia.org`.

A returned result that fails this test is **not silently dropped**. It is
recorded in `risk_flags` as `source_not_allowlisted` with its `scheme://host`
label, because a vanished result is indistinguishable from a result the
provider never returned. The crawler applies the same check to each seed
*before* fetching, and refuses a response whose landing URL differs from the
document that was requested.

## Cost policy

`max_cost_minor` in `config/research.yaml` is the ceiling; a job may request a
lower one but never a higher one. The cost is **priced before the call**:
`cost_for(query)` is evaluated against the job's ceiling before the transport
is touched, so a job that cannot afford its provider never reaches it. The
crawler prices per document, so five seeds cost five documents.

The durable job carries the same ceiling as its `cost_limit_minor` in the
leased-job substrate (`docs/authentication-and-jobs.md`), and research jobs run
with `max_attempts: 1` — a failed provider call is not retried into a second
charge.

## Lifecycle

```
queued → researching → ready_for_review → reviewed → locked
                    ↘ failed            ↘ rejected
```

`POST /v1/shows/{show_id}/research` opens a job. For a retrieval provider it is
`queued` and enqueued as a `research.provider_search` background job;
`manual_citations` opens directly in `researching`, awaiting operator citations.

`POST /v1/shows/{show_id}/research/{job_id}/run` drains that job through the
leased-job substrate: lease, execute, attempt receipt, cost accounting. The
internal trigger `POST /internal/jobs/run` drains the same handler.

A completed retrieval writes:

- `citations` — title, URL, provider, snippet, publication date, retrieval time;
- `evidence` — one receipt per citation naming the source and retrieval time;
- `brief.candidate_claims` — each claim bound to its citation index and marked
  `verified: false`, because retrieval verifies nothing;
- `segment_candidates` — a starting slate derived from the top citations;
- `risk_flags` — every refused source.

`POST …/annotate` is the human editorial pass: verify claims by index, add
counterarguments, add segments and risks. `POST …/review` records the decision;
approval requires at least one citation, at least one counterargument, and, for
a retrieval job, at least one operator-verified claim. `POST …/lock` freezes the
brief for downstream use and refuses any job that has not been reviewed (`403`).

## Immutability

Every write recomputes a SHA-256 **content checksum** over the brief,
citations, counterarguments, evidence, segment candidates, risk flags, cost, and
provider. Review stamps that checksum onto the job and into the review receipt.
Any later annotate, review, or lock recomputes it and refuses to proceed when it
no longer matches, so "reviewed" keeps meaning the content a human actually
read — even against a write that bypassed the service layer.

## Receipts

Every provider interaction writes a `provider_receipts` row under capability
`research`: the source name, its endpoint, the cost charged, how many results
were returned, how many were cited, and how many were refused. A blocked or
failed call writes a `blocked` receipt carrying the reason. The human decision
writes a second receipt under capability `research_review` carrying the
reviewer, the reviewer's role, the approval boolean, and the frozen checksum.

## Authorization

Only **producer or editorial_owner** sessions may start, run, annotate, review,
or lock research; every other role receives `403`. The dashboard mirrors this
through the `canResearch` capability, so a host or relationship-owner session
never renders the Research button. Show scope is enforced before any lookup, so
a session scoped to one show cannot read or mutate another show's research.

## Operator workflow

In Pilot Workbench, an approval card carries a **Research** button. Clicking it
opens a job for that candidate, runs it, and renders the brief in the Bounded
research console: verified claims, citations, counterarguments, evidence
receipts, segment candidates, and risk flags, each section counted. The console
exposes claim checkboxes, a counterargument field, and three separate
buttons — record review pass, approve review, lock research — because approval
and lock are two distinct human acts and HOSPES commits neither on its own.

## Configuration

```yaml
# config/research.yaml
max_cost_minor: 500          # ceiling for every job in the estate
max_results: 5               # per-query result ceiling
requires_counterargument: true
allowed_sources:             # https prefixes; everything else is a risk flag
  - https://en.wikipedia.org/
providers: [manual_citations, brave, serpapi, searxng, allowlisted_crawler]
provider_costs_minor: {brave: 5, serpapi: 25, searxng: 1, allowlisted_crawler: 2}
```

API keys never appear here. `config/runtime.yaml` names each live source's
`credential_ref`; an unprovisioned credential leaves the source visibly
`blocked` rather than silently substituting another.
