"""Exercise graph control flow with deterministic models and no provider API key."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import ToolException, tool
from langgraph.errors import GraphRecursionError

from mcp_agent.graph import build_graph


class FakeModel:
    def __init__(self, respond):
        self.respond = respond
        self.inputs = []
        self.tools = []

    def bind_tools(self, tools):
        self.tools = tools
        return RunnableLambda(self._invoke)

    def _invoke(self, messages, config):
        self.inputs.append(list(messages))
        return self.respond(messages)


@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@tool
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


def call_tool(name, arguments, call_id):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id}],
    )


async def test_multistep_tool_results_drive_next_model_request():
    def respond(messages):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if not results:
            return call_tool("add", {"a": 12, "b": 20}, "addition")
        if len(results) == 1:
            subtotal = float(results[-1].content)
            assert subtotal == 32
            return call_tool("multiply", {"a": subtotal, "b": 3}, "multiplication")
        return AIMessage(content=f"计算结果是 {float(results[-1].content):g}。")

    model = FakeModel(respond)
    graph = build_graph(model, [add, multiply], system_prompt="Always use tool results.")
    result = await graph.ainvoke({"messages": [HumanMessage(content="(12+20)*3")]})

    assert result["messages"][-1].content == "计算结果是 96。"
    assert [message.type for message in result["messages"]] == [
        "human",
        "ai",
        "tool",
        "ai",
        "tool",
        "ai",
    ]
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["addition", "multiplication"]
    assert model.tools == [add, multiply]
    assert len(model.inputs) == 3
    for messages in model.inputs:
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "Always use tool results."
        assert sum(isinstance(m, SystemMessage) for m in messages) == 1


async def test_model_can_finish_without_calling_tools():
    model = FakeModel(lambda messages: AIMessage(content="你好！"))
    result = await build_graph(model, [add]).ainvoke({"messages": [HumanMessage("你好")]})

    assert result["messages"][-1].content == "你好！"
    assert len(model.inputs) == 1
    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


async def test_recursion_limit_interrupts_an_endless_tool_loop():
    model = FakeModel(lambda messages: call_tool("add", {"a": 1, "b": 1}, f"loop-{len(messages)}"))

    with pytest.raises(GraphRecursionError):
        await build_graph(model, [add]).ainvoke(
            {"messages": [HumanMessage(content="Keep calculating.")]},
            config={"recursion_limit": 4},
        )
    assert 1 < len(model.inputs) <= 4


async def test_tool_exception_is_visible_to_model_as_an_error_result():
    @tool
    def unavailable() -> str:
        """Query a temporarily unavailable source."""
        raise ToolException("Source temporarily unavailable")

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            assert messages[-1].status == "error"
            assert "Source temporarily unavailable" in messages[-1].content
            return AIMessage(content="数据源暂时不可用。")
        return call_tool("unavailable", {}, "unavailable-call")

    model = FakeModel(respond)
    result = await build_graph(model, [unavailable]).ainvoke(
        {"messages": [HumanMessage(content="查询数据。")]}
    )

    assert result["messages"][-1].content == "数据源暂时不可用。"
    assert len(model.inputs) == 2


async def test_model_can_correct_invalid_tool_arguments():
    def respond(messages):
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if not results:
            return call_tool("add", {"a": "not-a-number", "b": 2}, "invalid")
        if len(results) == 1:
            assert results[-1].status == "error"
            assert "a" in results[-1].content
            return call_tool("add", {"a": 1, "b": 2}, "corrected")
        assert results[-1].status == "success"
        return AIMessage(content=f"{float(results[-1].content):g}")

    result = await build_graph(FakeModel(respond), [add]).ainvoke(
        {"messages": [HumanMessage(content="1+2")]}
    )

    assert result["messages"][-1].content == "3"
