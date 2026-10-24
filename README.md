# finviz-mcp

Read-only MCP server for [finviz.com](https://finviz.com) US market data, with a hard
**robots.txt compliance guard** in front of every request.

14 tools, stdio transport, no API key required. Built for agents that need US fundamentals and
market context next to an Indonesian-market data stack.

```bash
python finviz_cli.py list                                  # every tool, one line each
python finviz_cli.py quote ticker=AAPL
python finviz_cli.py quote_batch tickers=AAPL,MSFT,JPM preset=valuation
python finviz_cli.py group_performance group=sector
```

## Why it exists

The obvious Finviz tool is the screener — and it is the one you cannot use. Every
`/screener?...` URL redirects to `/screener/stocks?...`, which is `Disallow`ed by finviz's own
robots.txt, and the allow-listed variants (`v=340`, `v=320`, `v=210`) are JavaScript-rendered:
the HTML comes back 400 KB and contains **zero table rows**. The only CSV export endpoint is
disallowed *and* paywalled. So this server refuses screener calls by design and gives you
per-ticker fundamentals instead, which is what fundamental analysis actually needs.

## Tools (14)

| Tool | Source | Robots | Notes |
|---|---|---|---|
| `health` | `/` | OK | reachability + robots state + tool count |
| `robots_policy` | `/robots.txt` | OK | explains allow/deny for any path |
| `quote` | `/stock?t=` | OK | ~83 fundamentals/technicals for one ticker |
| `quote_batch` | `/stock?t=` | OK | fundamentals comparison for a **known** ticker list, 1..40 tickers |
| `signals_snapshot` | `/` | OK | 13–14 homepage mover blocks (gainers, new high, overbought, …) |
| `market_overview` | `/` | OK | breadth, futures, forex/rates/crypto, headlines, calendar |
| `news` | `/news?t=` | OK | headlines + source + url |
| `insider_trades` | `/insidertrading` | OK | latest Form-4 style rows |
| `economic_calendar` | `/api/calendar/economic` | OK | JSON, actual/forecast/previous, windowed |
| `earnings_calendar` | `/api/calendar/earnings` | OK | EPS estimate vs actual, surprise, 1-day price reaction |
| `dividends_calendar` | `/api/calendar/dividends` | OK | ex-date, ordinary/special, yield |
| `group_performance` | `/groups.ashx?g=` | OK | 11 sectors / 148 industries: stocks, cap, div, P/E, chg %, volume |
| `screener_signal` | `/screener?v=111&s=` | **DENIED** | returns a diagnosis, not rows |
| `custom_screener` | `/screener?v=111&f=` | **DENIED** | returns a diagnosis, not rows |

### `quote_batch` — the screener replacement

`_FUNDA_PRESETS`: `valuation` (P/E, P/S, P/B, P/C, P/FCF, PEG, EV/*, Price/*),
`profitability` (EPS, ROE, ROA, ROI, Margin, Sales), `growth` (Growth, EPS this Y,
EPS next Y, EPS next 5Y, EPS Q/Q, Sales Q/Q), `balance` (Debt, Current Ratio, Quick Ratio,
Book/sh, Cash/sh), plus `all`. `extra_metrics` adds exact labels; `include_header` adds
sector/industry/country per ticker.

Preset matching is by **label keyword**, not exact string, so a small label drift on Finviz
degrades to a missing column instead of a silently empty one. Measured: 3 tickers in **2.8 s**
at the default 1 s rate limit, 10 columns returned.

`quote_batch` does not discover tickers. If you do not already know the list, this server
cannot help you, and any tool here that pretends otherwise is lying.

### Calendar pagination

The internal JSON APIs return **50 rows per page** and ignore `pageSize` (verified).
`_calendar_collect()` merges up to **3 pages** (150 rows) before applying filters, because
row-level filters over a single page silently miss matches on later pages. Verified effect:
`min_yield=8` on dividends went from **4 rows to 22**. Page counts are reported back as
`counts.pages_fetched` and `counts.raw_items_considered`.

### Known-unavailable endpoints — do not re-probe

* `/api/quote` responds but always returns `400 {"error":"invalid quote query"}` for 16
  parameter shapes tried.
* `/map.ashx` (heatmap) renders via a JS bundle; the data is not in the HTML.
* `/etf`, `/earnings`, `/insider` (without `.ashx`) → 404.

### Unexplained behaviour

A **wider** date range can return **fewer** rows (1–30 Sep → 17 items; 8–11 Sep → 18 items).
The endpoint appears to window or offset results itself. Date ranges are not fully
deterministic — do not build anything that depends on strict monotonicity.

## Compliance model

`robots.txt` is fetched live (cached 24 h) and evaluated with a correct longest-match matcher
(`*` wildcard, `$` anchor, `Allow` wins ties). Every request is checked **before** sending
**and again against the final URL after redirects** — that second check is what catches the
`/screener?*` → `/screener/stocks?*` trap.

Verified policy map:

| Path | Verdict |
|---|---|
| `/`, `/stock?t=`, `/news?t=`, `/insidertrading`, `/calendar/*`, `/api/calendar/*` | allowed (no matching rule) |
| `/screener?v=340&s=…`, `v=320`, `v=210` (their ~35-URL allowlist) | allowed but JS-rendered |
| `/screener?v=111&s=…`, `/screener?v=111&f=…` | **Disallow: /screener?*** |
| `/export.ashx`, `/api/v1/screener-export-csv` | **Disallowed** (and paid) |

Screener tools therefore return a refusal with an explanation. They only execute when the
caller passes `allow_noncompliant=True` explicitly — the response is then flagged
`robots_compliant: false`. `FINVIZ_ALLOW_NONCOMPLIANT=1` flips the default globally; that is
your call to make, not this server's.

### Three stdlib robots.txt traps this server works around

Do not "simplify" this code away:

1. `RobotFileParser` stores `User-agent: *` rules in `rp.default_entry`, **not** in `rp.entries`.
   Reading only `entries` silently allows every request.
2. `RuleLine.path` is percent-quoted, so `/screener?*` is stored as `/screener%3F%2A`; it must
   be `unquote()`d before matching or the rule never fires.
3. Python's built-in `can_fetch` treats `*` literally, so it also gets `/screener?*` wrong.
   This server does its own longest-match evaluation.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Register with Hermes (or any MCP client that speaks stdio):

```yaml
mcp_servers:
  finviz:
    command: /path/to/.venv/bin/python
    args: ["/path/to/finviz_mcp_server.py"]
    trust: full
    timeout: 120
    env:
      FINVIZ_MIN_INTERVAL: "1.0"
      FINVIZ_CACHE_TTL: "60"
```

No env block is required — every variable has a default. The server never reads secrets,
because it has none.

## Usage

Via an MCP client: `finviz_quote(ticker="AAPL")`, `finviz_economic_calendar(min_importance=3)`.

Without a gateway restart, use the CLI (it speaks JSON-RPC over stdio to the same server):

```bash
python finviz_cli.py list
python finviz_cli.py quote ticker=AAPL
python finviz_cli.py quote_batch tickers=AAPL,MSFT,JPM preset=all
python finviz_cli.py group_performance group=industry
python finviz_cli.py economic_calendar min_importance=3
python finviz_cli.py screener_signal signal=topgainers allow_noncompliant=true
```

Argument coercion follows the **tool's declared schema**, not punctuation: `tickers=AAPL,MSFT`
becomes an array because the parameter is `list[str]`, while
`filters=fa_div_o6,fa_pe_u20` stays one string because the parameter is `str`. Getting this
wrong is the most common way to break the CLI, so it is handled centrally in `coerce_typed()`.

### Runtime test scripts

| Script | What it proves |
|---|---|
| `verify_mcp.py` | real MCP SDK client: initialize, `tools/list`, live calls |
| `smoke_test.py` | tool-by-tool functional pass |
| `smoke_new_tools.py` | the three JSON-API calendar tools + regression |
| `verify_new_tools.py` | calendar row shapes and pagination counters |
| `regression_all.py` | all older tools after a change; asserts the two screener tools *fail* for the right reason |

They locate the server and interpreter relative to themselves (`HERE`), and accept overrides:
`FINVIZ_PY`, `FINVIZ_SERVER`, `FINVIZ_ENV_FILE`, `FINVIZ_STDERR_LOG`. `FINVIZ_ENV_FILE` is
optional — without it they inherit `os.environ`, so they work outside any agent framework.

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `FINVIZ_MIN_INTERVAL` | `1.0` | seconds between requests — deliberate, do not lower |
| `FINVIZ_CACHE_TTL` | `60` | response cache seconds |
| `FINVIZ_ROBOTS_TTL` | `86400` | robots.txt cache seconds |
| `FINVIZ_TIMEOUT` | `25` | per-request timeout |
| `FINVIZ_USER_AGENT` | see source | override UA |
| `FINVIZ_ALLOW_NONCOMPLIANT` | `0` | `1` = allow disallowed screener paths globally |

## Limitations / risks

* **US markets only.** No IDX coverage; use a separate source for Indonesian data.
* Free tier is **delayed**, not real-time. Real-time + export is Finviz Elite.
* Finviz is migrating to React-rendered pages (`v=340`). Expect breakage on their next view change.
* Scraping at volume risks IP blocks. The 1 s limiter is deliberate.
* Data is external and unverified: label outputs `partially verified`, never authoritative.
* `quote_batch` costs one request per ticker at 1 s each — 40 tickers is ~40 s. Batch size is
  capped for that reason, not by an API limit.
* Calendar date ranges are not fully deterministic (see above).

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Finviz.

Educational/analytical use only. Not investment advice.
