"""OpenAI-compatible Chat Completions model factory."""

from langchain_openai import ChatOpenAI

from mcp_agent.settings import Settings


def create_llm(settings: Settings) -> ChatOpenAI:
    settings.require_llm()
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        use_responses_api=False,
    )
