# Implementation contracts v1

Modules separate data, strategy, research, risk, execution, accounting and reporting. Trading source, configuration and processed historical inputs are bound to each ledger; source changes require a new versioned replay. The report records their hashes and the actual record creation time.

Dates: ISO YYYY-MM-DD strings, timestamps timezone-aware Asia/Shanghai or UTC. Money in CNY, volume in shares, amount in CNY. No network calls at import. No real broker implementation.

## data.py (data worker)

`Snapshot` dataclass: `market: pd.DataFrame`, `securities: pd.DataFrame`, `calendar: list[str]`, `actions: pd.DataFrame`, `benchmarks: pd.DataFrame`, `metadata: dict`. `snapshot_id` property from metadata.

market required columns: ts_code, trade_date, open, high, low, close, volume, amount, adj_factor, up_limit, down_limit, suspended (bool), is_st (bool), state_known (bool), industry (str), industry_known (bool). available_at is optional for historical price replay and required for the current forward session. One row per active security/session, suspended rows explicitly flagged. Missing rows are NOT presumed suspension. Preserve unknown status flags.

securities: ts_code, list_date, delist_date (nullable/empty, delisting date exclusive), exchange ('SSE'/'SZSE'/'BSE'), board ('MAIN'/'CHINEXT'/'STAR'/'BSE'), name. Listing dates may precede data calendar; strategy min listing age uses business calendar count when available (missing prehistory conservative).

actions columns: action_id, ts_code, record_date, ex_date, pay_date, share_list_date, cash_per_share (NET amount assumption disclosed), bonus_ratio (additional shares / existing share), known_at. All date columns ISO. `actions_complete` metadata boolean required for formal simulation. Engine handles entitlement at record-date close, ex-date receivable/shares, cash payment date/share availability. Supported cash/bonus only; unknown actions fail closed. No actions allowed to silently disappear.

benchmarks: trade_date, ts_code, close; metadata benchmark_kind ('price'/'total_return'/'synthetic').

`save_snapshot(snapshot, root: Path) -> Path` immutable Parquet directory + JSON manifest with hashes and content ID. `load_snapshot(path: Path) -> Snapshot` validates hashes. `validate_snapshot(snapshot) -> dict` keys errors(list[str]), warnings(list[str]), tradable(bool). Structural invalid raises on save; unknown status may save research-only snapshot with tradable false. `make_demo_snapshot(sessions: int=520, seed: int=42) -> Snapshot` synthetic labelled, >=12 symbols, calendar includes one future session after market end for next-day proposals. `TushareDownloader(root: Path, token: str|None=None)` with `download(start: str,end: str,refresh: bool=False) -> dict`, `capabilities(date: str) -> dict`; raw caches may not be trading-ready; expose `build_snapshot(start,end, evidence_dir: Path|None=None) -> Snapshot` if possible, require explicit point-in-time evidence for unknown fields. Document evidence formats. `snapshot.sql(query: str) -> pd.DataFrame` optional DuckDB read-only query view of snapshot.

## strategy.py and research.py (strategy worker)

Consume snapshot duck-typed attrs above; consume plain config dictionaries.

`rank_candidates(snapshot, as_of: str, config: dict) -> pd.DataFrame`: no reads after as_of. Returns ts_code, rank, score, close(raw), ma120(adjusted), momentum60, momentum120, volatility20, atr14(raw scale as_of), adv20(shares), amount20, industry, eligible(bool), reason(str). Include supported active securities with sufficient history even if ineligible so existing holdings exits inspect trend. Only eligible get positive ranks; others null ranks.

`market_regime(snapshot, as_of: str, benchmark: str='000300.SH') -> dict` regime, score (not probability), signals. `is_rebalance_day(calendar: list[str], as_of: str) -> bool`: last trading day of ISO week by looking at next calendar trading date (calendar schedule known in advance). 

`select_targets(ranking: pd.DataFrame, held_symbols: list[str], rebalance: bool, config: dict) -> dict[str,float]`: target weights, retain top20 eligible holdings, fill new top10 at rebalance, no new entries off rebalance. Exiting holdings omitted; root handles risk trims. Suspended existing holdings should not automatically be liquidated for suspension; distinguish explicit invalidation vs missing data.

Config strategy keys min_listing_days=250, liquidity_min=100000000, candidate_count=20, max_positions=10, entry_rank=10, keep_rank=20, target_weight=0.08, max_gross=0.8, max_industry=0.25, momentum_windows=[60,120], trend_window=120. No silent factor fallback. Data status unknown is ineligible.

Research: strict Pydantic response schema with symbol, as_of, status, thesis, catalyst, invalidation, risk_flags, sources with published_at, evidence_quality, model_version,prompt_version. `research_candidates(ranking, as_of: str, config: dict, cache_dir: Path, evidence: list[dict]|None=None) -> list[dict]`. Default disabled/mechanical narrative, clearly not LLM. Optional generic HTTP provider with timeouts/budget/cache, read API env vars, errors produce unavailable records, no execution/portfolio fields. Never historical AI alpha claims.

## analytics.py/reporting.py (analytics worker)

`compute_metrics(equity: pd.DataFrame, fills: pd.DataFrame, initial_cash: float, benchmark: pd.DataFrame|None=None) -> dict`. equity columns trade_date,equity,cash,receivable,exposure,regime; fills columns fill_id,order_id,ts_code,side,quantity,price,fee,trade_date,realized_pnl (net closed-lot PnL where provided; buy rows null). CAGR period uses count of sessions incl first equity day /252; initial_cash is starting baseline preceding first row. Report formulas and null undefined. Benchmark must include previous session anchor when possible, avoid first-day omission. Include annual return, total return, costs, turnover, drawdown duration, Sharpe,Sortino,Calmar, exposure, trade win/PF close-fill pairing clearly. Regime attribution uses prior day's regime, since today's realized return cannot be predicted by today's close label; engine supplies exposure regime for period.

`write_report(output_dir: Path, payload: dict) -> dict[str,str]` writes report.md/report.html and JSON; HTML escape untrusted content, no remote assets. Payload keys run_id,mode,snapshot_id,synthetic,as_of,config,metrics,candidates(list records),research(list records),orders(list records),positions(list records),equity(list records),warnings(list strings),data_quality(dict). Return markdown/html/json file path strings. Compact offline equity SVG chart welcome. Clearly mark synthetic data and daily approximate fills.

`walk_forward_windows(calendar: list[str], start: str,end: str,train_sessions:int=252,test_sessions:int=63) -> list[dict]` fields train_start,train_end,test_start,test_end. Non-overlapping OOS windows, no parameter training in fixed baseline. Root engine runs windows independently initialized and explicitly reports not stitched tradable equity.

## Root config

Ledger `positions(date)` reads the current account, with `date` used only for lot unlocking. It is not a historical as-of query. `available_to_sell` means unlocked quantity minus all active sell reservations, including frozen future-session intents; this prevents double reservation. Use the saved daily report for historical positions. Bonus lots add zero book cost while preserving total original cost; per-fill FIFO realized win statistics may therefore differ from an economic trade grouping across corporate actions.

`Settings` Pydantic config sections strategy(dict-like via model_dump), risk, execution, data, research. Root engine converts sections to dict for worker interfaces. Only BACKTEST/PAPER accepted.
