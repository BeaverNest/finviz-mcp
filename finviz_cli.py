#!/usr/bin/env python
"""Direct stdio client for the BeaverNest Finviz MCP server.

Works WITHOUT Hermes MCP registration / gateway restart: it spawns the server over
stdio, speaks JSON-RPC (initialize -> notifications/initialized -> tools/call),
prints the tool result and exits.

Usage:
  python3 finviz_cli.py <tool> [key=value ...]
  python3 finviz_cli.py list
  python3 finviz_cli.py health
  python3 finviz_cli.py quote ticker=AAPL
  python3 finviz_cli.py quote ticker=MSFT metrics=P/E
  python3 finviz_cli.py market_overview
  python3 finviz_cli.py signals_snapshot
  python3 finviz_cli.py news ticker=NVDA limit=10
  python3 finviz_cli.py insider_trades limit=20
  python3 finviz_cli.py economic_calendar min_importance=3
  python3 finviz_cli.py robots_policy
  python3 finviz_cli.py screener_signal signal=topgainers allow_noncompliant=true
  python3 finviz_cli.py custom_screener filters=idx_sp500,fa_pe_u20 allow_noncompliant=true

Read-only, throttled and cached server-side. Finviz data is external free-tier
(delayed, US markets) — educational/analytical use, never investment advice.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

PY = os.environ.get("FINVIZ_PY", sys.executable)
SERVER = os.environ.get("FINVIZ_SERVER", str(HERE / "finviz_mcp_server.py"))
STDERR_LOG = os.environ.get("FINVIZ_STDERR_LOG", str(HERE / "finviz_mcp_stderr.log"))


def rpc(tool: str, args: dict, timeout: float = 120.0) -> dict:
    proc = subprocess.Popen(
        [PY, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(STDERR_LOG, "a"),
        text=True,
    )
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "finviz-cli", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": args}},
    ]
    for p in payload:
        proc.stdin.write(json.dumps(p) + "\n")
        proc.stdin.flush()

    lines: "queue.Queue[dict]" = queue.Queue()

    def _reader() -> None:
        for line in proc.stdout:
            try:
                lines.put(json.loads(line))
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = lines.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if msg.get("id") == 2:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return msg.get("result", {})
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "error": f"no tools/call response in {timeout}s",
            "hint": f"see {STDERR_LOG}"}


def coerce(value: str):
    """Tebak tipe argumen CLI: bool, JSON (list/dict), angka, atau string.

    Tanpa ini, `tickers=AAPL,MSFT` terkirim sebagai string dan tool yang mengharapkan
    list menolak dengan validation error.
    """
    raw = value.strip()
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if raw[:1] in "[{":
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
    if "," in raw and raw[:1] != "{":
        # daftar sederhana: tickers=AAPL,MSFT,JPM -> ["AAPL","MSFT","JPM"]
        return [part.strip() for part in raw.split(",") if part.strip()]
    try:
        if raw.lstrip("-").isdigit():
            return int(raw)
        f = float(raw)
        return int(f) if f.is_integer() else f
    except ValueError:
        return raw


def list_schemas() -> dict:
    """Ambil inputSchema tiap tool sekali, supaya CLI tahu tipe argumen yang benar."""
    proc = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=open(STDERR_LOG, "a"), text=True)
    for p in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                          "clientInfo": {"name": "finviz-cli", "version": "1"}}},
              {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
        proc.stdin.write(json.dumps(p) + "\n")
        proc.stdin.flush()
    out: dict = {}
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if msg.get("id") == 2:
            for t in msg.get("result", {}).get("tools", []):
                props = (t.get("inputSchema") or {}).get("properties") or {}
                out[t["name"]] = {k: (v.get("type") or "") for k, v in props.items()}
            break
    proc.kill()
    return out


def coerce_typed(value: str, declared: str):
    """Coerce mengikuti tipe yang dideklarasikan schema, bukan menebak dari isi.

    `filters=fa_div_o6,fa_pe_u20` adalah SATU string; `tickers=AAPL,MSFT` adalah array.
    Menebak dari koma saja akan merusak parameter string yang memang berisi koma.
    """
    if declared == "array":
        if value.strip()[:1] == "[":
            try:
                return json.loads(value)
            except Exception:  # noqa: BLE001
                pass
        return [part.strip() for part in value.split(",") if part.strip()]
    if declared in ("string", ""):
        return value
    return coerce(value)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tool = sys.argv[1]
    if tool == "list":
        proc = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=open(STDERR_LOG, "a"), text=True)
        for p in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "finviz-cli", "version": "1"}}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
            proc.stdin.write(json.dumps(p) + "\n")
            proc.stdin.flush()
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if msg.get("id") == 2:
                for t in msg.get("result", {}).get("tools", []):
                    print(f"{t['name']:20s} {(t.get('description') or '').splitlines()[0][:70]}")
                break
        proc.kill()
        return 0
    raw_args: dict = {}
    for raw in sys.argv[2:]:
        if "=" in raw:
            k, v = raw.split("=", 1)
        else:
            k, v = "ticker", raw
        raw_args[k] = v
    schema = list_schemas().get(tool, {})
    args: dict = {k: coerce_typed(v, schema.get(k, "")) for k, v in raw_args.items()}
    started = time.monotonic()
    result = rpc(tool, args)
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and content:
        text = content[0].get("text", "")
        try:
            print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            print(text)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"# elapsed {time.monotonic() - started:.1f}s")
    return 1 if (isinstance(result, dict) and result.get("isError")) else 0


if __name__ == "__main__":
    sys.exit(main())
