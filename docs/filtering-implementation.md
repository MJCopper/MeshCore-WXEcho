# Filtering implementation — 3 October 2026

All six follow-up findings from the review are implemented.

| Finding | Implemented behaviour |
| --- | --- |
| BOM geographic provenance | Match complete usable polygons, typed LGA names, or explicitly administrative names. Bare place names and incomplete/invalid footprints remain unknown, with reasons visible. Retain provider geocode types, codes and names. |
| Long notices | Queue complete notice descriptors and stream parts. Bound pending notices to 20 and stored text to 1 MiB, with a reserved verification slot. Capacity defers notices without truncation. Confirmed-part recovery survives restart; failures stop the remaining parts of that notice. |
| Roadwork schedules | Preserve recurring days, hours, impact and direction. Separate notice lifecycle from scheduled impact windows. Evaluate supported windows only with established timezone semantics; missing timezone or ambiguous ranges remain unknown. A schedule is not evidence of an actual closure. |
| Independent Traffic feeds | Fetch selected feeds independently. Healthy feeds continue through partial outages. Persist per-feed errors, publication times and success times. Missing counters and first-live baselines advance per successful feed only. |
| Uncertainty and freshness | Show BOM enrichment availability, feed observation age, provider publication information and geographic match methods/reasons. Traffic border points remain uncertain; regional-name fallback and approximate point boundaries are labelled. RFS timestamps with unspecified timezone remain raw. |
| Complete presentation | Paginate source records with total counts rather than silently capping at 500. Persist/display enriched BOM detail, RFS size and agency, and Traffic road detail, schedule, advice, diversions, public transport and additional information. |

## Fields preserved for transmission

BOM transmits warning identity/product, hazard areas, summary/detail, supplied hazard times, marine sections, cancellation/update state and supplied source attribution. RFS transmits incident identity, alert level, location/council, type/status, size and responsible agency. Traffic transmits category/title, roads and locations, council and match method, direction/impact, advice/diversions, supplied schedule, public transport and additional information. Unknown times are not replaced with host-local assumptions. Source-specific filters still determine eligibility; collection and confirmed transmission are recorded separately.

## Validation

252 tests pass, including 80 regressions beyond the original 172-test baseline. Coverage includes malformed feeds, partial outages, first-live baselines, ambiguous geography, overnight/DST schedules, 41-part delivery, interrupted delivery recovery, additive migration and pagination through 511 records for each source. Tests use isolated databases and radio doubles. One Starlette/httpx deprecation warning remains.

The test database was backed up to `/data/wx-echo-before-filtering-20261003.db` inside the existing external Docker volume before deployment. The image was rebuilt and deployed; health, dashboard, BOM, RFS, Traffic and History returned HTTP 200. Live BOM RSS and detail API requests succeeded, and two current warnings persisted enriched data and honest unknown-footprint reasons. Existing service and dry-run settings are retained. Captured official feeds replayed successfully: 36 RFS, 127 incident and 384 regional records. The largest Traffic notice was 44 parts; all parts respected the 126-byte budget.

## Practical limits

Simplified geographic boundaries and a single road point do not establish the full affected footprint. Missing provider timezone prevents a confident current schedule determination. Successful fetching does not prove the provider data is current. Local radio confirmation does not prove reception by another node. Notices exceeding the byte-storage bound are deferred, never silently shortened.
