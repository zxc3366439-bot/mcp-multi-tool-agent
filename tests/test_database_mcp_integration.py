"""Verify the database through real MCP subprocesses and a deterministic graph."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from mcp_agent.database import initialize_demo
from mcp_agent.graph import build_graph
from mcp_agent.mcp_client import discover_tools


def result_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block["type"] == "text")


def result_json(content):
    return json.loads(result_text(content))


@pytest.fixture
def database_config(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "数据库 demo.sqlite3"
    initialize_demo(database_path)
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "database": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": ["-m", "mcp_agent.servers.database"],
                        "env": {
                            "DB_ENGINE": "sqlite",
                            "SQLITE_PATH": str(database_path),
                            "PYTHONIOENCODING": "utf-8",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MYSQL_PASSWORD", "integration-password-must-not-leak")
    monkeypatch.setenv("LLM_API_KEY", "integration-api-key-must-not-leak")
    return config_path, database_path


async def test_real_database_tools_expose_schema_and_parameterized_results(database_config):
    config_path, database_path = database_config
    with sqlite3.connect(database_path) as connection:
        expected = connection.execute(
            "SELECT id, name FROM products ORDER BY id LIMIT 1"
        ).fetchone()
    assert expected is not None

    async with asyncio.timeout(60):
        tools = {tool.name: tool for tool in await discover_tools(config_path)}
        assert set(tools) == {
            "database_database_info",
            "database_list_tables",
            "database_describe_table",
            "database_query",
        }
        info = result_json(await tools["database_database_info"].ainvoke({}))
        tables = result_json(await tools["database_list_tables"].ainvoke({}))
        schema = result_json(
            await tools["database_describe_table"].ainvoke({"table_name": "products"})
        )
        query = result_json(
            await tools["database_query"].ainvoke(
                {
                    "sql": "SELECT id, name FROM products WHERE id = :product_id",
                    "parameters": {"product_id": expected[0]},
                    "max_rows": 10,
                }
            )
        )

    assert info["dialect"] == "sqlite"
    assert info["read_only"] is True
    assert "integration-password-must-not-leak" not in json.dumps(info)
    assert "integration-api-key-must-not-leak" not in json.dumps(info)
    assert {"products", "orders"} <= {table["name"] for table in tables["tables"]}
    assert schema["table"] == "products"
    assert {"id", "name", "category", "price", "stock"} == {
        column["name"] for column in schema["columns"]
    }
    assert query["columns"] == ["id", "name"]
    assert query["rows"] == [list(expected)]
    assert query["row_count"] == 1
    assert query["truncated"] is False


async def test_database_write_is_an_error_tool_message_and_preserves_data(database_config):
    config_path, database_path = database_config
    with sqlite3.connect(database_path) as connection:
        before = connection.execute("SELECT * FROM products ORDER BY id").fetchall()

    class WriteAttemptModel:
        def bind_tools(self, tools):
            return RunnableLambda(self.respond)

        def respond(self, messages):
            if isinstance(messages[-1], ToolMessage):
                assert messages[-1].status == "error"
                assert result_text(messages[-1].content)
                assert "integration-password-must-not-leak" not in result_text(messages[-1].content)
                return AIMessage(content="数据库工具拒绝了写入操作。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "database_query",
                        "args": {"sql": "DELETE FROM products"},
                        "id": "write-attempt",
                    }
                ],
            )

    async with asyncio.timeout(45):
        graph = build_graph(WriteAttemptModel(), await discover_tools(config_path))
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="删除商品数据。")]},
            config={"recursion_limit": 6},
        )

    assert result["messages"][-1].content == "数据库工具拒绝了写入操作。"
    tool_results = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "write-attempt"
    assert tool_results[0].status == "error"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT * FROM products ORDER BY id").fetchall() == before


async def test_graph_answers_from_real_database_count(database_config):
    config_path, database_path = database_config
    with sqlite3.connect(database_path) as connection:
        expected_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    class ProductCountModel:
        def bind_tools(self, tools):
            assert "database_query" in {tool.name for tool in tools}
            return RunnableLambda(self.respond)

        def respond(self, messages):
            if isinstance(messages[-1], ToolMessage):
                assert messages[-1].status == "success"
                payload = result_json(messages[-1].content)
                assert payload["columns"] == ["total"]
                return AIMessage(content=f"共有 {payload['rows'][0][0]} 种商品。")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "database_query",
                        "args": {"sql": "SELECT COUNT(*) AS total FROM products"},
                        "id": "count-products",
                    }
                ],
            )

    async with asyncio.timeout(45):
        graph = build_graph(ProductCountModel(), await discover_tools(config_path))
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="数据库中有多少种商品？")]},
            config={"recursion_limit": 6},
        )

    assert result["messages"][-1].content == f"共有 {expected_count} 种商品。"
    assert [message.type for message in result["messages"]] == ["human", "ai", "tool", "ai"]
    assert result["messages"][2].tool_call_id == "count-products"
