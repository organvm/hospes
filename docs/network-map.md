# Guest Network Map v1

**Source:** Public RSS parse of *Unlicensed Therapy* with Host Mannis  
**Parsed:** 2026-07-13  
**Dataset location:** `data/unlicensed-therapy/archive.json` + `guests.json`
**Public-footprint rule:** This dataset contains only public-figure facts derived from a
publicly distributed RSS feed. No private contact information, no location or health details,
no characterizations of any individual. All data is observation of the public record only.

## Executable contract

`hospes network-map` composes three sources for exactly one tenant and show:

1. relationship edges already stored in the scoped database;
2. manual edges, aliases, targets, and archive-source declarations in
   `config/network_edges.yaml`;
3. the configured public episode archive, or a path explicitly supplied by an
   operator with `--archive`.

The raw archive is checkout-level source material and is not copied into the
installed wheel. The wheel carries only a minimal synthetic archive fixture and
continues to support database/manual graphs. This avoids distributing a second
copy of the source export while keeping archive ingestion independently
testable.

Names resolve to scoped aliases, so both of these are equivalent in the shipped
flagship configuration:

```bash
hospes network-map --guest "Theo Von" --depth 2 --format mermaid
hospes network-map --guest theo-von --depth 2 --format csv
```

Supported formats are `mermaid`, `graphviz`, `json`, and `csv`. Every format is
rendered from the same `spec/network-map.schema.json` projection. Mermaid and
Graphviz receive generated node ids rather than interpolated guest ids. CSV has
a fixed column set, uses standards-compliant quoting, neutralizes formula-leading
labels, and marks a row when either declared endpoint is a target. JSON drops undeclared
source fields such as `guest_raw` and `description_head`.

The archive aggregation maps repeat guests to C3 recurring colleagues, single
high-confidence appearances to C2 prior professional interactions, and other
validated single appearances to C1 public adjacency. Future-dated appearances and
public labels containing contact-like private data are rejected. Traversal follows
relationship edges in either direction while exports preserve the declared semantic direction, so a
map rooted at Theo includes the incoming Host-to-Theo relationship and the
second-degree alumni beyond Host. Reachable C2/C3 alumni are target candidates;
their deterministic shortest paths are highlighted in visual exports and
listed in the dashboard Relationship paths panel. This is read-only evidence:
loading or exporting a map cannot send, book, consent, publish, or mutate an
opportunity.

New database writes and version 2 configuration accept only the five canonical
edge types. A legacy database row or version 1 configuration value outside that
taxonomy remains visible as `legacy_unspecified`; HOSPES does not invent a more
specific relationship meaning or make the rest of the scoped map unavailable.

### Private aliases

A private relationship may appear in manual configuration only under an opaque
`private-*` id with an opaque `label_ref`. It may not contain a display name.
Authorized operator projections expose only the generic label `Private relationship`;
the custody reference is never returned by the API or any
export. Alias, edge, target, and archive-source records all carry tenant/show
scope, and records from another scope are validated but excluded.

---

## Dataset Statistics

| Metric | Value |
|--------|-------|
| Total appearance records | 185 |
| Episode records parsed | 185 |
| Unique guest names in `archive.json` | 169 |
| Returning guests (2+ appearances) | 16 |
| Date range | 2014-10-16 → present |
| Source | *Unlicensed Therapy* public RSS |

---

## Top Returning Guests

All 16 returning guests appeared exactly twice. Listed alphabetically:

Abby Roberge, Brian Moses, Bruce Gray, Chelcie Lynn, Craig Conant, Earl Skakel,
Francisco Ramos, JT Parr, Lucas Hirl, Matt Lockwood, Mike Falzone, Mitch Burrow,
Mo Mandel, Sam Tripoli, Sara Weinshenk, Steven Randolph.

No guest appears three or more times in the public archive. The 16 returners represent
9.5% of the unique guest population and constitute the highest-confidence Ring 1 candidates:
Host has already invested enough social capital to bring them back once; a second re-engagement
on a new show is a materially lower-cost ask than a first-time booking.

---

## How This Dataset Seeds Ring 1

Ring 1 is the prior-professional-relationship tier — guests where at least one host has an
established working relationship and the outreach is a re-engagement, not a cold ask.

This archive provides the **public-footprint confirmation layer** for Ring 1. A name appearing
here means:

1. The person has appeared on a related show hosted by Host.
2. The appearance is publicly documented (RSS-verified date, title, episode type).
3. The relationship owner is Host for all 169 guest names in this archive.
4. Returners have confirmed willingness to appear a second time — the prior ask succeeded.

The archive does **not** provide contact routes, representation details, chemistry notes, or
topic tags. Those are producer-enrichment tasks (see Next Steps below).

Pilot A (brief `pilot-01.md`) draws directly from this Ring 1 pool: a comedian who appeared
on *Unlicensed Therapy*, re-engaged via producer email, social cost 2.

---

## Next Enrichment Steps (Producer Tasks)

These steps expand the archive from a name-and-date list into a bookable pipeline. All are
optional at launch; the archive is usable as-is for Ring 1 identification.

1. **Representation routes** — For each guest where booking is being considered, research
   current publicist or manager. Add to `pipeline.csv` `contact_route` column. Never store
   private contact details in the repo; use `TODO-by-producer` as placeholder until confirmed
   out-of-band.

2. **Topic tags** — Tag each archive entry with 2–3 topics from the episode title and
   description. Enables clustering: "which guests have comedy + music overlap?" This enrichment
   is a producer read of `archive.json`; no external API needed.

3. **Chemistry notes** — For returning guests and high-priority Ring 1 candidates, a producer
   note on the prior episode's dynamic: did the conversation run long, was the guest reluctant
   to go deep, did Host redirect the topic? These notes inform the Claim design for each
   episode brief.

4. **Melrose ecosystem cross-reference** — Guests who have worked at or near the Melrose
   studio may have relationships with Host that postdate or predate their podcast appearance.
   That enrichment requires producer knowledge and cannot be derived from the RSS alone.

5. **Producer's network overlay** — This archive covers Host's prior show only. Producer's direct
   professional network (Ring 1 from Producer's side) is a separate data source. Pilot B
   (`pilot-02.md`) tests the first entry from that overlay.

---

## Data Provenance and Integrity

- Source: public RSS feed, machine-parsed, no scraping of private pages.
- Confidence levels per episode: `high` (name explicit in title or guest field), `medium`
  (name inferred from description), `low` (ambiguous). Archive JSON includes `confidence`
  field per record.
- Episode count discrepancy (179 episodes mentioned in design session vs. 185 appearance
  records): the archive contains 185 appearance records; some episodes may have multiple
  guests or the count differs by which records are classified as "guest" type vs. solo
  episodes. The 185 figure is the ground truth from the parsed feed.
- `archive.json` contains 169 unique guest names while the derived `guests.json` contains 168;
  Bob Fisher is absent from the derived list. Runtime ingestion uses the source archive and
  therefore preserves the 169-name denominator instead of silently dropping that record.
- This dataset will not be automatically refreshed. A producer update cycle (monthly or
  per new episode) is a manual step until an automated RSS ingestion organ is built.
