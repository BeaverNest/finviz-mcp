#!/usr/bin/env python
"""Smoke test for the Finviz MCP tools — calls each @mcp.tool() function directly."""
import json
import sys

sys.path.insert(0, str(HERE))
import finviz_mcp_server as f  # noqa: E402
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def show(label: str, raw: str, keys: int = 6, rows: int = 3) -> None:
    print(f"\n=== {label} ===")
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print("  NOT JSON:", exc, raw[:200])
        return
    if not data.get("ok", True):
        print("  ok=False error:", str(data.get("error"))[:220])
    if isinstance(data.get("rows"), list):
        print(f"  rows={len(data['rows'])} headers={data.get('headers')}")
        for r in data["rows"][:rows]:
            print("   ", json.dumps(r, ensure_ascii=False)[:190])
    if isinstance(data.get("headlines"), list):
        print(f"  headlines={len(data['headlines'])}")
        for h in data["headlines"][:rows]:
            print("   ", h.get("source", ""), "|", h["title"][:80])
    if isinstance(data.get("events"), list):
        print(f"  events={len(data['events'])}")
        for e in data["events"][:rows]:
            print("   ", e["date"], e["event"], "| imp", e["importance"], "| act", e["actual"], "fct", e["forecast"])
    if isinstance(data.get("metrics"), dict):
        print(f"  metrics={len(data['metrics'])}: ", list(data["metrics"].items())[:keys])
        print("  header:", data.get("header"))
    if isinstance(data.get("signal_blocks"), dict):
        print(f"  blocks={data['block_count']}: {list(data['signal_blocks'])[:8]}")
        first = list(data["signal_blocks"].items())[0]
        print("   sample", first[0], first[1][:2])
    for grp in ("breadth", "futures", "forex_bonds_crypto"):
        if isinstance(data.get(grp), (dict, list)):
            print(f"  {grp}:", json.dumps(data[grp], ensure_ascii=False)[:260])
    if "robots" in label.lower() or "policy" in label.lower():
        for c in data.get("checks", [])[:12]:
            print(f"   {'ALLOW' if c['allowed'] else 'DENY '} {c['path']}")
    meta = data.get("_meta")
    if meta:
        print("  meta:", {k: meta[k] for k in ("robots_compliant", "from_cache", "data_quality")})


show("health", f.health())
show("robots_policy", f.robots_policy(), rows=2)
show("quote AAPL", f.quote("AAPL", ["P/E", "Forward P/E", "RSI (14)", "Beta", "Market Cap", "Short Float"]))
show("market_overview", f.market_overview())
show("signals_snapshot", f.signals_snapshot())
show("screener_signal (default = should deny)", f.screener_signal("topgainers"))
show("screener_signal (opt-in)", f.screener_signal("topgainers", limit=5, allow_noncompliant=True))
show("custom_screener (opt-in)", f.custom_screener("idx_sp500,fa_pe_u20,ta_sma200_pa", limit=5, allow_noncompliant=True))
show("news AAPL", f.news("AAPL", limit=5))
show("insider_trades", f.insider_trades(limit=5))
show("economic_calendar", f.economic_calendar(min_importance=2))
print("\nDONE")
