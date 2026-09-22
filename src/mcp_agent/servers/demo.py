"""Local MCP tools served over stdio.

Start with ``python -m mcp_agent.servers.demo``. Stdout is reserved for
MCP protocol messages; application logs go to stderr.
"""

from __future__ import annotations

import logging
import math
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-tools")


def _require_finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number; NaN and infinity are not supported.")
    return value


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two finite numbers. Reject non-finite inputs or an overflowing result."""
    _require_finite(a, "a")
    _require_finite(b, "b")
    return _require_finite(a + b, "Addition result")


@mcp.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two finite numbers. Reject non-finite inputs or an overflowing result."""
    _require_finite(a, "a")
    _require_finite(b, "b")
    return _require_finite(a * b, "Multiplication result")


@mcp.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """Return the current ISO 8601 time with a UTC offset for an IANA timezone."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Unknown or invalid IANA timezone: {timezone!r}. "
            "Use a name such as 'Asia/Shanghai', 'Europe/London', or 'UTC'."
        ) from exc
    return datetime.now(zone).isoformat()


def main() -> None:
    """Run the server without writing application logs to the protocol stream."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
