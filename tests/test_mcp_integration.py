"""Real stdio transport tests: discover tools, execute them, and traverse the graph."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from mcp_agent.graph import build_graph
from mcp_agent.mcp_client import discover_tools


def number_from_result(content):
    """MCP adapter results use text blocks; accept plain text from other adapters too."""
    if isinstance(content, str):
        return float(content)
    return float("".join(block["text"] for block in content if block["type"] == "text"))


async def test_discovers_and_calls_real_stdio_tools():
    async with asyncio.timeout(45):
        tools = {tool.name: tool for tool in await discover_tools()}
        assert {
            "demo_add",
            "demo_multiply",
            "demo_get_current_time",
            "database_database_info",
            "database_list_tables",
            "database_describe_table",
            "database_query",
            "web_search",
        } == set(tools)
        assert {"a", "b"} <= set(tools["demo_add"].args)

        addition = await tools["demo_add"].ainvoke({"a": 12, "b": 20})
        multiplication = await tools["demo_multiply"].ainvoke(
            {"a": number_from_result(addition), "b": 3}
        )

    assert number_from_result(addition) == 32
    assert number_from_result(multiplication) == 96


async def test_graph_completes_multistep_task_using_real_mcp_tools():
    class ArithmeticModel:
        def bind_tools(self, tools):
            assert {"demo_add", "demo_multiply"} <= {tool.name for tool in tools}
            return RunnableLambda(self.respond)

        def respond(self, messages):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                name, arguments, call_id = "demo_add", {"a": 12, "b": 20}, "add-call"
            elif len(results) == 1:
                assert results[-1].tool_call_id == "add-call"
                subtotal = number_from_result(results[-1].content)
                name, arguments, call_id = (
                    "demo_multiply",
                    {"a": subtotal, "b": 3},
                    "multiply-call",
                )
            else:
                assert results[-1].tool_call_id == "multiply-call"
                return AIMessage(content=f"答案是 {number_from_result(results[-1].content):g}")
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": arguments, "id": call_id}],
            )

    async with asyncio.timeout(45):
        graph = build_graph(ArithmeticModel(), await discover_tools())
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="先计算 12+20，再将结果乘以 3。")]},
            config={"recursion_limit": 10},
        )

    assert result["messages"][-1].content == "答案是 96"
    results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.name for m in results] == ["demo_add", "demo_multiply"]
    assert all(m.status == "success" for m in results)
