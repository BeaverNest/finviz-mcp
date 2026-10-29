#!/usr/bin/env python
"""Finviz MCP server for Hermes — read-only market data with robots.txt compliance guards.

Design notes
------------
* stdio transport (default for FastMCP); launched by Hermes as a local MCP server.
* Every request is checked against https://finviz.com/robots.txt *live* (cached 24h).
  Endpoints the site disallows (e.g. generic ``/screener?*``) are refused unless the
  caller explicitly passes ``allow_noncompliant=True`` — the refusal explains why.
* Rate limited (default >=1s between requests) + TTL cache, so we behave like a polite
  client, not a crawler.
* All payloads carry provenance: source_url, fetched_at, robots_compliant, cache_age_s.
* Data is external and free-tier (delayed); treat as `partially verified`, never as
  authoritative, and never as investment advice.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlencode
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
try:  # mcp >= 2.0 (FastMCP pindah ke mcpserver.MCPServer)
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

BASE = "https://finviz.com"
USER_AGENT = os.environ.get(
    "FINVIZ_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
ROBOTS_UA_TOKEN = os.environ.get("FINVIZ_ROBOTS_UA", "hermes-finviz-mcp")
MIN_INTERVAL = float(os.environ.get("FINVIZ_MIN_INTERVAL", "1.0"))
CACHE_TTL = int(os.environ.get("FINVIZ_CACHE_TTL", "60"))
ROBOTS_TTL = int(os.environ.get("FINVIZ_ROBOTS_TTL", "86400"))
TIMEOUT = float(os.environ.get("FINVIZ_TIMEOUT", "25"))
ALLOW_NONCOMPLIANT_ENV = os.environ.get("FINVIZ_ALLOW_NONCOMPLIANT", "0") == "1"

DISCLAIMER = (
    "External free-tier market data (delayed, US markets only). Educational/analytical use "
    "only — not investment advice."
)

# Signal names served by finviz's own screener shortcuts. The first group is explicitly
# Allow-ed in robots.txt; the second group is reachable but goes through /screener?* which
# robots.txt Disallows for generic clients.
ROBOTS_ALLOWED_SIGNALS = {
    "topgainers": "/screener?v=340&s=ta_topgainers",
    "toplosers": "/screener?v=340&s=ta_toplosers",
    "newhigh": "/screener?v=340&s=ta_newhigh",
    "newlow": "/screener?v=340&s=ta_newlow",
    "overbought": "/screener?v=210&s=ta_overbought",
    "oversold": "/screener?v=210&s=ta_oversold",
    "mostactive": "/screener?v=320&s=ta_mostactive",
    "mostvolatile": "/screener?v=320&s=ta_mostvolatile",
    "unusualvolume": "/screener?v=320&s=ta_unusualvolume",
    "earningsbefore": "/screener?v=340&s=n_earningsbefore",
    "earningsafter": "/screener?v=340&s=n_earningsafter",
    "upgrades": "/screener?v=340&s=n_upgrades",
    "downgrades": "/screener?v=340&s=n_downgrades",
    "insiderbuys": "/screener?v=340&s=it_latestbuys",
    "insidersales": "/screener?v=340&s=it_latestsales",
    "majornews": "/screener?v=320&s=n_majornews",
    "tlsupport": "/screener?v=210&s=ta_p_tlsupport",
    "tlresistance": "/screener?v=210&s=ta_p_tlresistance",
    "horizontal": "/screener?v=210&s=ta_p_horizontal",
    "wedgeup": "/screener?v=210&s=ta_p_wedgeup",
    "wedge": "/screener?v=210&s=ta_p_wedge",
    "wedgedown": "/screener?v=210&s=ta_p_wedgedown",
    "channelup": "/screener?v=210&s=ta_p_channelup",
    "channel": "/screener?v=210&s=ta_p_channel",
    "channeldown": "/screener?v=210&s=ta_p_channeldown",
    "doubletop": "/screener?v=210&s=ta_p_doubletop",
    "multipletop": "/screener?v=210&s=ta_p_multipletop",
    "doublebottom": "/screener?v=210&s=ta_p_doublebottom",
    "multiplebottom": "/screener?v=210&s=ta_p_multiplebottom",
    "headandshoulders": "/screener?v=210&s=ta_p_headandshoulders",
    "headandshouldersinv": "/screener?v=210&s=ta_p_headandshouldersinv",
}
# Map a signal -> the screener token, so we can also build the table (v=111) variant.
SIGNAL_TOKENS = {name: p.split("s=")[1] for name, p in ROBOTS_ALLOWED_SIGNALS.items()}

mcp = FastMCP(
    "finviz",
    instructions=(
        "Read-only access to Finviz (US market data). Tools: quote (fundamentals snapshot, 83 "
        "fields), quote_batch (valuation/profitability/growth/balance comparison for a KNOWN "
        "ticker list — this is the replacement for screening), market_overview (breadth, futures, "
        "rates, headlines, calendar), signals_snapshot (homepage movers), news, insider_trades, "
        "economic_calendar, earnings_calendar, dividends_calendar, group_performance "
        "(sector/industry performance), robots_policy. NOTE: screener_signal and custom_screener "
        "do NOT work — finviz redirects /screener?* into a robots-disallowed path and renders "
        "results in JavaScript; they only return a diagnosis. Every request is re-checked against "
        "finviz.com/robots.txt, including the final URL after redirects. Data is delayed external "
        "data: label it 'partially verified' and never present it as investment advice."
    ),
)

# --------------------------------------------------------------------------- infra

_session = requests.Session()
_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
})
_rate_lock = threading.Lock()
_last_call = [0.0]
_cache: dict[str, tuple[float, Any]] = {}


class RobotsState:
    def __init__(self) -> None:
        self.rp: RobotFileParser | None = None
        self.raw: str = ""
        self.fetched_at: float = 0.0
        self.error: str | None = None

    def load(self, force: bool = False) -> None:
        if not force and self.rp is not None and (time.time() - self.fetched_at) < ROBOTS_TTL:
            return
        try:
            resp = _session.get(BASE + "/robots.txt", timeout=TIMEOUT)
            resp.raise_for_status()
            self.raw = resp.text
            rp = RobotFileParser()
            rp.parse(self.raw.splitlines())
            self.rp = rp
            self.fetched_at = time.time()
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _match(pattern: str, target: str) -> bool:
        """Robots pattern matching with '*' wildcard and '$' end-anchor (longest-match rule)."""
        anchored = pattern.endswith("$")
        pat = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pat)
        return bool(re.fullmatch(rx, target)) if anchored else bool(re.match(rx, target))

    def _rules_for(self) -> list[Any]:
        """Rule lines that apply to us: matching specific entries, else the '*' default entry.

        Two stdlib gotchas this works around (verified on Python 3.11):
        * ``RobotFileParser`` keeps ``User-agent: *`` rules in ``default_entry``, NOT in
          ``entries`` — reading only ``entries`` silently allows everything.
        * ``RuleLine.path`` is percent-quoted, so ``/screener?*`` is stored as
          ``/screener%3F%2A`` and must be unquoted before matching.
        """
        rules: list[Any] = []
        for entry in self.rp.entries:
            if entry.applies_to(ROBOTS_UA_TOKEN):
                rules.extend(entry.rulelines)
        if not rules and self.rp.default_entry is not None:
            rules.extend(self.rp.default_entry.rulelines)
        return rules

    def check(self, url: str) -> dict[str, Any]:
        self.load()
        if self.rp is None:
            return {
                "allowed": True,
                "robots_available": False,
                "reason": f"robots.txt unavailable ({self.error or 'unknown'}) — request allowed with warning",
            }
        path = unquote(url[len(BASE):] if url.startswith(BASE) else url)
        best_allow: tuple[str, int] | None = None   # (pattern, length)
        best_deny: tuple[str, int] | None = None
        for line in self._rules_for():
            pattern = unquote(line.path)
            if not self._match(pattern, path):
                continue
            cand = (pattern, len(pattern.rstrip("$")))
            if line.allowance:
                if best_allow is None or cand[1] > best_allow[1]:
                    best_allow = cand
            else:
                if best_deny is None or cand[1] > best_deny[1]:
                    best_deny = cand
        # robots semantics: longest match wins; on equal length, Allow wins.
        if best_deny and not (best_allow and best_allow[1] >= best_deny[1]):
            return {
                "allowed": False,
                "robots_available": True,
                "matched_rule": f"Disallow: {best_deny[0]}",
                "reason": (
                    f"robots.txt rule 'Disallow: {best_deny[0]}' matches {path}. Finviz Allow-lists "
                    f"only specific signal URLs; generic /screener?* queries are disallowed. Pass "
                    f"allow_noncompliant=True to override (your explicit call — it violates their "
                    f"published crawl policy)."
                ),
            }
        rule = f"Allow: {best_allow[0]}" if best_allow else "no matching rule (allowed by default)"
        return {"allowed": True, "robots_available": True, "matched_rule": rule,
                "reason": f"allowed — {rule}"}


_robots = RobotsState()


def _throttle() -> None:
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _get(
    path: str,
    params: dict[str, Any] | None = None,
    ttl: int | None = None,
    allow_noncompliant: bool = False,
    expect_json: bool = False,
) -> dict[str, Any]:
    qs = ("?" + urlencode(params)) if params else ""
    url = BASE + path + qs
    check = _robots.check(url)
    if not check["allowed"] and not (allow_noncompliant or ALLOW_NONCOMPLIANT_ENV):
        raise PermissionError(check["reason"])

    ttl = CACHE_TTL if ttl is None else ttl
    key = url
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        payload, age = hit[1], round(time.time() - hit[0], 1)
        return {"payload": payload, "url": url, "cache_age_s": age, "from_cache": True,
                "robots_compliant": check["allowed"]}

    last_err: Exception | None = None
    for attempt, backoff in enumerate((0.0, 1.5, 4.0)):
        if backoff:
            time.sleep(backoff)
        try:
            _throttle()
            resp = _session.get(url, timeout=TIMEOUT)
            if resp.status_code in (429, 403, 503):
                raise RuntimeError(f"HTTP {resp.status_code} (rate limited / blocked)")
            resp.raise_for_status()
            # A permitted URL can REDIRECT into a disallowed one (verified 2026-10-21: every
            # /screener?... — including the paths finviz Allow-lists — 302s to
            # /screener/stocks?..., which robots.txt Disallows). Checking only the requested URL
            # made that redirect invisible, so the final URL is re-checked here for every tool.
            final_url = getattr(resp, "url", url) or url
            if final_url.split("?")[0].rstrip("/") != url.split("?")[0].rstrip("/") or final_url != url:
                landed = _robots.check(final_url)
                if not landed["allowed"] and not (allow_noncompliant or ALLOW_NONCOMPLIANT_ENV):
                    raise PermissionError(
                        f"redirected to a robots-disallowed URL — {landed['reason']} "
                        f"(requested {url}, landed {final_url})")
            payload = resp.json() if expect_json else resp.text
            _cache[key] = (time.time(), payload)
            return {"payload": payload, "url": url, "cache_age_s": 0.0, "from_cache": False,
                    "robots_compliant": check["allowed"]}
        except PermissionError:
            raise  # policy decision, not a transient failure — never retry it
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"GET {url} failed after 3 attempts: {type(last_err).__name__}: {last_err}")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _txt(node: Any) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _meta(res: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "source_url": res["url"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from_cache": res["from_cache"],
        "cache_age_s": res["cache_age_s"],
        "robots_compliant": res["robots_compliant"],
        "data_quality": "partially verified (external, free tier, delayed)",
        "disclaimer": DISCLAIMER,
    }
    if extra:
        out.update(extra)
    return out


def _out(obj: dict[str, Any], res: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> str:
    if res is not None:
        obj = {**obj, "_meta": _meta(res, extra)}
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# ----------------------------------------------------------------------- parsers


def _parse_snapshot(soup: BeautifulSoup) -> dict[str, str]:
    """Finviz quote page: pair <div class=snapshot-td-label> with <div class=snapshot-td-content>."""
    out: dict[str, str] = {}
    for tr in soup.select("table.js-snapshot-table tr"):
        labels = tr.select(".snapshot-td-label")
        values = tr.select(".snapshot-td-content")
        for lab, val in zip(labels, values):
            key = _txt(lab)
            if key and key not in out:
                out[key] = _txt(val)
    return out


def _parse_screener_rows(soup: BeautifulSoup, limit: int = 0) -> tuple[list[str], list[dict[str, Any]]]:
    tables = soup.find_all("table")
    if not tables:
        return [], []
    best = max(tables, key=lambda t: len(t.select("tr.styled-row")))
    headers = [_txt(th) for th in best.select("thead th")]
    rows: list[dict[str, Any]] = []
    for tr in best.select("tr.styled-row"):
        tds = tr.find_all("td")
        if not tds:
            continue
        ticker = tr.get("data-boxover-ticker") or ""
        company = tr.get("data-boxover-company") or ""
        if not ticker:
            holder = tr.select_one("[data-boxover-ticker]")
            ticker = holder.get("data-boxover-ticker", "") if holder else ""
            company = company or (holder.get("data-boxover-company", "") if holder else "")
        if not ticker:
            for a in tr.find_all("a", href=True):
                m = re.search(r"stock\?t=([A-Za-z0-9\.\-]{1,8})", a["href"])
                if m:
                    ticker = m.group(1)
                    break
        if not ticker:
            for a in tr.find_all("a", href=True):
                if "company-ticker" in (a.get("class") or []):
                    ticker = _txt(a)
                    break
        cells = [_txt(td) for td in tds]
        if not ticker:
            ticker = cells[1] if len(cells) > 1 else (cells[0] if cells else "")
        item: dict[str, Any] = {
            "no": cells[0] if cells else "",
            "ticker": ticker,
            "company": company or (cells[2] if len(cells) > 2 else ""),
        }
        for idx in range(2, len(cells)):
            if idx == 2 and item["company"]:
                continue
            name = headers[idx] if idx < len(headers) and headers[idx] else f"col{idx}"
            key = name if name not in item else f"{name}_{idx}"
            item[key] = cells[idx]
        box = tr.select_one("[data-boxover-industry]")
        if box:
            item.setdefault("Industry", box.get("data-boxover-industry", ""))
            item.setdefault("Country", box.get("data-boxover-country", ""))
        rows.append(item)
        if limit and len(rows) >= limit:
            break
    return headers, rows


def _rows_from_json(html: str, limit: int = 0) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """Pull screener rows out of the JSON blob finviz embeds in every /screener page.

    Verified 2026-10-29: these views are server-rendered, just not as an HTML table — the rows
    ship inside ``<script type="application/json">`` at ``data.rows[]``, each row shaped
    ``{"text": {...}, "numbers": {...}}``. The HTML-only parser therefore reported
    "JavaScript-rendered, zero rows" for pages carrying the full result set (the first
    ``f=industry_shellcompanies`` request contained 328 companies in 20 rows). Same payload,
    better source: ``numbers`` arrives typed (floats), so no re-parsing of formatted strings.
    """
    m = re.search(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], [], {}
    try:
        blob = json.loads(m.group(1))
    except (ValueError, TypeError):
        return [], [], {}
    data = blob.get("data") if isinstance(blob, dict) else None
    if not isinstance(data, dict):
        return [], [], {}
    raw_rows = data.get("rows") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        return [], [], {}

    headers: list[str] = []
    for r in raw_rows:
        for src in ("text", "numbers"):
            block = r.get(src) if isinstance(r, dict) else None
            if isinstance(block, dict):
                for k in block:
                    if k not in headers and k != "order":
                        headers.append(k)
    rows: list[dict[str, Any]] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        item: dict[str, Any] = {}
        for src in ("text", "numbers"):
            block = r.get(src)
            if isinstance(block, dict):
                for k, v in block.items():
                    if k == "order":
                        continue
                    item[k] = v
        if not item:
            continue
        rows.append(item)
        if limit and len(rows) >= limit:
            break
    meta = {
        "total_count": data.get("totalCount"),
        "first_row": data.get("firstRow"),
        "page_size": data.get("pageSize"),
        "parsed_from": "embedded_json",
    }
    return headers, rows, meta


def _parse_kv_tables(soup: BeautifulSoup, must_contain: list[str]) -> list[dict[str, str]]:
    """Generic: for each row of a table whose text mentions any must_contain token."""
    found: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        if not any(tok in table.get_text(" ", strip=True) for tok in must_contain):
            continue
        for tr in table.find_all("tr"):
            cells = [_txt(td) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                found.append({"name": cells[0], "values": cells[1:]})
        if found:
            return found
    return found


def _parse_breadth(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pats = {
        "advancing_declining": r"Advancing\s*([\d\.]+%)?\s*\((\d+)\)\s*Declining\s*\((\d+)\)\s*([\d\.]+%)?",
        "new_high_low": r"New High\s*([\d\.]+%)?\s*\((\d+)\)\s*New Low\s*\((\d+)\)\s*([\d\.]+%)?",
        "above_below_sma50": r"Above\s*([\d\.]+%)?\s*\((\d+)\)\s*SMA50\s*Below\s*\((\d+)\)\s*([\d\.]+%)?",
        "above_below_sma200": r"Above\s*([\d\.]+%)?\s*\((\d+)\)\s*SMA200\s*Below\s*\((\d+)\)\s*([\d\.]+%)?",
    }
    for key, pat in pats.items():
        m = re.search(pat, text)
        if not m:
            continue
        a_pct, a_n, b_n, b_pct = m.groups()
        out[key] = {
            "first": {"pct": a_pct, "count": _int(a_n)},
            "second": {"pct": b_pct, "count": _int(b_n)},
        }
    return out


def _parse_market_quotes(text: str) -> dict[str, list[dict[str, str]]]:
    """Regex out the homepage Futures / Forex+Bonds+Crypto ticker strips.

    The homepage renders these as label + last + change + change% runs inside one table;
    flattening to text and matching label groups is far more stable than DOM traversal.
    """
    groups = {
        "futures": ["Crude Oil", "Natural Gas", "Gold", "Dow", "S&P 500", "Nasdaq 100", "Russell 2000"],
        "forex_bonds_crypto": ["EUR/USD", "USD/JPY", "GBP/USD", "BTC/USD",
                               "5-Year Treasury", "10-Year Treasury", "30-Year Treasury"],
    }
    out: dict[str, list[dict[str, str]]] = {}
    for key, labels in groups.items():
        rows: list[dict[str, str]] = []
        for label in labels:
            pat = re.escape(label) + r"\s+([\d,\.]+)\s+([-+]?[\d,\.]+)\s+([-+]?[\d\.]+%)"
            m = re.search(pat, text)
            if m:
                rows.append({"name": label, "last": m.group(1), "change": m.group(2),
                             "change_pct": m.group(3)})
        if rows:
            out[key] = rows
    return out


LABEL_TO_SIGNAL = {
    "Top Gainers": "topgainers", "Top Losers": "toplosers",
    "New High": "newhigh", "New Low": "newlow",
    "Overbought": "overbought", "Oversold": "oversold",
    "Most Active": "mostactive", "Most Volatile": "mostvolatile",
    "Unusual Volume": "unusualvolume",
    "Earnings Before": "earningsbefore", "Earnings After": "earningsafter",
    "Insider Buying": "insiderbuys", "Insider Selling": "insidersales",
    "Upgrades": "upgrades", "Downgrades": "downgrades", "Major News": "majornews",
    "TrendlineTL Supp.": "tlsupport", "TrendlineTL Resist.": "tlresistance",
    "Horizontal S/R": "horizontal",
    "Wedge Up": "wedgeup", "Wedge": "wedge", "Wedge Down": "wedgedown",
    "Channel Up": "channelup", "Channel": "channel", "Channel Down": "channeldown",
    "Double Top": "doubletop", "Multiple Top": "multipletop",
    "Double Bottom": "doublebottom", "Multiple Bottom": "multiplebottom",
    "Head&Shoulders": "headandshoulders", "Head&Shoulders Inv": "headandshouldersinv",
}


def _int(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return None


# ------------------------------------------------------------------------- tools


@mcp.tool()
def health() -> str:
    """Check Finviz reachability, robots.txt state, and the tool list."""
    report: dict[str, Any] = {"ok": False, "tools": [
        "quote", "market_overview", "signals_snapshot", "screener_signal", "custom_screener",
        "news", "insider_trades", "economic_calendar", "robots_policy",
    ]}
    try:
        res = _get("/", ttl=30)
        report["ok"] = True
        report["homepage_bytes"] = len(res["payload"])
        report["robots_compliant"] = res["robots_compliant"]
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    _robots.load()
    report["robots_available"] = _robots.rp is not None
    report["robots_fetched_at"] = (
        datetime.fromtimestamp(_robots.fetched_at, tz=timezone.utc).isoformat(timespec="seconds")
        if _robots.fetched_at else None
    )
    report["robots_error"] = _robots.error
    report["allow_noncompliant_env"] = ALLOW_NONCOMPLIANT_ENV
    report["rate_limit_s"] = MIN_INTERVAL
    report["cache_ttl_s"] = CACHE_TTL
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp.tool()
def robots_policy(path: str = "") -> str:
    """Show how finviz robots.txt treats a path (default: the screener endpoints).

    Args:
        path: Path with optional query, e.g. "/screener?v=111&s=ta_topgainers". Empty
              returns the policy map for the main endpoints this server uses.

    Returns:
        JSON with allowed flag, matched rule, and the curated allow/disallow map.
    """
    checks: list[dict[str, Any]] = []
    targets = [path] if path.strip() else [
        "/", "/stock?t=AAPL", "/news?t=AAPL", "/insidertrading", "/calendar/economic",
        "/api/calendar/economic?dateFrom=2026-01-01&dateTo=2026-01-02",
        "/screener?v=111&s=ta_topgainers", "/screener?v=111&f=idx_sp500",
        "/screener?v=340&s=ta_topgainers", "/export.ashx?v=111", "/api/v1/screener-export-csv",
    ]
    for target in targets:
        result = _robots.check(BASE + target)
        checks.append({"path": target, "allowed": result["allowed"], "reason": result["reason"]})
    return json.dumps({
        "robots_url": BASE + "/robots.txt",
        "robots_available": _robots.rp is not None,
        "checks": checks,
        "explicitly_allowed_signals": sorted(ROBOTS_ALLOWED_SIGNALS),
        "note": (
            "Finviz Allow-lists only a fixed set of signal URLs and Disallows generic /screener?*. "
            "Their Allow-ed URLs (view v=340/v=320/v=210) are JavaScript-rendered, so HTML parsing "
            "yields no rows; the server-rendered v=111 table view is the one that parses, and it is "
            "disallowed. Screener tools therefore require allow_noncompliant=True."
        ),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def quote(ticker: str, metrics: list[str] | None = None) -> str:
    """Fundamentals/technical snapshot for one US ticker (finviz /stock page).

    Args:
        ticker: US ticker symbol, e.g. "AAPL". Case-insensitive.
        metrics: Optional subset of metric labels to return (e.g. ["P/E", "RSI (14)"]).

    Returns:
        JSON: company/sector header fields, all snapshot metrics, plus provenance meta.
    """
    sym = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9\.\-]{1,10}", sym):
        return json.dumps({"ok": False, "error": f"invalid ticker: {ticker!r}"}, indent=2)
    try:
        res = _get("/stock", {"t": sym}, ttl=60)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    snapshot = _parse_snapshot(soup)
    if metrics:
        wanted = [m.strip().lower() for m in metrics]
        snapshot = {k: v for k, v in snapshot.items() if k.lower() in wanted}
    title = _txt(soup.select_one("h1")) or _txt(soup.select_one("title"))
    # Company name + sector/industry row appear near the quote header links.
    header: dict[str, str] = {}
    for a in soup.find_all("a", href=True)[:400]:
        href = a["href"]
        if "screener?v=111&f=sec_" in href and "sector" not in header:
            header["sector"] = _txt(a)
        elif "screener?v=111&f=ind_" in href and "industry" not in header:
            header["industry"] = _txt(a)
        elif "screener?v=111&f=geo_" in href and "country" not in header:
            header["country"] = _txt(a)
    if not snapshot:
        return json.dumps({"ok": False, "error": f"no snapshot metrics parsed for {sym}",
                           "_meta": _meta(res)}, indent=2)
    return _out({"ok": True, "ticker": sym, "title": title, "header": header, "metrics": snapshot}, res)


# Preset keyword-groups for quote_batch. Matching is by LABEL KEYWORD, not exact string, so a
# small label drift on Finviz does not silently empty a column.
_FUNDA_PRESETS = {
    "valuation": ("P/E", "P/S", "P/B", "P/C", "P/FCF", "PEG", "EV/", "Price/"),
    "profitability": ("EPS", "ROE", "ROA", "ROI", "Margin", "Sales"),
    "growth": ("Growth", "EPS this Y", "EPS next Y", "EPS next 5Y", "EPS Q/Q", "Sales Q/Q"),
    "balance": ("Debt", "Current Ratio", "Quick Ratio", "Book/sh", "Cash/sh"),
}


@mcp.tool()
def quote_batch(
    tickers: list[str],
    preset: str = "valuation",
    extra_metrics: list[str] | None = None,
    include_header: bool = True,
) -> str:
    """Fundamentals comparison for a KNOWN ticker list (1 request per ticker, >=1s apart).

    Why this exists: the Finviz screener page is not usable from here (see screener_signal —
    the URL redirects into a robots-disallowed path and the table is JavaScript-rendered), so
    discovery is out. Per-ticker snapshots are fine, and a comparison table is what fundamental
    analysis actually needs: give a watchlist, get valuation/profitability/growth/balance columns.

    Args:
        tickers: 1..40 US tickers, e.g. ["AAPL", "MSFT", "JPM"].
        preset: "valuation" | "profitability" | "growth" | "balance" | "all".
        extra_metrics: Extra exact metric labels to add (e.g. ["RSI (14)", "Target Price"]).
        include_header: Also return sector/industry/country per ticker.

    Returns:
        JSON: columns, rows (one per ticker), missing tickers, request count and elapsed seconds.
    """
    syms = [s.strip().upper() for s in (tickers or []) if s and s.strip()]
    if not syms:
        return json.dumps({"ok": False, "error": "tickers cannot be empty"}, indent=2)
    if len(syms) > 40:
        return json.dumps({"ok": False, "error": f"too many tickers ({len(syms)}); max 40 — "
                                                 "each one is a separate request"}, indent=2)
    preset = (preset or "valuation").strip().lower()
    if preset not in _FUNDA_PRESETS and preset != "all":
        return json.dumps({"ok": False, "error": f"unknown preset {preset!r}",
                           "valid": sorted(_FUNDA_PRESETS) + ["all"]}, indent=2)

    t0 = time.time()
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    got_headers: dict[str, dict[str, str]] = {}
    columns: list[str] = []
    for sym in syms:
        raw = quote(sym)  # reuses the same cached/throttled fetch path
        try:
            q = json.loads(raw)
        except Exception:  # noqa: BLE001
            missing.append(sym)
            continue
        if not q.get("ok"):
            missing.append(sym)
            continue
        metrics = q.get("metrics") or {}
        if preset == "all":
            keep = dict(metrics)
        else:
            kws = _FUNDA_PRESETS[preset]
            keep = {k: v for k, v in metrics.items() if any(kw.lower() in k.lower() for kw in kws)}
        if extra_metrics:
            for want in extra_metrics:
                for k, v in metrics.items():
                    if k.lower() == want.strip().lower():
                        keep[k] = v
        row: dict[str, Any] = {"ticker": sym}
        row.update(keep)
        rows.append(row)
        if include_header:
            got_headers[sym] = q.get("header") or {}
        for k in keep:
            if k not in columns:
                columns.append(k)

    return json.dumps({
        "ok": bool(rows),
        "preset": preset,
        "columns": ["ticker"] + columns,
        "rows": rows,
        "headers": got_headers if include_header else {},
        "missing_or_failed": missing,
        "request_count": len(syms),
        "elapsed_s": round(time.time() - t0, 1),
        "note": "External free-tier Finviz data (delayed, US only) — treat as partially verified, "
                "not investment advice.",
    }, indent=2)



@mcp.tool()
def signals_snapshot() -> str:
    """Homepage movers: every signal block Finviz server-renders on the front page.

    This is the robots-compliant way to get signal lists (top gainers/losers, new
    high/low, overbought/oversold, unusual volume, most active/volatile, earnings
    before/after, insider buys/sales, and TA pattern blocks).

    Returns:
        JSON: mapping of signal label -> rows (ticker, last, change_pct, volume).
    """
    try:
        res = _get("/", ttl=45)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    blocks: dict[str, list[dict[str, str]]] = {}
    for table in soup.find_all("table"):
        header_cells = [_txt(c) for c in table.find_all("th")]
        if not ("Ticker" in header_cells and any("Signal" in c for c in header_cells)):
            continue
        for tr in table.find_all("tr"):
            cells = [_txt(td) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            label = cells[-1]
            ticker = ""
            for a in tr.find_all("a", href=True):
                m = re.search(r"stock\?t=([A-Za-z0-9\.\-]{1,8})", a["href"])
                if m:
                    ticker = m.group(1)
                    break
            if not ticker or not label:
                continue
            plain = [c for c in cells if re.fullmatch(r"[-+]?[\d,\.]+", c)]
            pcts = [c for c in cells if re.fullmatch(r"[-+]?[\d,\.]+%", c)]
            vols = [c for c in cells if re.fullmatch(r"[\d,\.]+[KMB]", c)]
            ticker = re.sub(r"\s+", " ", ticker).strip().split(" ")[-1]
            row = {"ticker": ticker,
                   "last": plain[0] if plain else "",
                   "change_pct": pcts[0] if pcts else "",
                   "volume": vols[0] if vols else ""}
            blocks.setdefault(label, [])
            if row not in blocks[label]:
                blocks[label].append(row)
    # Keep only recognised signal blocks (mover/pattern tables), then dedupe tickers inside a
    # block. This drops promo tables ("Recent Quotes"), index heatmap captions, etc.
    cleaned: dict[str, list[dict[str, str]]] = {}
    dropped: list[str] = []
    for label, rows in blocks.items():
        signal = LABEL_TO_SIGNAL.get(label.strip())
        if not signal:
            dropped.append(label.strip())
            continue
        seen: set[str] = set()
        uniq: list[dict[str, str]] = []
        for row in rows:
            if not row["last"] or not row["change_pct"] or not row["ticker"]:
                continue
            if row["ticker"] in seen:
                continue
            seen.add(row["ticker"])
            uniq.append(row)
        if uniq:
            cleaned[signal] = uniq
    return _out({"ok": True, "signal_blocks": cleaned, "block_count": len(cleaned),
                 "dropped_non_signal_blocks": sorted(set(dropped))[:10]}, res)


@mcp.tool()
def market_overview(sections: list[str] | None = None) -> str:
    """Front-page dashboard: breadth, futures, forex/rates/crypto, headlines, calendar.

    Args:
        sections: Optional subset of ["breadth", "futures", "rates", "headlines", "calendar"].
                  Default: all.

    Returns:
        JSON dashboard assembled from the Finviz homepage (robots-compliant).
    """
    want = [s.strip().lower() for s in sections] if sections else ["breadth", "futures", "rates", "headlines", "calendar"]
    try:
        res = _get("/", ttl=45)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    text = _txt(soup.body or soup)
    data: dict[str, Any] = {"ok": True}
    if "breadth" in want:
        data["breadth"] = _parse_breadth(text)
    if "futures" in want or "rates" in want:
        quotes = _parse_market_quotes(text)
        if "futures" in want:
            data["futures"] = quotes.get("futures", [])
        if "rates" in want:
            data["forex_bonds_crypto"] = quotes.get("forex_bonds_crypto", [])
    if "headlines" in want:
        heads = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "finviz.com" not in href:
                t = _txt(a)
                if 25 < len(t) < 200:
                    heads.append({"title": t, "url": href})
        seen, uniq = set(), []
        for h in heads:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            uniq.append(h)
        data["headlines"] = uniq[:15]
    if "calendar" in want:
        try:
            cal = json.loads(economic_calendar())
            data["calendar_today"] = cal.get("events", [])[:12]
        except Exception as exc:  # noqa: BLE001
            data["calendar_error"] = f"{type(exc).__name__}: {exc}"
    return _out(data, res)


@mcp.tool()
def screener_signal(
    signal: str,
    page: int = 1,
    sort: str = "",
    limit: int = 20,
    allow_noncompliant: bool = False,
) -> str:
    """Rows for a named Finviz signal (top gainers, new highs, TA patterns, ...).

    The server-rendered table view of /screener is Disallowed by finviz robots.txt, so this
    tool refuses by default. Set allow_noncompliant=True to use it anyway — that is your
    explicit decision to ignore their crawl policy; the response is flagged accordingly.

    Args:
        signal: One of the names from robots_policy() (e.g. "topgainers", "newhigh",
                "unusualvolume", "headandshoulders").
        page: 1-based page (20 rows per page).
        sort: Finviz sort field, e.g. "-marketcap", "-change", "-volume". Empty = site default.
        limit: Max rows to return (<=20 per page).
        allow_noncompliant: Opt-in override for the robots.txt restriction.

    Returns:
        JSON with headers + rows + provenance/compliance flags.
    """
    name = (signal or "").strip().lower().replace("_", "").replace("-", "")
    token = SIGNAL_TOKENS.get(name)
    if not token:
        return json.dumps({
            "ok": False,
            "error": f"unknown signal {signal!r}",
            "valid_signals": sorted(SIGNAL_TOKENS),
        }, indent=2)
    params: dict[str, Any] = {"v": "340", "s": token}
    if sort.strip():
        params["o"] = sort.strip()
    if page and page > 1:
        params["r"] = (int(page) - 1) * 20 + 1
    try:
        res = _get("/screener", params, ttl=45, allow_noncompliant=allow_noncompliant)
    except PermissionError as exc:
        return json.dumps({"ok": False, "error": str(exc), "diagnosis": (
            "This signal's URL is not in finviz's Allow-list. Only the specific /screener?v=... "
            "signal URLs listed by robots_policy() are permitted; everything else falls under "
            "Disallow: /screener?*."),
            "use_instead": ["quote_batch (fundamentals for a known list)",
                            "signals_snapshot (homepage movers, server-rendered)",
                            "market_overview", "group_performance (sector/industry table)"]}, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    # JSON blob FIRST: its fields are typed and its tickers are explicit. The legacy HTML-table
    # parser stays as a fallback only — verified 2026-10-29 that on v=340&s=n_upgrades it picked
    # the cell text "Upgrade" as a ticker while the JSON blob held the real 8 rows.
    headers, rows, meta = _rows_from_json(res["payload"], limit=int(limit or 20))
    origin = "embedded_json" if rows else "none"
    if not rows:
        headers, rows = _parse_screener_rows(soup, limit=int(limit or 20))
        origin = "html_table" if rows else "none"
    if not rows:
        return _out({
            "ok": False,
            "error": "no rows in either the embedded JSON blob or the HTML table",
            "hint": "The page fetched but carried no result set; retry or check the signal name.",
        }, res)
    return _out({"ok": True, "signal": name, "page": page, "headers": headers, "rows": rows,
                 "row_count": len(rows), "parsed_from": origin}, res, meta)


@mcp.tool()
def custom_screener(
    filters: str,
    view: str = "111",
    sort: str = "-marketcap",
    page: int = 1,
    limit: int = 20,
    allow_noncompliant: bool = False,
) -> str:
    """Filtered screener query. Disallowed by finviz robots.txt -> requires opt-in.

    Args:
        filters: Finviz filter string, e.g. "idx_sp500,fa_pe_u20,ta_sma200_pa"
                 (index/sector + fundamentals + technicals, comma-separated).
        view: Finviz view id: "111" (Overview), "121" (Valuation), "161" (Financial),
              "171" (Ownership), "111" default is the only one verified to server-render.
        sort: Sort field, e.g. "-marketcap", "-pe", "-change".
        page: 1-based page (20 rows per page).
        limit: Max rows returned.
        allow_noncompliant: Opt-in override for the robots.txt restriction.

    Returns:
        JSON with headers + rows + provenance/compliance flags.
    """
    flt = (filters or "").strip()
    if not flt:
        return json.dumps({"ok": False, "error": "filters cannot be empty"}, indent=2)
    params: dict[str, Any] = {"v": str(view or "111").strip(), "f": flt}
    if sort.strip():
        params["o"] = sort.strip()
    if page and page > 1:
        params["r"] = (int(page) - 1) * 20 + 1
    try:
        res = _get("/screener", params, ttl=45, allow_noncompliant=allow_noncompliant)
    except PermissionError as exc:
        return json.dumps({"ok": False, "error": str(exc), "diagnosis": (
            "Custom screening needs /screener?f=..., which robots.txt Disallows for generic "
            "clients and which 302-redirects into /screener/stocks?* (also Disallowed). The "
            "redirect is followed and re-checked, so the override is required end-to-end."),
            "use_instead": ["quote_batch (fundamentals for a known ticker list)",
                            "group_performance (sector/industry valuation context)",
                            "screener_signal (the Allow-listed signal URLs)"]}, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    headers, rows, meta = _rows_from_json(res["payload"], limit=int(limit or 20))
    origin = "embedded_json" if rows else "none"
    if not rows:
        headers, rows = _parse_screener_rows(soup, limit=int(limit or 20))
        origin = "html_table" if rows else "none"
    if not rows:
        return _out({"ok": False,
                     "error": "no rows in either the embedded JSON blob or the HTML table"}, res)
    return _out({"ok": True, "filters": flt, "view": view, "page": page, "headers": headers,
                 "rows": rows, "row_count": len(rows), "parsed_from": origin}, res, meta)


@mcp.tool()
def news(ticker: str = "", limit: int = 25) -> str:
    """Latest headlines from the Finviz news page (all markets, or one ticker).

    Args:
        ticker: Optional US ticker to focus the news feed on (e.g. "NVDA").
        limit: Max headlines returned.

    Returns:
        JSON list of {title, source, url}.
    """
    params = {"t": ticker.strip().upper()} if ticker.strip() else None
    try:
        res = _get("/news", params, ttl=90)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http") or "finviz.com" in href:
            continue
        title = _txt(a)
        if len(title) < 25 or href in seen:
            continue
        seen.add(href)
        src = re.sub(r"^www\.", "", href.split("/")[2]) if "//" in href else ""
        items.append({"title": title, "source": src, "url": href})
        if len(items) >= max(1, int(limit)):
            break
    return _out({"ok": True, "ticker": ticker.upper() or None, "headlines": items,
                 "count": len(items)}, res)


@mcp.tool()
def insider_trades(ticker: str = "", transaction: str = "", limit: int = 50) -> str:
    """Latest insider transactions from the Finviz insider page.

    Args:
        ticker: Optional ticker filter applied client-side over the latest rows.
        transaction: Optional filter, e.g. "buy" or "sale".
        limit: Max rows returned.

    Returns:
        JSON rows: ticker, owner, relationship, date, transaction, cost, shares, value.
    """
    try:
        res = _get("/insidertrading", None, ttl=120)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    tables = soup.find_all("table")
    if not tables:
        return _out({"ok": False, "error": "no tables found"}, res)
    best = max(tables, key=lambda t: len(t.find_all("tr")))
    headers = [_txt(c) for c in best.find_all("th")]
    rows: list[dict[str, str]] = []
    want_t = ticker.strip().upper()
    want_tx = transaction.strip().lower()
    for tr in best.find_all("tr"):
        cells = [_txt(td) for td in tr.find_all("td")]
        if len(cells) < 8 or cells[0].lower().startswith("ticker"):
            continue
        if want_t and cells[0].split()[-1].upper() != want_t:
            continue
        if want_tx and want_tx not in cells[4].lower():
            continue
        rows.append({
            "ticker": cells[0].split()[-1], "owner": cells[1], "relationship": cells[2],
            "date": cells[3], "transaction": cells[4], "cost": cells[5], "shares": cells[6],
            "value_usd": cells[7],
        })
        if len(rows) >= max(1, int(limit)):
            break
    return _out({"ok": True, "headers": headers, "rows": rows, "row_count": len(rows),
                 "filters": {"ticker": want_t or None, "transaction": want_tx or None}}, res)


@mcp.tool()
def economic_calendar(date_from: str = "", date_to: str = "", min_importance: int = 0) -> str:
    """Economic calendar (JSON API, robots-compliant): events, actual/forecast/previous.

    Args:
        date_from: ISO date (YYYY-MM-DD). Default: today (UTC).
        date_to: ISO date. Default: date_from + 7 days.
        min_importance: 1=low, 2=medium, 3=high. Filters out lower-importance events.

    Returns:
        JSON list of events with category, importance, actual/forecast/previous.
    """
    start = date_from.strip() or date.today().isoformat()
    if date_to.strip():
        end = date_to.strip()
    else:
        try:
            end = (date.fromisoformat(start) + timedelta(days=7)).isoformat()
        except ValueError:
            end = start
    try:
        res = _get("/api/calendar/economic", {"dateFrom": start, "dateTo": end}, ttl=300, expect_json=True)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    raw = res["payload"]
    events = []
    dropped_outside = 0
    for ev in raw if isinstance(raw, list) else raw.get("events", []):
        if int(ev.get("importance") or 0) < int(min_importance or 0):
            continue
        # Finviz's calendar endpoint returns a WIDER set than requested (verified 2026-10-21:
        # asked 2026-10-22..29, got 31 of 52 events dated before 2026-10-22). Report a window
        # and return events outside it is a lie, so enforce the window here and say how many
        # rows were dropped.
        ev_day = str(ev.get("date") or "")[:10]
        if ev_day and (ev_day < start or ev_day > end):
            dropped_outside += 1
            continue
        events.append({
            "date": ev.get("date"), "reference": ev.get("reference"),
            "event": ev.get("event"), "category": ev.get("category"),
            "importance": ev.get("importance"), "actual": ev.get("actual"),
            "forecast": ev.get("forecast"), "previous": ev.get("previous"),
        })
    return _out({"ok": True, "date_from": start, "date_to": end, "events": events,
                 "event_count": len(events),
                 "window_enforced": True,
                 "events_dropped_outside_window": dropped_outside}, res)


# ------------------------------------------- kalender laba/dividen & performa grup
# Tiga sumber BARU (dipetakan 2026-09-14 dari halaman yang belum dipakai server ini):
#   /api/calendar/earnings   JSON internal, 16 field termasuk EPS aktual vs estimasi + reaksi harga
#   /api/calendar/dividends  JSON internal: exdate, ordinary, special, yield
#   /groups.ashx?g=sector|industry  tabel server-rendered: 11 sektor / 148 industri
# CATATAN TERVERIFIKASI: `page` BEKERJA, tetapi `pageSize`/`limit`/`perPage` DIABAIKAN tanpa
# error (selalu 50 baris). Karena itu jumlah halaman dilaporkan balik, bukan diasumsikan.
# Yang TIDAK bisa diambil: /map.ashx (data dimuat lewat bundle JS, tak ada di HTML) dan
# /api/quote (rutenya ada tapi selalu 400 "invalid quote query" untuk semua format yang dicoba).


def _calendar_window(date_from: str, date_to: str, days: int = 7) -> tuple[str, str]:
    start = (date_from or "").strip() or date.today().isoformat()
    end = (date_to or "").strip()
    if not end:
        try:
            end = (date.fromisoformat(start) + timedelta(days=days)).isoformat()
        except ValueError:
            end = start
    return start, end


def _calendar_items(res: dict[str, Any], limit: int, page: int) -> tuple[list[dict], dict]:
    raw = res["payload"] if isinstance(res["payload"], dict) else {}
    items = raw.get("items") or []
    meta = {"page": raw.get("page"), "page_size": raw.get("pageSize"),
            "total_items": raw.get("totalItemsCount"), "total_pages": raw.get("totalPages"),
            "requested_page": page, "returned": min(len(items), limit),
            "page_size_is_fixed": True}
    return items[: max(0, int(limit or 50))], meta


def _calendar_collect(path: str, base: dict[str, Any], page: int, limit: int,
                      max_pages: int = 3) -> tuple[list[dict], dict, dict]:
    """Ambil beberapa halaman sampai cukup bahan untuk difilter.

    Filter (ticker/yield/market cap) dijalankan DI SISI KITA, sedangkan `pageSize` Finviz tetap
    50 dan tidak bisa diubah — jadi satu halaman bisa tidak memuat baris yang lolos filter.
    Ditemukan oleh Eko saat memakai tool ini (laporan: "filter applied per page").
    Maksimum `max_pages` halaman supaya tidak membanjiri situs.
    """
    p = max(1, int(page or 1))
    raw_needed = max(50, int(limit or 25) * 3)
    collected: list[dict] = []
    counts: dict = {}
    res: dict = {}
    pages_fetched = 0
    while pages_fetched < max_pages and len(collected) < raw_needed:
        params = dict(base)
        if p > 1:
            params["page"] = p
        res = _get(path, params, ttl=300, expect_json=True)
        items, counts = _calendar_items(res, 50, p)
        collected.extend(items)
        pages_fetched += 1
        total_pages = int(counts.get("total_pages") or 1)
        if p >= total_pages:
            break
        p += 1
    counts["pages_fetched"] = pages_fetched
    counts["raw_items_considered"] = len(collected)
    return collected, counts, res


@mcp.tool()
def earnings_calendar(
    date_from: str = "",
    date_to: str = "",
    ticker: str = "",
    min_market_cap: float = 0.0,
    limit: int = 25,
    page: int = 1,
) -> str:
    """Jadwal rilis laba (earnings) — API JSON internal Finviz, robots-compliant, tanpa kunci.

    Berisi EPS aktual vs estimasi, kejutan (surprise), estimasi penjualan, dan reaksi harga
    sehari setelah rilis pada emiten yang sudah melapor.

    Args:
        date_from / date_to: ISO YYYY-MM-DD. Kosong = hari ini s/d +7 hari.
        ticker: saring satu emiten (mis. "AAPL"); kosong = semua.
        min_market_cap: saring market cap minimum (satuan Finviz: juta USD).
        limit: maksimum baris dikembalikan (maksimum server 50 per halaman).
        page: halaman (1-based). `pageSize` TIDAK bisa diubah — selalu 50.

    Returns:
        JSON: items + penghitung halaman + provenance/compliance.
    """
    start, end = _calendar_window(date_from, date_to)
    try:
        items, counts, res = _calendar_collect("/api/calendar/earnings", {"dateFrom": start, "dateTo": end},
                                               int(page or 1), int(limit or 25))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    want = (ticker or "").strip().upper()
    mind = float(min_market_cap or 0)
    rows = []
    for it in items:
        if want and str(it.get("ticker", "")).upper() != want:
            continue
        if mind and float(it.get("marketCap") or 0) < mind:
            continue
        rows.append({
            "earnings_date": it.get("earningsDate"),
            "date_is_estimate": it.get("isEarningDateEstimate"),
            "ticker": it.get("ticker"), "company": it.get("company"),
            "market_cap_musd": it.get("marketCap"),
            "eps_estimate": it.get("epsEstimate"), "eps_actual": it.get("epsActual"),
            "eps_surprise": it.get("epsSurprise"),
            "eps_reported_actual": it.get("epsReportedActual"),
            "eps_reported_surprise": it.get("epsReportedSurprise"),
            "sales_estimate": it.get("salesEstimate"), "sales_actual": it.get("salesActual"),
            "sales_surprise": it.get("salesSurprise"),
            "one_day_price_reaction": it.get("oneDayPriceReaction"),
        })
    rows = rows[: max(1, int(limit or 25))]
    return _out({"ok": True, "date_from": start, "date_to": end, "rows": rows,
                 "row_count": len(rows), "counts": counts,
                 "notes": ["pageSize Finviz tetap 50 dan tidak bisa diubah; tool ini otomatis "
                           "menggabungkan sampai 3 halaman agar filter punya cukup bahan "
                           "(lihat counts.pages_fetched / raw_items_considered).",
                           "eps_actual kosong berarti emiten belum melapor pada saat pengambilan."]}, res)


@mcp.tool()
def dividends_calendar(
    date_from: str = "",
    date_to: str = "",
    ticker: str = "",
    min_yield: float = 0.0,
    limit: int = 25,
    page: int = 1,
) -> str:
    """Jadwal ex-dividend — API JSON internal Finviz, robots-compliant, tanpa kunci.

    Args:
        date_from / date_to: ISO YYYY-MM-DD. Kosong = hari ini s/d +7 hari.
        ticker: saring satu emiten; kosong = semua.
        min_yield: saring dividend yield minimum (persen, mis. 5 untuk >= 5%).
        limit: maksimum baris (halaman Finviz tetap 50).
        page: halaman (1-based).
    """
    start, end = _calendar_window(date_from, date_to)
    try:
        items, counts, res = _calendar_collect("/api/calendar/dividends", {"dateFrom": start, "dateTo": end},
                                               int(page or 1), int(limit or 25))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    want = (ticker or "").strip().upper()
    miny = float(min_yield or 0)
    rows = []
    for it in items:
        if want and str(it.get("ticker", "")).upper() != want:
            continue
        if miny and float(it.get("yield") or 0) < miny:
            continue
        rows.append({
            "ticker": it.get("ticker"), "company": it.get("company"),
            "ex_date": it.get("exdate"), "ordinary": it.get("ordinary"),
            "special": it.get("special"), "yield_pct": it.get("yield"),
        })
    rows = rows[: max(1, int(limit or 25))]
    return _out({"ok": True, "date_from": start, "date_to": end, "rows": rows,
                 "row_count": len(rows), "counts": counts}, res)


@mcp.tool()
def group_performance(group: str = "sector", limit: int = 20, sort: str = "") -> str:
    """Performa per SEKTOR atau INDUSTRI (tabel server-rendered /groups.ashx) — tanpa kunci.

    Args:
        group: "sector" (11 baris) atau "industry" (148 baris).
        limit: maksimum baris dikembalikan.
        sort: nama kolom untuk pengurutan lokal, mis. "market_cap", "dividend", "pe",
              "change_pct", "volume", "stocks". Kosong = urutan situs.

    Returns:
        JSON: kolom + baris (stocks, market cap, dividend, P/E, fwd P/E, PEG, debt, float short,
        rekomendasi, change %, volume).
    """
    g = (group or "sector").strip().lower()
    if g not in ("sector", "industry"):
        return json.dumps({"ok": False, "error": f"group tidak dikenal: {group!r}",
                           "valid": ["sector", "industry"]}, indent=2)
    try:
        res = _get("/groups.ashx", {"g": g, "v": "110"}, ttl=300)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2)
    soup = _soup(res["payload"])
    # Semua baris data memakai kelas 'styled-row' dan konsisten 14 sel.
    header: list[str] = []
    rows: list[dict[str, Any]] = []
    for tr in soup.select("tr.styled-row"):
        cells = [_txt(td) for td in tr.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        if not header and any(c.lower() in ("name", "no.") for c in cells[:2]):
            header = cells
            continue
        if cells[0].lower().startswith("no"):
            header = cells
            continue
        rows.append(cells)
    cols = header or ["No.", "Name", "Stocks", "Market Cap", "Dividend", "P/E", "Fwd P/E", "PEG",
                      "LTDebt/Eq", "Debt/Eq", "Float Short", "Recom", "Change %", "Volume"]
    keys = ["no", "name", "stocks", "market_cap", "dividend", "pe", "fwd_pe", "peg",
            "lt_debt_eq", "debt_eq", "float_short", "recom", "change_pct", "volume"]
    out_rows = []
    for r in rows:
        rec = {k: (r[i] if i < len(r) else None) for i, k in enumerate(keys)}
        out_rows.append(rec)
    srt = (sort or "").strip()
    if srt:
        if srt not in keys:
            return json.dumps({"ok": False, "error": f"kolom sort tidak dikenal: {sort!r}",
                               "valid": keys}, indent=2)

        def num(v: Any) -> float:
            s = str(v or "").replace("%", "").replace(",", "")
            mult = 1.0
            if s.endswith("B"):
                mult, s = 1e3, s[:-1]
            elif s.endswith("M"):
                mult, s = 1.0, s[:-1]
            if s.endswith("K"):
                mult, s = 1e-3, s[:-1]
            try:
                return float(s) * mult
            except ValueError:
                return float("-inf")

        out_rows.sort(key=lambda d: num(d.get(srt)), reverse=True)
    out_rows = out_rows[: max(1, int(limit or 20))]
    return _out({"ok": True, "group": g, "columns": keys, "rows": out_rows,
                 "row_count": len(out_rows)}, res)


if __name__ == "__main__":
    mcp.run()
