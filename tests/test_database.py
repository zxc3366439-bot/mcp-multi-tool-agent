"""Database safety and behavior, with real SQLite and mocked MySQL transport."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

import mcp_agent.database as database
from mcp_agent.database import (
    DatabaseClient,
    DatabaseError,
    DatabaseSettings,
    _json_value,
    _prepare_query,
    initialize_demo,
)


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "中文 database.db"
    initialize_demo(path)
    return DatabaseClient(DatabaseSettings(_env_file=None, sqlite_path=path))


def test_sample_schema_and_parameterized_query(client):
    assert client.database_info()["read_only"] is True
    assert {row["name"] for row in client.list_tables()["tables"]} == {"products", "orders"}
    schema = client.describe_table("products")
    assert [column["name"] for column in schema["columns"]] == [
        "id",
        "name",
        "category",
        "price",
        "stock",
    ]
    assert schema["columns"][0]["primary_key"] is True
    result = client.query("SELECT id, name FROM products WHERE id = :product_id", {"product_id": 1})
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "机械键盘"]]
    assert result["row_count"] == 1


def test_demo_init_never_overwrites_and_missing_database_never_creates(tmp_path, client):
    original = client.settings.sqlite_path.read_bytes()
    with pytest.raises(DatabaseError, match="already exists"):
        initialize_demo(client.settings.sqlite_path)
    assert client.settings.sqlite_path.read_bytes() == original
    absent = tmp_path / "absent.db"
    other = DatabaseClient(DatabaseSettings(_env_file=None, sqlite_path=absent))
    with pytest.raises(DatabaseError, match="db-init"):
        other.database_info()
    assert not absent.exists()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO products VALUES (6, 'x', 'x', 1, 1)",
        "UPDATE products SET stock = 0",
        "DELETE FROM products",
        "DROP TABLE products",
        "CREATE TABLE bad (id INTEGER)",
        "SELECT 1; DELETE FROM products",
        "WITH bad AS (DELETE FROM products RETURNING *) SELECT * FROM bad",
        "SELECT * INTO new_table FROM products",
        "SELECT * FROM products FOR UPDATE",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE ':memory:' AS other",
        "SELECT load_extension('bad')",
        "SELECT readfile('secret')",
        "SELECT writefile('bad', 'bad')",
        "SELECT * FROM pragma_database_list()",
    ],
)
def test_sqlite_rejects_writes_and_side_effects(client, sql):
    with pytest.raises(DatabaseError):
        client.query(sql)
    assert client.query("SELECT COUNT(*) FROM products")["rows"] == [[5]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(10)",
        "SELECT BENCHMARK(1000000, 1)",
        "SELECT GET_LOCK('lock', 10)",
        "SELECT RELEASE_LOCK('lock')",
        "SELECT LOAD_FILE('/secret')",
        "SELECT some_stored_function()",
        "SELECT mydb.abs(1)",
        "SELECT LAST_INSERT_ID(100)",
        "SELECT @variable := 1",
        "SELECT @@sql_mode",
        "SELECT * FROM t FOR SHARE",
        "SELECT * INTO OUTFILE '/tmp/secret' FROM t",
        "SELECT * FROM t INTO DUMPFILE '/tmp/secret'",
        "SELECT 1 /*!50000 INTO OUTFILE '/tmp/secret' */",
        "SELECT /*+ MAX_EXECUTION_TIME(0) */ * FROM t",
    ],
)
def test_mysql_rejects_side_effects_before_connect(sql):
    with pytest.raises(DatabaseError):
        _prepare_query(sql, None, "mysql", 101)


def test_physical_sqlite_read_only_even_without_ast_validation(client):
    engine = client._make_engine()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only = OFF")
            with pytest.raises(OperationalError):
                connection.execute(text("DELETE FROM products"))
    finally:
        engine.dispose()
    assert client.query("SELECT COUNT(*) FROM products")["rows"] == [[5]]


def test_sqlite_authorizer_denies_attach_even_if_validator_is_bypassed(client):
    with pytest.raises(DatabaseError):
        with client._connect() as connection:
            connection.exec_driver_sql("ATTACH ':memory:' AS other")


def test_joins_aggregates_ctes_and_strings_are_supported(client):
    result = client.query(
        "WITH sales AS (SELECT product_id, SUM(quantity) AS qty FROM orders GROUP BY product_id) "
        "SELECT p.name, s.qty FROM sales s JOIN products p ON p.id = s.product_id "
        "WHERE s.qty >= :minimum ORDER BY s.qty DESC, p.id",
        {"minimum": 3},
    )
    assert result["rows"] == [["保温杯", 4], ["机械键盘", 3], ["无线鼠标", 3]]
    assert client.query("SELECT ':not_a_parameter; DELETE FROM products' AS value")["rows"] == [
        [":not_a_parameter; DELETE FROM products"]
    ]
    assert (
        client.query(
            "SELECT name FROM products WHERE name = :name",
            {"name": "' OR 1=1; DELETE FROM products; --"},
        )["rows"]
        == []
    )


def test_sqlite_date_formats_preserve_dialect_meaning(client):
    assert client.query("SELECT STRFTIME('%Y-%m', '2026-09-22')")["rows"] == [["2026-09"]]
    assert client.query("SELECT STRFTIME('%H:%M:%S', '2026-09-22 13:14:15')")["rows"] == [
        ["13:14:15"]
    ]


def test_mysql_date_formats_are_bound_after_dialect_generation():
    sql, params = _prepare_query(
        "SELECT MONTHNAME('2026-09-22'), DATE_FORMAT('2026-09-22 13:14:15', '%H:%i:%s')",
        None,
        "mysql",
        101,
    )
    assert sql.count("DATE_FORMAT(") == 2
    assert "%M" in params.values()  # MySQL month name, not SQLGlot's canonical %B.
    assert any(value in {"%H:%i:%s", "%T"} for value in params.values())
    assert "%B" not in params.values()
    assert "%H:%M:%S" not in params.values()


@pytest.mark.parametrize(
    "sql",
    [
        "CALL private_proc('super_secret_value')",
        "GRANT SELECT ON shop.* TO 'reader' IDENTIFIED BY 'super_secret_value'",
    ],
)
def test_unsupported_sql_does_not_leak_into_parser_logs(caplog, sql):
    with pytest.raises(DatabaseError):
        _prepare_query(sql, None, "mysql", 101)
    assert "super_secret_value" not in caplog.text
    assert "private_proc" not in caplog.text
    assert "reader" not in caplog.text


def test_quoted_identifiers_with_colons_are_not_bind_parameters(client):
    with sqlite3.connect(client.settings.sqlite_path) as connection:
        connection.execute('CREATE TABLE ":table" (":column" TEXT)')
        connection.execute('INSERT INTO ":table" VALUES (?)', ("value:colon",))
    assert client.query('SELECT ":column" FROM ":table"')["rows"] == [["value:colon"]]


def test_mysql_quoted_identifier_cannot_turn_extra_parameter_into_sql():
    sql, params = _prepare_query(
        "SELECT 1 AS `:alias`",
        {"alias": "x` WHERE GET_LOCK(0x6d63702d74657374, 60) -- "},
        "mysql",
        101,
    )
    compiled = text(sql).compile(dialect=mysql.dialect(paramstyle="pyformat"))
    assert str(compiled) == "SELECT 1 AS `:alias` LIMIT 101"
    assert compiled.params == {}  # The extra value is never sent as an SQL fragment.
    assert "alias" in params


def test_row_caps_and_explicit_limits(client):
    assert client.query("SELECT id FROM products ORDER BY id", max_rows=2) == {
        "columns": ["id"],
        "rows": [[1], [2]],
        "row_count": 2,
        "truncated": True,
        "max_rows": 2,
    }
    assert client.query("SELECT id FROM products LIMIT 1", max_rows=2)["truncated"] is False
    assert client.query("SELECT id FROM products LIMIT :n", {"n": 0})["rows"] == []
    client.settings.db_max_rows = 1
    assert client.query("SELECT id FROM products", max_rows=200)["row_count"] == 1
    statement, _ = _prepare_query("SELECT id FROM products", {}, "mysql", 201)
    assert statement.endswith("LIMIT 201")


@pytest.mark.parametrize("max_rows", [0, -1, 201, 1.5, True])
def test_invalid_limits_rejected(client, max_rows):
    with pytest.raises(DatabaseError, match="max_rows"):
        client.query("SELECT 1", max_rows=max_rows)


def test_timeout_stops_expensive_query_and_next_query_works(client):
    client.settings.db_query_timeout_seconds = 0.001
    with pytest.raises(DatabaseError, match="timed out"):
        client.query(
            "WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n + 1 "
            "FROM numbers WHERE n < 100000000) SELECT SUM(n) FROM numbers"
        )
    client.settings.db_query_timeout_seconds = 10
    assert client.query("SELECT 1")["rows"] == [[1]]


def test_error_messages_do_not_include_sql_or_parameters(client):
    secret = "super-secret-password"
    with pytest.raises(DatabaseError) as error:
        client.query("SELECT missing_column FROM products WHERE name = :secret", {"secret": secret})
    assert secret not in str(error.value)
    assert "missing_column" not in str(error.value)
    with pytest.raises(DatabaseError, match="parameter"):
        client.query("SELECT :missing")


def test_describe_table_quotes_names_and_rejects_missing_tables(client):
    with sqlite3.connect(client.settings.sqlite_path) as connection:
        connection.execute('CREATE TABLE "name""; DROP TABLE products; --" (id INTEGER)')
    result = client.describe_table('name"; DROP TABLE products; --')
    assert result["columns"][0]["name"] == "id"
    with pytest.raises(DatabaseError, match="not found"):
        client.describe_table("nonexistent")
    assert client.query("SELECT COUNT(*) FROM products")["rows"] == [[5]]


def test_json_safe_values_and_bounded_large_cells(client):
    result = client.query("SELECT X'0102FF' AS blob")
    assert result["rows"][0][0] == {
        "encoding": "base64",
        "data": "AQL/",
        "bytes": 3,
        "truncated": False,
    }
    values = [
        _json_value(value)
        for value in [
            Decimal("123.4567890123456789"),
            datetime(2026, 9, 22, tzinfo=UTC),
            timedelta(days=1),
            b"x" * 9000,
            "x" * 9000,
            float("inf"),
        ]
    ]
    json.dumps(values, allow_nan=False)
    assert values[0] == "123.4567890123456789"
    assert values[3]["truncated"] is True
    assert values[4]["truncated"] is True


def test_mysql_url_special_password_and_timeouts(monkeypatch):
    create_engine = MagicMock()
    monkeypatch.setattr(database, "create_engine", create_engine)
    settings = DatabaseSettings(
        _env_file=None,
        db_engine="mysql",
        mysql_user="reader",
        mysql_database="shop",
        mysql_password=SecretStr("p@ss:/word?#%with space"),
        mysql_ssl_ca="certs/ca.pem",
        db_query_timeout_seconds=1.25,
    )
    DatabaseClient(settings)._make_engine()
    url = create_engine.call_args.args[0]
    assert url.password == "p@ss:/word?#%with space"
    assert url.database == "shop"
    assert "p@ss" not in str(url)
    arguments = create_engine.call_args.kwargs["connect_args"]
    assert arguments["connect_timeout"] == 2
    assert arguments["read_timeout"] == 2
    assert arguments["local_infile"] is False
    assert arguments["ssl_verify_cert"] is True
    assert arguments["ssl_verify_identity"] is True


def test_mysql_read_transaction_and_streamed_query(monkeypatch):
    settings = DatabaseSettings(
        _env_file=None,
        db_engine="mysql",
        mysql_database="shop",
        mysql_user="reader",
        db_query_timeout_seconds=1.25,
    )
    client = DatabaseClient(settings)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    result = connection.execution_options.return_value.execute.return_value.__enter__.return_value
    result.keys.return_value = ["value"]
    result.fetchmany.return_value = [(Decimal("1.25"),)]
    monkeypatch.setattr(client, "_make_engine", lambda: engine)
    assert client.query("SELECT price AS value FROM products", max_rows=2)["rows"] == [["1.25"]]
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SET SESSION MAX_EXECUTION_TIME = 1250",
        "START TRANSACTION READ ONLY",
    ]
    result.fetchmany.assert_called_once_with(3)
    result.fetchall.assert_not_called()
    connection.rollback.assert_called_once()
    engine.dispose.assert_called_once()


def test_database_settings_bounds_and_secrets():
    with pytest.raises(ValidationError):
        DatabaseSettings(_env_file=None, db_max_rows=201)
    with pytest.raises(ValidationError):
        DatabaseSettings(_env_file=None, db_query_timeout_seconds=0)
    settings = DatabaseSettings(_env_file=None, mysql_password="secret")
    assert "secret" not in repr(settings)
    assert DatabaseSettings(_env_file=None, mysql_ssl_ca="").mysql_ssl_ca is None


def test_mysql_connection_errors_are_sanitized(monkeypatch):
    client = DatabaseClient(DatabaseSettings(_env_file=None))
    engine = MagicMock()
    engine.connect.side_effect = OperationalError(
        "SELECT secret", {"password": "super-secret"}, Exception("private-host")
    )
    monkeypatch.setattr(client, "_make_engine", lambda: engine)
    with pytest.raises(DatabaseError) as error:
        client.database_info()
    assert "super-secret" not in str(error.value)
    assert "private-host" not in str(error.value)
    assert "SELECT secret" not in str(error.value)
