"""Real MCP protocol smoke test: spawn the server over stdio and call tools via the SDK client."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

PY = os.environ.get("FINVIZ_PY", sys.executable)
SERVER = os.environ.get("FINVIZ_SERVER", str(HERE / "finviz_mcp_server.py"))


async def main() -> int:
    params = StdioServerParameters(command=PY, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            name = getattr(info, "name", "?") if info is not None else "?"
            proto = getattr(init, "protocol_version", None) or getattr(init, "protocolVersion", "?")
            print("server:", name, "| protocol:", proto)
            tools = await session.list_tools()
            print("tools:", len(tools.tools))
            for t in tools.tools:
                print("  -", t.name, "|", (t.description or "").split("\n")[0][:70])

            calls = [
                ("robots_policy", {}),
                ("quote", {"ticker": "MSFT", "metrics": ["P/E", "RSI (14)"]}),
                ("signals_snapshot", {}),
                ("screener_signal", {"signal": "newhigh"}),
            ]
            for name, args in calls:
                res = await session.call_tool(name, args)
                text = res.content[0].text if res.content else ""
                data = {}
                try:
                    data = json.loads(text)
                except Exception:  # noqa: BLE001
                    pass
                verdict = "ok" if data.get("ok", True) else "error"
                detail = ""
                if name == "robots_policy":
                    detail = " | ".join(
                        f"{'A' if c['allowed'] else 'D'}:{c['path'][:26]}" for c in data.get("checks", [])[:6]
                    )
                elif name == "quote":
                    detail = f"metrics={list((data.get('metrics') or {}).items())}"
                elif name == "signals_snapshot":
                    detail = f"blocks={data.get('block_count')} keys={list(data.get('signal_blocks', {}))[:6]}"
                elif name == "screener_signal":
                    detail = (data.get("error") or f"rows={data.get('row_count')}")[:80]
                print(f"CALL {name} -> {verdict} | {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
