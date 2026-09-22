import os
import subprocess
import sys


def test_missing_key_exits_with_readable_utf8_error_before_mcp_starts(tmp_path):
    environment = {
        **os.environ,
        "LLM_MODEL": "test-model",
        "LLM_API_KEY": "",
        "MCP_CONFIG_PATH": str(tmp_path / "does-not-exist.json"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "mcp_agent", "ask", "你好"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "请在 .env 中填写 LLM_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
