#!/usr/bin/env python3
"""Regresi LENGKAP 10 tool lama finviz pasca-penambahan 3 tool baru."""
import asyncio
import json
import os
import pathlib
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import sys

HERE = pathlib.Path(__file__).resolve().parent

PY = os.environ.get("FINVIZ_PY", sys.executable)
SRV = os.environ.get("FINVIZ_SERVER", str(HERE / "finviz_mcp_server.py"))


def load_env(p=None):
    """Env untuk server. File opsional: FINVIZ_ENV_FILE, atau ~/.hermes/profiles/<p>/.env.

    Kalau tidak ada file, pakai os.environ apa adanya supaya skrip tetap jalan di mesin
    yang tidak memakai Hermes.
    """
    env = dict(os.environ)
    cand = pathlib.Path(os.environ.get("FINVIZ_ENV_FILE") or (
        Path.home() / ".hermes" / "profiles" / p / ".env" if p else ""))
    if cand and cand.is_file():
        for line in cand.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


CASES = [
    ("health", {}),
    ("robots_policy", {}),
    ("quote", {"ticker": "AAPL"}),
    ("signals_snapshot", {}),
    ("market_overview", {}),
    ("news", {"limit": 3}),
    ("insider_trades", {"limit": 3}),
    # --- insider_trades: server-side filtering (2026-10-30). The old client-side filter
    # returned 0 rows for NVDA because it only scanned the 200-row global page.
    ("insider_trades", {"ticker": "NVDA", "limit": 5}),
    ("insider_trades", {"ticker": "NVDA", "transaction": "buy", "limit": 5}),
    ("insider_trades", {"ticker": "BRK.B", "limit": 3}),
    ("insider_trades", {"min_value": 5_000_000, "limit": 3}),
    ("insider_trades", {"owner_type": "10%_owner", "limit": 3}),
    ("insider_trades", {"sort": "value", "limit": 3}),
    ("insider_trades", {"ticker": "AAPL,MSFT"}),            # harus MENOLAK (1 ticker/call)
    ("insider_trades", {"transaction": "buyback"}),         # harus MENOLAK (nilai tak dikenal)
    ("economic_calendar", {"min_importance": 1}),
    ("screener_signal", {"signal": "topgainers", "limit": 5}),      # harus LOLOS (URL allow-list, v=340)
    ("custom_screener", {"filters": "idx_sp500", "limit": 5}),      # harus MENOLAK (butuh opt-in)
    ("custom_screener", {"filters": "industry_shellcompanies", "limit": 5,
                         "allow_noncompliant": True}),              # harus LOLOS dengan opt-in
    # --- 6 tool yang dulu tidak pernah diuji regresi (ditemukan 2026-10-30 oleh catatan eko) ---
    ("quote_batch", {"tickers": ["NVDA"], "preset": "valuation"}),  # Market Cap wajib ada
    ("screener_filters", {"search": "shell", "limit": 5}),          # katalog, tidak boleh menolak diri sendiri
    ("screener_all", {"filters": "industry_shellcompanies", "max_pages": 2,
                      "allow_noncompliant": True}),                 # pagination harus berhenti
    ("earnings_calendar", {}),
    ("dividends_calendar", {}),
    ("group_performance", {"kind": "sector"}),
]


def expects_reject(name: str, args: dict) -> str:
    """Kasus yang JUSTRU HARUS ditolak. Mengembalikan substring error yang diharapkan, atau ""."""
    if name == "custom_screener" and not args.get("allow_noncompliant"):
        return "robots"
    if name == "insider_trades":
        if "," in str(args.get("ticker", "")):
            return "one ticker per call"
        tx = str(args.get("transaction", "")).strip().lower()
        if tx and tx not in ("all", "buy", "sale", "option"):
            return "unknown transaction"
    return ""


def invariant_failure(name: str, args: dict, d: dict) -> str:
    """Kontrak yang harus dipegang hasil, bukan cuma ok=True. Kembalikan pesan bila dilanggar."""
    if name != "insider_trades" or d.get("ok") is not True:
        return ""
    rows = d.get("rows") or []
    want_t = str(args.get("ticker", "")).strip().upper().replace(".", "-")
    if want_t and any(r.get("ticker") != want_t for r in rows):
        return f"row ticker != {want_t}"
    if want_t in ("NVDA", "AAPL", "BRK-B") and not rows:
        return f"{want_t} returned 0 rows — server-side filter regressed"
    if args.get("transaction") == "buy" and any("buy" not in r.get("transaction", "").lower() for r in rows):
        return "non-buy row in buy result"
    mv = int(args.get("min_value") or 0)
    if mv and any((r.get("value_usd_num") or 0) < mv for r in rows):
        return f"row below min_value {mv}"
    if args.get("owner_type") == "10%_owner" and any(r.get("relationship") != "10% Owner" for r in rows):
        return "non-10%-owner row"
    if "coverage" not in d:
        return "coverage block missing"
    return ""


async def main():
    p = StdioServerParameters(command=PY, args=[SRV], env=load_env())
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print(f"TOOL terdaftar: {len(tools)} -> {', '.join(tools)}\n")
            lolos = gagal = netral = 0
            for name, args in CASES:
                try:
                    d = json.loads((await s.call_tool(name, args)).content[0].text)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {name:<20} EKSEPSI {type(exc).__name__}: {exc}")
                    gagal += 1
                    continue
                ok = d.get("ok")
                detail = ""
                if "event_count" in d:
                    detail = f"events={d['event_count']}"
                elif "rows" in d:
                    cov = d.get("coverage") or {}
                    detail = f"rows={len(d['rows'])}"
                    if cov:
                        detail += f" page={cov.get('page_rows_total')} match={cov.get('rows_matching_filters')}"
                elif "headlines" in d:
                    detail = f"headlines={len(d['headlines'])}"
                elif name == "quote":
                    # quote() returns its fundamentals under "metrics" (older key was "snapshot")
                    metrics = d.get("metrics") or d.get("fundamentals") or d.get("snapshot") or {}
                    detail = f"metrics={len(metrics)}"
                elif name == "health":
                    detail = f"reachable={d.get('reachable') or d.get('ok')}"
                elif name == "robots_policy":
                    detail = f"robots_url={'ada' if d.get('robots_url') else '-'}"
                if ok is True:
                    bad = invariant_failure(name, args, d)
                    if bad:
                        gagal += 1
                        print(f"  {name:<20} ok=True  {detail} <- PELANGGARAN KONTRAK: {bad}")
                    else:
                        lolos += 1
                        print(f"  {name:<20} ok=True  {detail}")
                elif ok is False:
                    # Penolakan yang BENAR didefinisikan di expects_reject(), bukan dengan
                    # menerima semua ok=False (itu akan menyembunyikan regresi baru).
                    err = str(d.get("error", ""))
                    want = expects_reject(name, args)
                    benar = bool(want) and want.lower() in err.lower()
                    print(f"  {name:<20} ok=False {err[:60]} {'<- memang seharusnya begitu' if benar else '<- PERIKSA'}")
                    if benar:
                        netral += 1
                    else:
                        gagal += 1
                else:
                    print(f"  {name:<20} tanpa kunci 'ok': {list(d)[:5]}")
                    netral += 1
            print(f"\n  lolos={lolos} ditolak-sesuai-desain={netral} eksepsi={gagal} (dari {len(CASES)} tool lama)")


if __name__ == "__main__":
    asyncio.run(main())
