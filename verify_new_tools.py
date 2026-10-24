#!/usr/bin/env python3
"""Bukti rinci dua kalender baru + pemeriksaan isi (bukan sekadar ok=True)."""
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


async def main():
    p = StdioServerParameters(command=PY, args=[SRV], env=load_env())
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(f"TOOL: {len((await s.list_tools()).tools)}")

            print("\n=== earnings_calendar: jendela minggu ini (default) ===")
            d = json.loads((await s.call_tool("earnings_calendar", {"limit": 6})).content[0].text)
            print(f"  ok={d['ok']} {d['date_from']} → {d['date_to']} | rows={d['row_count']} | counts={d['counts']}")
            for x in d["rows"]:
                print(f"   {x['earnings_date'][:16]} {x['ticker']:<6} cap=${x['market_cap_musd']}M "
                      f"epsEst={x['eps_estimate']} est?={x['date_is_estimate']}")

            print("\n=== earnings_calendar: minggu lalu (ada EPS aktual?) ===")
            d = json.loads((await s.call_tool("earnings_calendar",
                                              {"date_from": "2026-09-08", "date_to": "2026-09-11",
                                               "limit": 50})).content[0].text)
            rep = [x for x in d["rows"] if x.get("eps_actual") is not None]
            print(f"  rows={d['row_count']} | eps_actual terisi: {len(rep)} | counts={d['counts']}")
            for x in rep[:5]:
                print(f"   {x['ticker']:<6} est={x['eps_estimate']} aktual={x['eps_actual']} "
                      f"surprise={x['eps_surprise']} reaksi_harga={x['one_day_price_reaction']}")

            print("\n=== dividends_calendar ===")
            d = json.loads((await s.call_tool("dividends_calendar", {"limit": 6})).content[0].text)
            print(f"  ok={d['ok']} rows={d['row_count']} counts={d['counts']}")
            for x in d["rows"]:
                y = f"{x['yield_pct']:.2f}%" if x.get("yield_pct") else "-"
                print(f"   {x['ex_date']} {x['ticker']:<6} {str(x['company'])[:32]:<34} "
                      f"ordinary={x['ordinary']} special={x['special']} yield={y}")

            print("\n=== filter bekerja? (yield >= 8%) ===")
            d = json.loads((await s.call_tool("dividends_calendar",
                                              {"min_yield": 8, "limit": 10})).content[0].text)
            print(f"  rows={d['row_count']} (harus lebih sedikit dari tanpa filter)")
            for x in d["rows"][:4]:
                print(f"   {x['ticker']:<6} yield={x['yield_pct']:.2f}%")

            print("\n=== paginasi: halaman 1 vs 2 memakai counts yang dilaporkan ===")
            d = json.loads((await s.call_tool("earnings_calendar",
                                              {"date_from": "2026-09-01", "date_to": "2026-09-30",
                                               "limit": 50})).content[0].text)
            print(f"  total_items={d['counts']['total_items']} total_pages={d['counts']['total_pages']} "
                  f"page_size={d['counts']['page_size']} (fixed, param pageSize diabaikan)")


if __name__ == "__main__":
    asyncio.run(main())
