# Data snapshots

Version 0.1.1. See [the correctness review](HARDENING_REVIEW.md) for the trust
boundary: receipt validation is not independent proof of source authenticity.

`ashare_agent.data` defines the data boundary used by the research and paper
trading workers. A `Snapshot` contains normalized daily market rows,
securities, a known trading calendar, corporate actions, and benchmark rows.
The snapshot is a replay input: it does not call Tushare when loaded and its
`snapshot_id` is retained in reports.
Tushare-built IDs include evidence, raw-cache, and normalized-table
fingerprints, so a refreshed source partition cannot silently reuse the old
ID.

## Snapshot files

`save_snapshot(snapshot, root)` validates the structural contract and creates
an immutable directory named by `snapshot.snapshot_id`. It contains
`market.parquet`, `securities.parquet`, `actions.parquet`,
`benchmarks.parquet`, `calendar.json`, and `manifest.json`. The manifest stores
one SHA-256 hash per file plus a content ID. `load_snapshot` verifies every
file hash, the content ID, the snapshot ID, and the structural contract before
returning. Saving a different snapshot to an existing ID raises rather than
replacing the old data.

Market fields include raw OHLC, `volume` in shares, `amount` in CNY,
`adj_factor`, limits, `suspended`, and explicit ST/industry knowledge flags.
A historical security/calendar spine exists before quote joins. A quote-less
session retains null OHLC; only explicit full-day suspension evidence permits
a known suspended state. Otherwise its status is unknown. `valuation_price`,
`valuation_date`, and `valuation_kind` are separate stale-mark fields, never
execution prices. A missing quote across an ex-date invalidates the carried mark.
`available_at`, when present, is timezone-aware; real data additionally requires
the complete receipt contract below, even when this optional column is absent.

`Snapshot.sql` exposes read-only DuckDB views named `market`, `securities`,
`actions`, `benchmarks`, and `calendar`. Only `SELECT`, `WITH`, and
`DESCRIBE` statements are accepted.

## Validation and trading readiness

`validate_snapshot` returns `errors`, `warnings`, and `tradable`. Structural
errors include missing required fields, duplicate security/session rows,
dates outside the calendar, invalid numbers, and invalid listing intervals.
Unknown historical ST or industry status is preserved and produces a warning;
it makes `tradable` false while still allowing a research-only snapshot to be
saved. `actions_complete` must be explicitly true in metadata. When actions
exist, every action must also have a known-at date. The synthetic demo marks
its actions as complete because its empty action table is part of the fixture,
not because real-world corporate actions were inferred.

## Tushare raw cache

`TushareDownloader(root, token=None)` stores immutable partitions below
`root/raw/<endpoint>/<logical_key>/revisions/<revision>/`, containing
`data.parquet` and `manifest.json`. Logical keys are dates, ranges or separate
stock status keys. Each manifest records endpoint, request parameters, revision,
fetched time, row count, SHA-256, status and subrequest parameter trace. Failed
requests also create a revision; an older success does not hide a latest failure.
`transport_complete` is separate from `source_coverage_verified` (false).
Stock metadata and calendars have a default 24-hour TTL; historical quote
partitions require explicit refresh. Overlapping unique keys with different
values are rejected; identical rows may be deduplicated. Legacy flat caches lack
these manifests and are rejected: retain them for audit and download into a new
root, rather than assuming refresh converts old files. It lazily creates a Tushare
Pro client only when a missing partition needs downloading; importing the
module never contacts the network. `download` returns a status, cache paths,
row counts, and warnings. A complete raw cache is still marked
`trading_ready: false` until normalized and checked.

`capabilities(date)` is explicitly a cache-only report and sets
`network_checked=false`. An explicit `capabilities(date, probe=True)` call may
perform a small `trade_cal` probe when a client is configured; the result then
records whether that probe succeeded. A configured token alone is never
presented as verified access.
Provider calls use a small configurable interval between requests; tests can
inject zero when no real network is involved.

The current raw endpoint set is `daily`, `adj_factor`, `stk_limit`,
`suspend_d`, `trade_cal`, `stock_basic`, `dividend`, and `index_daily` for
`000300.SH`. Daily endpoints are cached one open session at a time so a
partial response is not treated as complete merely because it has rows.
`stock_basic` is paginated and cached separately for list statuses `L`, `D`,
and `P`, with listing/delisting fields explicitly requested. Calendar responses
must cover every natural date in the requested range with unique valid flags;
there is no weekday fallback. Tushare `vol` is
converted from hands to shares and `amount` from thousand CNY to CNY.
Normalization maps only 600/601/603/605 and 000/001/002/003 prefixes to
`MAIN`; 688 is `STAR`, 300/301 is `CHINEXT`, and recognized 4/8/92 prefixes
are `BSE`. Other prefixes, including 200 and 900 B-share codes, are marked
`OTHER` so they cannot enter the supported baseline by accident.
`suspend_d` rows with event type `S` are marked suspended, `R` rows are
treated as resume events, and an unrecognized or absent event type sets an
incomplete evidence flag. These events do not independently establish an
all-day halt. Missing quotes receive spine rows, never invented quote prices.

Tushare's `cash_div` is treated as the documented tax-after (net) amount;
`cash_div_tax` is tax-before and is never substituted as net. `stk_div` is
already the per-share additional-share ratio and is not divided by ten.
Only rows with `div_proc=实施` become executable action rows. Other dividend
rows are excluded from the action table but recorded in `unknown_actions`, so
they cannot silently disappear into a formally tradable snapshot. Implemented
rows are marked unknown when they lack `ex_date`, implementation announcement
date, or a relevant settlement/share-list date. The implementation date field
is `imp_ann_date` and the share-list field is `div_listdate`.

Each request has a configurable retry count and row limit. A partition that
reaches the limit is retried by symbol chunks when a cached stock universe is
available; if it still reaches the limit, the result cannot receive complete
status and the overall download is reported as `partial`.

## Point-in-time evidence

Tushare's current stock metadata does not establish historical ST state or
historical industry classification. `build_snapshot` therefore initializes
these fields as unknown (`state_known=False`, `industry_known=False`) unless
matching evidence is supplied. It never fills them from today's `stock_basic`
labels. Quote-less sessions remain unknown unless explicitly proven suspended.
A real `index_daily` response becomes the benchmark table; when
it is absent the benchmark table is empty and the metadata carries
`benchmark_missing=true`. No synthetic index is calculated from the stock
rows.

Pass an `evidence_dir` containing any of the following files. Each file may be
Parquet, CSV, JSON Lines, or JSON array/object with the same stem:

* `state`: `ts_code`, `trade_date`, `is_st`; an optional `state_known` flag may
  be supplied. Each row must be a historical label that was available by that
  trade date.
* `industry`: `ts_code`, `trade_date`, `industry`; an optional
  `industry_known` flag may be supplied. Each row must be a historical
  classification with its own source/version retained by the evidence owner.
* `availability`: `ts_code`, `trade_date`, `available_at` (a timezone-aware
  timestamp). This records when a complete daily row became available to the
  pipeline and is merged into the optional market column of the snapshot.
* `suspensions`: `ts_code`, `trade_date`, `full_day` (a real boolean). Duplicate
  keys or a full-day assertion conflicting with a quote are rejected.
* `opening`: `ts_code`, `trade_date`, `opening_volume` in shares,
  `opening_observed_at` with timezone. Real-data fills require positive capacity
  evidence observed between 09:25 and 09:30 on the execution date. This is only
  a necessary condition, not proof of queue priority or absence of all intraday
  look-ahead in a daily execution proxy.
* `actions`: optional supplementary normalized corporate action rows matching
  `ACTION_COLUMNS`, including actions whose record date is inside the sample
  but ex-date is later. Conflicting events are rejected.
* `actions_complete.json`: either `true` or
  `{ "actions_complete": true }`. This marker is required before corporate
  actions can make a normalized snapshot formally tradable, but is not sufficient.

Additionally, `inventory_receipts.json` and `session_receipts.json` are JSON
objects keyed by ISO trade date. Every receipt contains `source`, `payload`,
`content_hash`, `published_at`, `fetched_at`, and `available_at`. All timestamps
must have timezones, publication/acquisition cannot exceed availability, and
availability cannot exceed the decision cutoff. This includes warmup dates.

Inventory payload is `{trade_date, complete_inventory, scope:"SSE_SZSE_MAIN"}`.
The inventory is sorted by code and includes code, listing date, exchange and
board. Session payload is defined by `hardening.session_payload`: complete market
rows, benchmark, active inventory, known corporate actions and next session.
Canonical hashing is defined by `hardening.content_hash`; receipts bind exact
normalized content, not merely a date label. Import adapters must preserve the
original source archive and an auditable transformation chain.

`ingest_receipt` stamps actual current acquisition and availability; it does not
provide a backdating option. Current downloads cannot establish availability
at a historical cutoff. Importing an old receipt is allowed only as a source
attestation whose authenticity must be independently audited; generating a hash
from today's reconstruction does not supply that proof. Missing/late/mismatched
receipts retain `unverified_reconstruction` and deny formal simulation.

All evidence files enter snapshot identity. Unknown or date-only evidence does
not acquire an invented historical publication time. A boolean knowledge label
or completeness marker alone never establishes historical PIT readiness.

## Synthetic demo

`make_demo_snapshot(sessions=520, seed=42)` creates deterministic synthetic
prices for 16 main-board symbols, 520 market sessions, an explicit known status
and industry for each row, and one future weekday in `calendar` after the last
market row. Prices use a 0.01 tick and volumes are 100-share lots scaled to
exercise the baseline liquidity path. Its ID is versioned as `demo-v2-*` so
older saved fixtures are not overwritten. The extra date is a synthetic
weekday, not an SSE holiday-aware trading calendar. The fixture is suitable
for software and ledger tests only. Its content is labelled `synthetic` and
must not be presented as market performance evidence.
