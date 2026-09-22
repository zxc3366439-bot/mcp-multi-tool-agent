"""Discover tools from configurable MCP servers via the official adapter."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StdioServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    inherit_env: list[str] = Field(default_factory=list)


class HttpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["streamable_http"]
    url: str = Field(pattern=r"^https?://")
    headers: dict[str, str] | None = None


Server = Annotated[StdioServer | HttpServer, Field(discriminator="transport")]
SERVER_ADAPTER = TypeAdapter(dict[str, Server])
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, environment: dict[str, str | None]) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            replacement = environment.get(name)
            if replacement is None or replacement == "":
                raise ValueError(f"MCP 配置引用了未设置的环境变量：{name}")
            return replacement

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, environment) for item in value]
    return value


DATABASE_ENV = [
    "DB_ENGINE",
    "SQLITE_PATH",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DATABASE",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_SSL_CA",
    "DB_QUERY_TIMEOUT_SECONDS",
    "DB_MAX_ROWS",
]
WEB_SEARCH_ENV = [
    "WEB_SEARCH_PROVIDER",
    "TAVILY_API_KEY",
    "WEB_SEARCH_TIMEOUT_SECONDS",
    "WEB_SEARCH_MAX_RESULTS",
    "WEB_SEARCH_REGION",
    "WEB_SEARCH_BACKEND",
]


def _builtin_server(module: str, inherit_env: list[str]) -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", module],
        "env": {"PYTHONIOENCODING": "utf-8"},
        "inherit_env": inherit_env,
    }


def load_connections(path: Path | None = None) -> dict[str, Any]:
    """Use the three built-in servers unless an explicit configuration is supplied."""
    if path is None:
        raw = {
            "mcpServers": {
                "demo": _builtin_server("mcp_agent.servers.demo", []),
                "database": _builtin_server("mcp_agent.servers.database", DATABASE_ENV),
                "web": _builtin_server("mcp_agent.servers.web_search", WEB_SEARCH_ENV),
            }
        }
    else:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or set(raw) != {"mcpServers"}:
        raise ValueError("MCP 配置必须包含且仅包含 mcpServers 对象。")
    environment = {**dotenv_values(".env"), **os.environ, "PYTHON": sys.executable}
    servers = SERVER_ADAPTER.validate_python(_expand(raw["mcpServers"], environment))
    if not servers:
        raise ValueError("请至少配置一个 MCP Server。")
    for name in servers:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("MCP Server 名称只允许字母、数字、下划线和连字符。")
    connections = {}
    for name, server in servers.items():
        connection = server.model_dump(exclude_none=True)
        if isinstance(server, StdioServer):
            connection.pop("inherit_env")
            inherited = {
                key: environment[key]
                for key in server.inherit_env
                if environment.get(key) is not None
            }
            connection["env"] = {**inherited, **(server.env or {})}
        connections[name] = connection
    return connections


async def discover_tools(path: Path | None = None, *, timeout: float = 30) -> list[BaseTool]:
    connections = load_connections(path)
    # Stateless sessions are owned/closed by the adapter on each invocation.
    client = MultiServerMCPClient(connections, tool_name_prefix=True)
    async with asyncio.timeout(timeout):
        tools = await client.get_tools()
    names = [tool.name for tool in tools]
    if not names:
        raise ValueError("MCP Server 未提供任何工具。")
    if len(names) != len(set(names)):
        raise ValueError("MCP 工具名称冲突，请修改 Server 或工具名称。")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names):
        raise ValueError("工具名（含 Server 前缀）须为 1–64 个字母、数字、下划线或连字符。")
    return tools
