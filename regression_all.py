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
    ("economic_calendar", {"min_importance": 1}),
    ("screener_signal", {"signal": "topgainers", "limit": 5}),      # harus LOLOS (URL allow-list, v=340)
    ("custom_screener", {"filters": "idx_sp500", "limit": 5}),      # harus MENOLAK (butuh opt-in)
    ("custom_screener", {"filters": "industry_shellcompanies", "limit": 5,
                         "allow_noncompliant": True}),              # harus LOLOS dengan opt-in
]


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
                    detail = f"rows={len(d['rows'])}"
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
                    lolos += 1
                    print(f"  {name:<20} ok=True  {detail}")
                elif ok is False:
                    # Hanya penolakan custom_screener TANPA opt-in yang benar; sinyal allow-list
                    # sekarang harus lolos, jadi ok=False di sana adalah regresi.
                    err = str(d.get("error", ""))[:60]
                    benar = (name == "custom_screener" and "robots" in err.lower()
                             and not args.get("allow_noncompliant"))
                    print(f"  {name:<20} ok=False {err} {'<- memang seharusnya begitu' if benar else '<- PERIKSA'}")
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
