"""Command-line entry points for discovery, one-shot questions and chat."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError
from pydantic import ValidationError

from mcp_agent.graph import build_graph
from mcp_agent.llm import create_llm
from mcp_agent.mcp_client import discover_tools
from mcp_agent.settings import Settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="LLM + LangGraph + MCP 多工具智能体")
    result.add_argument("--mcp-config", type=Path, help="指定 MCP JSON 配置文件")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("tools", help="列出 MCP 工具，无需 LLM API Key")
    db_init = commands.add_parser("db-init", help="创建新的 SQLite 示例库，不覆盖已有文件")
    db_init.add_argument("--path", type=Path, help="文件路径，默认读取 SQLITE_PATH")
    call = commands.add_parser("call", help="直接调用 MCP 工具，无需 LLM API Key")
    call.add_argument("tool", help="工具全名，例如 database_list_tables 或 web_search")
    call_arguments = call.add_mutually_exclusive_group()
    call_arguments.add_argument("--args", default="{}", help="工具参数 JSON 对象")
    call_arguments.add_argument("--args-file", type=Path, help="UTF-8 JSON 参数文件")
    ask = commands.add_parser("ask", help="执行单轮任务")
    ask.add_argument("prompt", help="发送给智能体的任务")
    ask.add_argument("--trace", action="store_true", help="显示本轮工具调用及结果")
    chat = commands.add_parser("chat", help="交互对话；/reset 清空上下文，/exit 退出")
    chat.add_argument("--trace", action="store_true", help="显示本轮工具调用及结果")
    return result


def print_turn(messages: list[BaseMessage], *, trace: bool) -> None:
    if trace:
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    arguments = json.dumps(call["args"], ensure_ascii=False)
                    print(f"[工具调用] {call['name']} {arguments}")
            elif isinstance(message, ToolMessage):
                content = message.content
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                print(f"[工具结果:{message.status}] {message.name}: {content}")
    if messages:
        print(messages[-1].text)


def error_message(error: Exception) -> str:
    """Give useful errors without echoing HTTP bodies, credentials or headers."""
    if isinstance(error, BaseExceptionGroup):
        return (
            "；".join(
                dict.fromkeys(
                    error_message(item) for item in error.exceptions if isinstance(item, Exception)
                )
            )
            or "MCP 会话异常。"
        )
    if isinstance(error, ValidationError):
        fields = [".".join(map(str, item["loc"])) for item in error.errors()]
        return "配置或参数格式错误，请检查：" + ", ".join(fields)
    if isinstance(error, GraphRecursionError):
        return "已达到工具循环上限，请简化任务或调整 AGENT_RECURSION_LIMIT。"
    if isinstance(error, TimeoutError):
        return "执行超时，请检查 MCP/LLM 服务或调整相应 TIMEOUT_SECONDS 配置。"
    if isinstance(error, AuthenticationError):
        return "LLM 鉴权失败，请检查 LLM_API_KEY 和 LLM_BASE_URL。"
    if isinstance(error, RateLimitError):
        return "LLM 请求受限，请检查服务额度或稍后重试。"
    if isinstance(error, APIConnectionError):
        return "无法连接 LLM，请检查网络和 LLM_BASE_URL。"
    if isinstance(error, APIStatusError):
        return f"LLM 返回 HTTP {error.status_code}，请检查模型名、服务状态和 tool calling 支持。"
    if isinstance(error, FileNotFoundError):
        return "找不到配置文件或 MCP 启动程序，请检查路径和 command。"
    if isinstance(error, json.JSONDecodeError):
        return f"MCP 配置不是有效 JSON（第 {error.lineno} 行，第 {error.colno} 列）。"
    if isinstance(error, ValueError):
        return str(error)
    return f"执行失败（{type(error).__name__}），请检查 MCP 服务及配置。"


async def run(args: argparse.Namespace) -> int:
    if args.command == "db-init":
        from mcp_agent.database import DatabaseSettings, initialize_demo

        database_settings = DatabaseSettings()
        if args.path is None and database_settings.db_engine != "sqlite":
            raise ValueError(
                "db-init 只创建 SQLite 示例库；请显式指定 --path，或设置 DB_ENGINE=sqlite。"
            )
        path = args.path if args.path is not None else database_settings.sqlite_path
        result = initialize_demo(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    settings = Settings()
    # Fail on missing credentials before starting any server for LLM commands.
    model = create_llm(settings) if args.command in {"ask", "chat"} else None
    config_path = args.mcp_config if args.mcp_config is not None else settings.mcp_config_path
    tools = await discover_tools(config_path, timeout=settings.mcp_startup_timeout_seconds)
    if args.command == "tools":
        for tool in tools:
            print(f"{tool.name}: {tool.description}")
        return 0

    if args.command == "call":
        raw = args.args_file.read_text(encoding="utf-8-sig") if args.args_file else args.args
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("工具参数必须是有效的 JSON 对象。") from error
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象。")
        tool = next((item for item in tools if item.name == args.tool), None)
        if tool is None:
            raise ValueError("找不到此工具，请先运行 mcp-agent tools 查看名称。")
        async with asyncio.timeout(settings.agent_timeout_seconds):
            response = await tool.ainvoke(
                {
                    "type": "tool_call",
                    "id": "cli-call",
                    "name": tool.name,
                    "args": arguments,
                }
            )
        content = response.content if isinstance(response, ToolMessage) else response
        if (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "text"
        ):
            content = content[0]["text"]
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                pass
        print(
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, indent=2)
        )
        return 1 if isinstance(response, ToolMessage) and response.status == "error" else 0

    graph = build_graph(model, tools)
    history: list[BaseMessage] = []

    async def ask(question: str) -> list[BaseMessage]:
        async with asyncio.timeout(settings.agent_timeout_seconds):
            state = await graph.ainvoke(
                {"messages": [*history, HumanMessage(content=question)]},
                config={"recursion_limit": settings.agent_recursion_limit},
            )
        messages = state["messages"]
        print_turn(messages[len(history) :], trace=args.trace)
        return messages

    if args.command == "ask":
        if not args.prompt.strip():
            raise ValueError("问题不能为空。")
        await ask(args.prompt)
        return 0

    print("智能体已就绪。/reset 清空当前上下文，/exit 退出。")
    while True:
        try:
            question = input("\n你 > ").strip()
        except EOFError:
            break
        if question == "/exit":
            break
        if question == "/reset":
            history.clear()
            print("上下文已清空。")
            continue
        if not question:
            continue
        try:
            history = await ask(question)
        except Exception as error:
            print(f"错误：{error_message(error)}", file=sys.stderr)
    return 0


def main() -> None:
    # Keep redirected Chinese output readable on Windows as well as in UTF-8 terminals.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = parser().parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已退出。", file=sys.stderr)
        code = 130
    except Exception as error:
        print(f"错误：{error_message(error)}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
