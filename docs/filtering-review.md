# Filtering, matching and normalisation review — 3 October 2026

The review traced BOM, NSW RFS and Live Traffic NSW through fetch, normalisation, council/product selection, revision detection, storage, web presentation, multipart formatting and queue admission. The requirements used were the documented repository behaviour and its settings: preserve the selected notice's hazard, location, relevant summary/advice, action and source attribution; distinguish collection from confirmed local transmission; retain excluded records for inspection.

This document records the initial review. All six follow-up findings below have now been addressed; see [implementation and validation](filtering-implementation.md) for current behaviour. The historical findings are retained to explain the changes.

## Evidence and validation

- Baseline: 172 existing tests passed before changes.
- Final verification: 214 tests passed, including 42 additional regression cases. One existing Starlette/httpx deprecation warning remains.
- Tests ran against copied source in `/tmp/wxecho-review` in the test container, using temporary databases and radio doubles. No real radio sends were made.
- Official feed samples captured during this review contained 36 RFS incidents, 127 state Traffic incidents, 384 regional Traffic items and a BOM marine product with two hazard sections. These are point-in-time samples, not exhaustive coverage of every provider product.
- After council-name normalisation, every named council in the captured RFS and regional Traffic samples matched the configured NSW council vocabulary. This checks names; it does not establish polygon/point accuracy.
- At the conservative 126-byte payload budget, the marine product produced four correctly capped parts. The largest captured Traffic notices required 41 state-incident parts and 34 regional parts. Three state incidents and sixteen regional items exceeded 20 parts before applying user selection and active-state filters.

Sources compared: [BOM RSS catalogue and feed behaviour](https://www.bom.gov.au/rss/), [official RFS incident feed](https://www.rfs.nsw.gov.au/feeds/majorIncidents.json), [official Traffic incident feed](https://data.livetraffic.com/traffic/hazards/incident.json), [official Traffic regional feed](https://data.livetraffic.com/traffic/hazards/regional/lga-incidents-open.json), and [BOM marine warning detail](https://api.bom.gov.au/apikey/v1/warnings/warning/IDN20400).

## Corrected findings

| Area | Defect and consequence | Correction |
| --- | --- | --- |
| Traffic advice | Only the first advice field survived parsing. Additional advice and diversions disappeared from transmitted content. | Preserve distinct advice A/B/C, other advice and diversions. Include those values in revision detection, persisted detail and the Traffic page. |
| Traffic location/impact | Only the first period and lane were considered; cross streets and additional roads were discarded. | Preserve cross streets, second locations, additional roads and all supplied period/lane impact and direction strings. Multipart output preserves these fields. |
| Council names | Removing one trailing word left names such as a regional council unmatched; spelling and municipal-name variants also failed. | Normalise stacked council suffixes, city/municipality prefixes and the observed MidCoast spelling. Apply the same function across services. |
| Traffic duplicate feeds | Deduplicating IDs in fetch order could discard the selected flood/fire copy before filtering. | Retain feed copies through collection, choose an eligible copy after filtering and prefer the specialised feed. Record suppressed copies with a duplicate reason. |
| BOM revision detection | Enriched summaries, locations and hazard times could change without updating the broadcast hash. Referenced updates required a changed headline or expiry. | Hash the content used to prepare messages and recognise changed content on referenced updates. Include product/district selection in history's policy revision. |
| BOM cancellation | Current product selection could hide cancellation of a previously sent warning; referenced cancellations could repeat. | Check sent-warning cancellation before product filtering and suppress an identical confirmed cancellation. |
| BOM selection display | An eligible warning became “excluded” once deduplication prevented another transmission. | Separate policy eligibility from the decision to transmit again. |
| BOM district filtering | District-excluded items were removed during RSS parsing and vanished from the supposedly complete current-feed view. | Collect the complete feed and apply district filtering during processing. Keep excluded items and their reasons. |
| BOM presentation | Enriched locations, summaries and marine sections used on-air were not stored or shown on the BOM page. | Persist and display those fields. Read supplied API issue and expiry times; exclude expired warnings without treating expiry as hazard end time. |
| Feed validity | Well-formed HTML or an XML document without an RSS channel could count as a successful empty BOM feed. A missing link could produce an invented reference. | Validate the RSS channel and item identity, preserve failure rather than clearing the snapshot, and keep absent links absent. Validate major API response shapes. |
| Time normalisation | RSS publication time was treated as hazard onset. Naive dates were interpreted using the Docker host's timezone. Nonfinite Traffic publication dates passed freshness checks. | Keep issue and onset separate, decline to infer a timezone for naive values, and reject nonfinite publication timestamps. Honour Traffic's hidden-end-date flag in radio wording. |
| Traffic baseline | A dry-run poll marked the first-live baseline complete, causing existing notices to transmit when switched live. | Only a live poll completes the baseline. Existing matching items on that poll are recorded without transmission, as documented. |
| Service controls | RFS/Traffic could continue processing an in-flight fetch after monitoring was disabled. Already queued parts did not recheck Dry Run or selection. | Recheck settings after fetching and before queueing. Before sending each queued part, check Dry Run and source-specific eligibility/current revision; suppress invalid parts with a recorded failure. |
| RFS current view | Incidents absent from two successful polls remained in the current table until the 45-minute window elapsed. | Remove them from the current view after the second absence while retaining stored records/history and making no claim of resolution. |

## Original follow-up findings (now addressed)

1. **High — complete long notices cannot be admitted.** `app/delivery.py` and `app/transmit.py` reject notices with more than `QUEUE_MAX=20` parts. The live replay demonstrates this is reachable with real provider advice. The failure is recorded, but the whole notice is not sent, and an unchanged oversized notice is not retried. Increasing the cap alone increases backlog and does not establish a general solution. Recommended next change: queue bounded notice descriptors and stream their parts, preserving priority, confirmed-part retry and a limit on pending notices/bytes. Do not silently truncate advice to make the test pass.

2. **High — a recognised place name is not proof of an LGA footprint.** `app/bom_area.py::_explicit_councils` accepts names solely by vocabulary membership. A broad label such as Sydney can become a match for the Sydney council, causing a warning to be excluded for another selected metropolitan council. Unknown-match inclusion does not protect against a falsely confident match. Recommended next change: retain typed LGA provenance from the provider, require an explicitly administrative designation for name-only matching, and treat untyped place/district names as unknown. Existing tests explicitly accept shortened council-like names; this needs a deliberate matching-policy change, rather than adding more aliases indiscriminately.

3. **Medium — scheduled roadwork “active” is its overall lifecycle.** `TrafficItem.active()` checks ended/start/end, but not recurring closure windows in `periods` such as day-of-week or nightly hours. The SCHEDULED ROADWORK topic provides some context, but it does not prove the stated closure is happening now. Recommended next change: preserve and display the schedule, distinguish an active notice from a currently active closure, and only infer closure state where provider timezone/window semantics are established.

4. **Medium — one Traffic feed outage blocks all Traffic collection.** The client fetches all five feeds and fails the entire poll when any one fails, even if that feed is not selected. This prevents false disappearance accounting, but unnecessarily suppresses healthy selected feeds. Recommended next change: track successful/failed feeds separately, collect relevant healthy feeds, show partial status and advance missing counters only for successfully fetched feeds.

5. **Medium — fallback and freshness limits remain.** Simplified council boundaries can misclassify points near borders; a Traffic point does not describe the footprint of a long road closure. Invalid/missing coordinates rely on a regional council name or cause exclusion. BOM marine coast areas remain unknown LGA matches. API enrichment failures fall back to RSS without a separate enrichment-health indicator. RFS freshness uses local fetch time rather than an established provider publication timezone. These limits should remain visible to the operator; a successful fetch alone does not prove current or geographically complete hazard data.

6. **Presentation/completeness — pages and provider fields are summaries.** Current-list queries cap results at 500; captured Traffic items across the two sampled feeds already total 511. The UI needs pagination or a displayed count/truncation indicator to avoid implying it shows everything. RFS size/responsible agency and Traffic schedule, public-transport and ancillary fields are not fully represented. Required broadcast fields should be agreed explicitly before adding every provider field, because multipart size is already a delivery constraint.

## Behaviour and upgrade implications

The existing conservative BOM unknown-area option, RFS alert-level/council filters, Traffic ended/future exclusion, scheduled-roadwork opt-in and Traffic fire suppression while RFS is enabled remain policy choices. RFS/fire suppression applies broadly and does not prove that an identical RFS notice was delivered. Feed disappearance is recorded without claiming hazard resolution or a reopened road.

The new database columns migrate additively. Revised hash definitions can trigger a one-time update of current previously sent notices on upgrade. The Traffic first-live baseline now also applies after dry-run preview. Complete advice now uses bounded notice descriptors rather than a 20-part admission limit. None of the replay tests establishes reception by another MeshCore node; success still means confirmation by the local radio.

To rebuild the test environment with the implemented fixes:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
```

Keep the existing external test volume. The implementation is uncommitted; rebuilding loads the working-tree source.
