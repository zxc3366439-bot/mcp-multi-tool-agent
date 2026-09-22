import json
import sys

import pytest
from pydantic import ValidationError

from mcp_agent.mcp_client import load_connections
from mcp_agent.settings import Settings


def write_config(tmp_path, servers):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_default_uses_current_interpreter_and_installed_module(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    connections = load_connections()
    assert connections["demo"]["command"] == sys.executable
    assert connections["demo"]["args"] == ["-m", "mcp_agent.servers.demo"]


def test_http_headers_expand_environment_without_requiring_llm(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_MCP_TOKEN", "test-token")
    path = write_config(
        tmp_path,
        {
            "search": {
                "transport": "streamable_http",
                "url": "http://localhost:8000/mcp",
                "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"},
            }
        },
    )
    assert load_connections(path)["search"]["headers"]["Authorization"] == "Bearer test-token"


def test_config_uses_dotenv_but_environment_has_priority(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_MCP_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("TEST_MCP_TOKEN", "from-environment")
    path = write_config(
        tmp_path,
        {
            "demo": {
                "transport": "stdio",
                "command": "${PYTHON}",
                "env": {"TOKEN": "${TEST_MCP_TOKEN}"},
            }
        },
    )
    assert load_connections(path)["demo"]["env"]["TOKEN"] == "from-environment"
    assert load_connections(path)["demo"]["command"] == sys.executable


def test_stdio_only_inherits_allowlisted_settings_and_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYSQL_PASSWORD", "private-test-password")
    monkeypatch.setenv("LLM_API_KEY", "unrelated-secret")
    monkeypatch.setenv("DB_ENGINE", "mysql")
    path = write_config(
        tmp_path,
        {
            "database": {
                "transport": "stdio",
                "command": "${PYTHON}",
                "inherit_env": ["MYSQL_PASSWORD", "DB_ENGINE", "UNSET_OPTIONAL_VARIABLE"],
                "env": {"DB_ENGINE": "sqlite"},
            },
            "demo": {"transport": "stdio", "command": "${PYTHON}"},
        },
    )
    connections = load_connections(path)
    assert connections["database"]["env"] == {
        "MYSQL_PASSWORD": "private-test-password",
        "DB_ENGINE": "sqlite",
    }
    assert "inherit_env" not in connections["database"]
    assert "MYSQL_PASSWORD" not in connections["demo"]["env"]
    assert "LLM_API_KEY" not in connections["database"]["env"]


def test_missing_environment_variable_fails_before_startup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    path = write_config(
        tmp_path,
        {
            "demo": {
                "transport": "stdio",
                "command": "${MISSING_MCP_TOKEN}",
            }
        },
    )
    with pytest.raises(ValueError, match="MISSING_MCP_TOKEN"):
        load_connections(path)


@pytest.mark.parametrize(
    "servers",
    [
        {},
        {"bad name": {"transport": "stdio", "command": "python"}},
        {"demo": {"transport": "bogus", "command": "python"}},
        {"demo": {"transport": "stdio", "command": "python", "typo": True}},
    ],
)
def test_reject_invalid_server_configuration(tmp_path, servers):
    with pytest.raises(ValueError):
        load_connections(write_config(tmp_path, servers))


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_recursion_limit", 1),
        ("agent_timeout_seconds", 0),
        ("llm_timeout_seconds", float("inf")),
        ("llm_temperature", float("nan")),
    ],
)
def test_invalid_limits_fail_fast(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_no_key_needed_until_model_creation():
    settings = Settings(_env_file=None, llm_model="test-model", llm_api_key=None)
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        settings.require_llm()


def test_secret_not_in_settings_repr():
    settings = Settings(_env_file=None, llm_api_key="test-secret-value")
    assert "test-secret-value" not in repr(settings)
