"""Exercise the real OpenAI-compatible client against an in-memory HTTP service."""

import json

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import mcp_agent.llm as llm_module
from mcp_agent.graph import build_graph
from mcp_agent.settings import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        llm_model="mock-tool-calling-model",
        llm_api_key="unit-test-key-not-valid",
        llm_base_url="https://mock-provider.invalid/custom/v1",
        llm_temperature=0.25,
        llm_timeout_seconds=12.5,
        llm_max_retries=0,
    )


def _completion(message: dict, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1,
            "model": "mock-tool-calling-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )


def _inject_transport(monkeypatch, client: httpx.AsyncClient) -> None:
    def create_client(**kwargs):
        return ChatOpenAI(**kwargs, http_async_client=client)

    monkeypatch.setattr(llm_module, "ChatOpenAI", create_client)


async def test_create_llm_uses_configured_compatible_endpoint(monkeypatch):
    settings = _settings()
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _completion({"role": "assistant", "content": "connected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        _inject_transport(monkeypatch, client)
        model = llm_module.create_llm(settings)
        result = await model.ainvoke([HumanMessage(content="ping")])

    assert result.content == "connected"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://mock-provider.invalid/custom/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer unit-test-key-not-valid"
    body = json.loads(request.content)
    assert body["model"] == settings.llm_model
    assert body["temperature"] == 0.25
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert request.extensions["timeout"]["read"] == 12.5
    assert model.max_retries == 0
    assert model.use_responses_api is False
    assert "unit-test-key-not-valid" not in repr(settings)


async def test_real_chat_openai_completes_langgraph_tool_loop(monkeypatch):
    requests = []
    executed = []

    @tool("demo_add")
    def add(a: int, b: int) -> int:
        """Add two integers and return their sum."""
        executed.append((a, b))
        return a + b

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/custom/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "mock-tool-calling-model"
        assert body["tools"][0]["type"] == "function"
        function = body["tools"][0]["function"]
        assert function["name"] == "demo_add"
        assert set(function["parameters"]["required"]) == {"a", "b"}
        assert function["parameters"]["properties"]["a"]["type"] == "integer"

        if len(requests) == 1:
            assert [message["role"] for message in body["messages"]] == ["system", "user"]
            assert body["messages"][-1]["content"] == "计算 2 + 3。"
            return _completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_demo_add_1",
                            "type": "function",
                            "function": {
                                "name": "demo_add",
                                "arguments": json.dumps({"a": 2, "b": 3}),
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )

        assert len(requests) == 2
        assert [message["role"] for message in body["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        assert body["messages"][-2]["tool_calls"][0]["id"] == "call_demo_add_1"
        tool_result = body["messages"][-1]
        assert tool_result["tool_call_id"] == "call_demo_add_1"
        assert tool_result["content"] == "5"
        return _completion({"role": "assistant", "content": "2 + 3 = 5。"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        _inject_transport(monkeypatch, client)
        graph = build_graph(llm_module.create_llm(_settings()), [add])
        result = await graph.ainvoke({"messages": [HumanMessage(content="计算 2 + 3。")]})

    assert len(requests) == 2
    assert executed == [(2, 3)]
    assert isinstance(result["messages"][-2], ToolMessage)
    assert result["messages"][-2].tool_call_id == "call_demo_add_1"
    assert result["messages"][-1].content == "2 + 3 = 5。"
