"""Validation of the bundled tools before crossing the MCP transport boundary."""

import math
import sys
from datetime import UTC, datetime, timedelta

import pytest

from mcp_agent.servers.demo import add, get_current_time, multiply


@pytest.mark.parametrize(
    ("operation", "a", "b", "expected"),
    [
        (add, 1.25, -2.5, -1.25),
        (add, sys.float_info.max, -sys.float_info.max, 0.0),
        (multiply, -2.5, 4.0, -10.0),
        (multiply, sys.float_info.max, 0.0, 0.0),
    ],
)
def test_arithmetic_with_finite_numbers(operation, a, b, expected):
    result = operation(a, b)
    assert math.isfinite(result)
    assert result == expected


@pytest.mark.parametrize("operation", [add, multiply])
@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("argument", ["a", "b"])
def test_arithmetic_rejects_non_finite_inputs(operation, non_finite, argument):
    arguments = {"a": 0.0, "b": 0.0, argument: non_finite}
    with pytest.raises(ValueError, match=rf"{argument} must be a finite number"):
        operation(**arguments)


@pytest.mark.parametrize(
    ("operation", "a", "b"),
    [
        (add, sys.float_info.max, sys.float_info.max),
        (add, -sys.float_info.max, -sys.float_info.max),
        (multiply, sys.float_info.max, 2.0),
        (multiply, -sys.float_info.max, 2.0),
    ],
)
def test_arithmetic_rejects_overflow(operation, a, b):
    with pytest.raises(ValueError, match="result must be a finite number"):
        operation(a, b)


@pytest.mark.parametrize("zone", ["Not/A_Timezone", "", "../UTC", "/UTC"])
def test_invalid_timezone_returns_clear_error(zone):
    with pytest.raises(ValueError, match="Unknown or invalid IANA timezone"):
        get_current_time(zone)


def test_default_time_is_current_and_has_shanghai_offset():
    before = datetime.now(UTC)
    result = datetime.fromisoformat(get_current_time())
    after = datetime.now(UTC)
    assert result.utcoffset() == timedelta(hours=8)
    assert before <= result <= after


def test_explicit_utc_time_is_current_and_has_offset():
    before = datetime.now(UTC)
    result = datetime.fromisoformat(get_current_time("UTC"))
    after = datetime.now(UTC)
    assert result.utcoffset() == timedelta(0)
    assert before <= result <= after
