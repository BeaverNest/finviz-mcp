# finviz-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-6E56CF.svg)](https://modelcontextprotocol.io)
[![Tools](https://img.shields.io/badge/tools-14-2ea44f.svg)](#tools-14)
[![Robots](https://img.shields.io/badge/robots.txt-enforced-critical.svg)](#compliance-model)
[![API key](https://img.shields.io/badge/API%20key-not%20required-lightgrey.svg)](#install)

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

The obvious Finviz tool is the screener — and the docs used to say it was unusable. That was
wrong, and the correction is the most useful thing in this repo. Every `/screener?...` URL
does redirect to `/screener/stocks?...`, which finviz's own robots.txt `Disallow`s for generic
clients — so **custom filters still require an explicit opt-in**. But the allow-listed signal
URLs (`v=340`, `v=320`, `v=210`) work, and the "JavaScript-rendered, zero rows" claim was this
server's own parsing gap: those pages ship their full result set inside a
`<script type="application/json">` blob at `data.rows[]`, not as an HTML `<table>`. Reading
that blob yields typed numbers and every row on the page (`f=industry_shellcompanies` returns
all 328 shell companies, 20 per page, with `marketCap`/`price` as floats, and `v=121`/`v=161`
give valuation and balance-sheet fields). Use `screener_signal` for the allow-listed signals
and `custom_screener` with `allow_noncompliant=True` for arbitrary filters. For fundamentals
on a list of tickers you already have, `quote_batch` is still cheaper and fully compliant.

## Tools (16)

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
| `screener_signal` | `/screener?v=340&s=` | OK | allow-listed signals only; rows via embedded JSON |
| `custom_screener` | `/screener?v=&f=` | **opt-in** | arbitrary filters, one page; needs `allow_noncompliant=True` |
| `screener_filters` | `/screener` | **opt-in** | catalogue of 23 groups / 828 valid `f=` values, searchable |
| `screener_all` | `/screener` | **opt-in** | paginated fetch, `max_pages` budget (default 3, cap 50) |

### Reading screener rows without a 1e6 error

Free-tier finviz reports `marketCap` in **millions of USD** — the raw float is
`48499.35` for a $48.5B company. Both screener tools therefore also emit
`marketCap_usd` and a `_units` legend (`marketCap`, `price`, `prevClose`, `change`,
`volume`), while leaving the original keys untouched. Measured against `quote()`:
screener `5149206.09` == quote `5149.21B`.

`screener_all` stops on its own rather than trusting the page count: offsets past the
end **clamp** to the last page (`r=341/401/981` all return `first_row=320` with the
same 8 rows) and `r>=1001` is HTTP 403, so a naive loop would spend its entire budget
re-reading one page. Verified full pull: `f=industry_shellcompanies` → 17 pages, 328
unique tickers (== `totalCount`), 0 duplicates, 28.7 s.

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
| `/screener?v=340&s=…`, `v=320`, `v=210` (their ~35-URL allowlist) | **allowed — and usable** (rows in embedded JSON) |
| `/screener?v=111&s=…`, `/screener?v=111&f=…`, `/screener/stocks?*` | **Disallow: /screener?*** and `Disallow: /screener/stocks?*` |
| `/export.ashx`, `/api/v1/screener-export-csv` | **Disallowed** (and paid) |

`screener_signal` runs **without** any override for the allow-listed signals — an earlier
version hardcoded `v=111` and so denied itself; if a signal URL is not Allow-listed it still
refuses. `custom_screener` needs `allow_noncompliant=True` because arbitrary `f=` filters
302-redirect into `/screener/stocks?*`; the response is then flagged `robots_compliant: false`.
`FINVIZ_ALLOW_NONCOMPLIANT=1` flips the default globally; that is your call to make, not this
server's.

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
