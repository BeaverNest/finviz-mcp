#!/usr/bin/env python3
"""Uji 3 tool BARU finviz + regresi tool lama."""
import asyncio
import json
import os
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = pathlib.Path(__file__).resolve().parent

PY = os.environ.get("FINVIZ_PY", sys.executable)
SRV = os.environ.get("FINVIZ_SERVER", str(HERE / "finviz_mcp_server.py"))
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "eko"


def load_env(p=None):
    """Env untuk server. File opsional: FINVIZ_ENV_FILE, atau ~/.hermes/profiles/<p>/.env.

    Kalau tidak ada file, pakai os.environ apa adanya supaya skrip tetap jalan di mesin
    yang tidak memakai Hermes.
    """
    env = dict(os.environ)
    cand = pathlib.Path(os.environ.get("FINVIZ_ENV_FILE") or (
        pathlib.Path.home() / ".hermes" / "profiles" / p / ".env" if p else ""))
    if cand and cand.is_file():
        for line in cand.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def brief(label, d, keys=()):
    ok = d.get("ok")
    extra = " | " + " ".join(f"{k}={d.get(k)}" for k in keys if k in d) if keys else ""
    print(f"  {label}: ok={ok}{extra}")
    return d


async def main():
    p = StdioServerParameters(command=PY, args=[SRV], env=load_env(PROFILE))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            print(f"TOOL ({len(tools)}): {', '.join(tools)}\n")

            print("--- BARU: earnings_calendar (jendela minggu ini)")
            d = json.loads((await s.call_tool("earnings_calendar", {"limit": 5})).content[0].text)
            brief("earnings", d, ("date_from", "date_to", "row_count"))
            print(f"     counts={d.get('counts')}")
            for row in (d.get("rows") or [])[:4]:
                print(f"     {row['earnings_date'][:10]} {row['ticker']:<6} {str(row['company'])[:34]:<36} "
                      f"cap={row['market_cap_musd']} epsEst={row['eps_estimate']} epsAct={row['eps_actual']}")

            print("\n--- BARU: earnings_calendar dengan EPS aktual (jendela yang sudah lewat)")
            d = json.loads((await s.call_tool("earnings_calendar",
                                              {"date_from": "2026-09-08", "date_to": "2026-09-12",
                                               "limit": 50})).content[0].text)
            lap = [r for r in (d.get("rows") or []) if r.get("eps_actual") is not None]
            print(f"     row_count={d.get('row_count')} | sudah melapor (eps_actual terisi): {len(lap)}")
            for row in lap[:3]:
                print(f"     {row['ticker']:<6} epsEst={row['eps_estimate']} epsAct={row['eps_actual']} "
                      f"surprise={row['eps_surprise']} reaksi={row['one_day_price_reaction']}")

            print("\n--- BARU: dividends_calendar")
            d = json.loads((await s.call_tool("dividends_calendar", {"limit": 5})).content[0].text)
            brief("dividends", d, ("row_count",))
            for row in (d.get("rows") or [])[:4]:
                print(f"     {row['ex_date']} {row['ticker']:<6} {str(row['company'])[:36]:<38} "
                      f"ord={row['ordinary']} yield={round(row['yield_pct'], 2) if row['yield_pct'] else None}%")

            print("\n--- BARU: group_performance(group='sector')")
            d = json.loads((await s.call_tool("group_performance", {"group": "sector"})).content[0].text)
            brief("sector", d, ("row_count",))
            for row in (d.get("rows") or [])[:4]:
                print(f"     {row['name']:<22} stocks={row['stocks']:<5} cap={row['market_cap']:<10} "
                      f"div={row['dividend']:<7} pe={row['pe']:<7} chg={row['change_pct']}")

            print("\n--- BARU: group_performance(group='industry', sort='change_pct', limit=5)")
            d = json.loads((await s.call_tool("group_performance",
                                              {"group": "industry", "sort": "change_pct",
                                               "limit": 5})).content[0].text)
            brief("industry", d, ("row_count",))
            for row in (d.get("rows") or []):
                print(f"     {row['name']:<28} chg={row['change_pct']:<8} stocks={row['stocks']}")

            print("\n--- KASUS ERROR tool baru")
            d = json.loads((await s.call_tool("group_performance", {"group": "ngawur"})).content[0].text)
            print(f"  group salah : ok={d.get('ok')} | {str(d.get('error'))[:70]}")
            d = json.loads((await s.call_tool("group_performance", {"sort": "ngawur"})).content[0].text)
            print(f"  sort salah  : ok={d.get('ok')} | {str(d.get('error'))[:70]}")

            print("\n--- REGRESI tool lama (harus tetap jalan)")
            for tool, args in [("economic_calendar", {}), ("market_overview", {}),
                               ("quote", {"ticker": "AAPL"}), ("news", {"limit": 3}),
                               ("insider_trades", {"limit": 3})]:
                d = json.loads((await s.call_tool(tool, args)).content[0].text)
                keys = [k for k in ("event_count", "row_count", "headlines") if k in d]
                print(f"  {tool:<20} ok={d.get('ok')} {'| ' + str({k: (len(d[k]) if isinstance(d[k], list) else d[k]) for k in keys}) if keys else ''}")


if __name__ == "__main__":
    asyncio.run(main())
