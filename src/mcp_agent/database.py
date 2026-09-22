"""Bounded, read-only SQLite and MySQL access for the database MCP server."""

from __future__ import annotations

import base64
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import sqlglot
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

HARD_MAX_ROWS = 200
MAX_CELL_SIZE = 8_192


class DatabaseError(ValueError):
    """A public error whose message contains no SQL, parameters, or credentials."""


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    db_engine: Literal["sqlite", "mysql"] = "sqlite"
    sqlite_path: Path = Path("data/agent_demo.db")
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: SecretStr = SecretStr("")
    mysql_ssl_ca: Path | None = None
    db_query_timeout_seconds: float = Field(default=10, gt=0, le=3600, allow_inf_nan=False)
    db_max_rows: int = Field(default=200, ge=1, le=HARD_MAX_ROWS)

    @field_validator("mysql_ssl_ca", mode="before")
    @classmethod
    def empty_ssl_ca_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


# Unknown functions are denied: MySQL stored functions and UDFs can have side effects.
# Names are SQLGlot's canonical function names, not arbitrary user spelling.
_SAFE_FUNCTIONS = frozenset(
    """ABS AVG CEIL CEILING FLOOR ROUND SIGN SQRT POW POWER EXP LN LOG LOG10 LOG2 MOD
    COUNT SUM MIN MAX COALESCE NULLIF IF IFNULL IIF CAST TRY_CAST CONVERT
    LOWER UPPER LENGTH CHAR_LENGTH CHARACTER_LENGTH OCTET_LENGTH CONCAT CONCAT_WS
    SUBSTRING SUBSTR LEFT RIGHT TRIM LTRIM RTRIM REPLACE REPEAT REVERSE
    LPAD RPAD INSTR LOCATE POSITION STRPOS ASCII CHAR CHR HEX UNHEX HEX_STRING
    DATE DATETIME TIME STRFTIME JULIANDAY UNIXEPOCH DATE_ADD DATE_SUB DATE_DIFF
    DATE_FORMAT STR_TO_DATE TIME_TO_STR STR_TO_TIME TS_OR_DS_TO_DATE TS_OR_DS_TO_TIME
    TS_OR_DS_TO_TIMESTAMP
    CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP LOCALTIME LOCALTIMESTAMP NOW
    YEAR MONTH DAY DAY_OF_MONTH DAY_OF_WEEK DAY_OF_YEAR HOUR MINUTE SECOND
    EXTRACT TIMESTAMP TIMESTAMP_DIFF TIMESTAMP_ADD TIMESTAMP_SUB UNIX_TIMESTAMP
    FROM_UNIXTIME WEEK WEEKOFYEAR QUARTER MONTHNAME DAYNAME LAST_DAY
    ROW_NUMBER RANK DENSE_RANK PERCENT_RANK CUME_DIST NTILE LAG LEAD
    FIRST_VALUE LAST_VALUE NTH_VALUE GROUP_CONCAT ARRAY_AGG
    JSON_EXTRACT JSON_ARRAY JSON_OBJECT JSON_LENGTH JSON_TYPE JSON_VALID
    JSON_UNQUOTE JSON_VALUE JSON_PATH JSON_PATH_ROOT JSON_PATH_KEY JSON_PATH_SUBSCRIPT
    GREATEST LEAST STDDEV STDDEV_POP STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP
    CASE EXISTS LIKE GLOB
    """.split()
)
_FORBIDDEN_NODES = frozenset(
    """into lock hint command pragma attach detach transaction commit rollback
    grant revoke use set execute describe show copy load data propertyeq parameter
    sessionparameter nextvalue tablefromrows pivot unpivot lateral matchrecognize
    """.split()
)
_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _prepare_query(
    sql: str, parameters: dict[str, Any] | None, dialect: str, row_limit: int
) -> tuple[str, dict[str, Any]]:
    """Validate the complete tree and emit fresh SQL with literal strings bound."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 50_000:
        raise DatabaseError("SQL must contain one SELECT statement, at most 50000 characters.")
    # Executable comments and optimizer hints have semantics outside the SQL AST.
    if "/*!" in sql or "/*+" in sql or re.search(r"/\*M!", sql, re.IGNORECASE):
        raise DatabaseError("Executable comments and optimizer hints are not allowed.")
    params = dict(parameters or {})
    for name, value in params.items():
        if not isinstance(name, str) or not _PARAMETER_NAME.fullmatch(name):
            raise DatabaseError("Use named parameters such as :product_id.")
        if not isinstance(value, str | int | float | bool | type(None)):
            raise DatabaseError("SQL parameters must be scalar JSON values.")
        if isinstance(value, float) and not math.isfinite(value):
            raise DatabaseError("SQL parameters must contain finite numbers.")
    try:
        statements = [
            node
            # SQLGlot's unsupported-command warning normally includes raw SQL.
            # Suppress its SQL context for this parse without muting global logs.
            for node in sqlglot.parse(sql, read=dialect, error_message_context=0)
            if node is not None and not isinstance(node, exp.Semicolon)
        ]
        if len(statements) != 1 or not isinstance(
            statements[0], exp.Select | exp.Union | exp.Intersect | exp.Except
        ):
            raise DatabaseError("Only one read-only SELECT or WITH ... SELECT is allowed.")
        tree = statements[0]
        for node in tree.walk():
            if isinstance(node, exp.DDL | exp.DML) or node.key in _FORBIDDEN_NODES:
                raise DatabaseError("Only read-only queries without writes or locks are allowed.")
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
                if name not in _SAFE_FUNCTIONS:
                    raise DatabaseError("This SQL function is not in the read-only allowlist.")
                if any(isinstance(parent, exp.Dot) for parent in node.iter_expressions()):
                    raise DatabaseError("Qualified SQL functions are not allowed.")
                parent = node.parent
                while parent is not None and not isinstance(parent, exp.Query):
                    if isinstance(parent, exp.Dot):
                        raise DatabaseError("Qualified SQL functions are not allowed.")
                    parent = parent.parent
            if isinstance(node, exp.Placeholder):
                if not _PARAMETER_NAME.fullmatch(node.name) or node.name not in params:
                    raise DatabaseError("Provide every SQL parameter using :name and parameters.")
        # Apply the cap in the database as well as fetchmany; unbuffered MySQL
        # cursor.close() otherwise drains an arbitrarily large result set.
        original_limit = tree.args.get("limit")
        if original_limit is not None:
            limit = original_limit.expression
            if isinstance(limit, exp.Literal) and limit.is_int:
                original = int(limit.this)
            elif isinstance(limit, exp.Placeholder):
                original = params[limit.name]
            else:
                raise DatabaseError("LIMIT must be a nonnegative integer or named parameter.")
            if type(original) is not int or original < 0:
                raise DatabaseError("LIMIT must be a nonnegative integer.")
            row_limit = min(row_limit, original)
        tree = tree.limit(row_limit, copy=False)
        # Render FIRST: SQLGlot stores formats canonically (MONTHNAME becomes
        # DATE_FORMAT(..., '%B') internally, then MySQL output uses '%M'). Binding
        # AST literals before generation would skip those dialect conversions.
        rendered = tree.sql(dialect=dialect, comments=False)
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(rendered)
        pieces: list[str] = []
        previous_end = 0
        for index, token in enumerate(tokens):
            replacement = None
            if token.token_type in {TokenType.STRING, TokenType.NATIONAL_STRING}:
                name = f"_mcp_literal_{index}"
                while name in params:
                    name += "_"
                params[name] = token.text
                replacement = f":{name}"
            elif token.token_type == TokenType.IDENTIFIER:
                # SQLAlchemy text() scans even quoted identifiers for :bind.
                # Its compiler unescapes these colons before sending SQL.
                replacement = rendered[token.start : token.end + 1].replace(":", r"\:")
            if replacement is not None:
                pieces.extend([rendered[previous_end : token.start], replacement])
                previous_end = token.end + 1
        pieces.append(rendered[previous_end:])
        return "".join(pieces), params
    except DatabaseError:
        raise
    except (SqlglotError, ValueError, TypeError, RecursionError):
        raise DatabaseError("SQL could not be parsed as a supported read-only query.") from None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | datetime_time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return {
            "encoding": "base64",
            "data": base64.b64encode(raw[:MAX_CELL_SIZE]).decode("ascii"),
            "bytes": len(raw),
            "truncated": len(raw) > MAX_CELL_SIZE,
        }
    result = str(value)
    if len(result) > MAX_CELL_SIZE:
        return {"text": result[:MAX_CELL_SIZE], "characters": len(result), "truncated": True}
    return result


def _sqlite_authorizer(
    action: int,
    first: str | None,
    second: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    if action in {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_TRANSACTION,
    }:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return (
            sqlite3.SQLITE_OK if (second or "").upper() in _SAFE_FUNCTIONS else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_PRAGMA and first in {"table_info", "table_xinfo"}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class DatabaseClient:
    """One configured database; every operation opens and closes a read connection."""

    def __init__(self, settings: DatabaseSettings | None = None):
        self.settings = settings or DatabaseSettings()

    def _make_engine(self) -> Engine:
        settings = self.settings
        if settings.db_engine == "sqlite":
            path = settings.sqlite_path.expanduser().resolve()
            if not path.is_file():
                raise DatabaseError("SQLite database does not exist. Run mcp-agent db-init first.")

            def connect_sqlite() -> sqlite3.Connection:
                connection = sqlite3.connect(
                    path.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=settings.db_query_timeout_seconds,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                if hasattr(connection, "enable_load_extension"):
                    connection.enable_load_extension(False)
                return connection

            return create_engine(
                "sqlite+pysqlite://",
                creator=connect_sqlite,
                poolclass=NullPool,
                hide_parameters=True,
            )
        if not settings.mysql_database.strip() or not settings.mysql_user.strip():
            raise DatabaseError("Set MYSQL_DATABASE and MYSQL_USER to a SELECT-only account.")
        url = URL.create(
            "mysql+pymysql",
            username=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            host=settings.mysql_host,
            port=settings.mysql_port,
            database=settings.mysql_database,
            query={"charset": "utf8mb4"},
        )
        timeout = max(1, math.ceil(settings.db_query_timeout_seconds))
        connect_args: dict[str, Any] = {
            "connect_timeout": min(timeout, 31536000),
            "read_timeout": timeout,
            "write_timeout": timeout,
            "local_infile": False,
        }
        if settings.mysql_ssl_ca:
            connect_args["ssl_ca"] = str(settings.mysql_ssl_ca)
            connect_args["ssl_verify_cert"] = True
            connect_args["ssl_verify_identity"] = True
        return create_engine(
            url,
            connect_args=connect_args,
            poolclass=NullPool,
            hide_parameters=True,
        )

    @contextmanager
    def _connect(self) -> Iterator[Connection]:
        engine = None
        try:
            engine = self._make_engine()
            with engine.connect() as connection:
                raw = None
                if self.settings.db_engine == "sqlite":
                    raw = connection.connection.driver_connection
                    deadline = time.monotonic() + self.settings.db_query_timeout_seconds
                    raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    raw.set_authorizer(_sqlite_authorizer)
                else:
                    milliseconds = max(1, math.ceil(self.settings.db_query_timeout_seconds * 1000))
                    connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {milliseconds}")
                    connection.exec_driver_sql("START TRANSACTION READ ONLY")
                try:
                    yield connection
                finally:
                    if raw is not None:
                        raw.set_authorizer(None)
                        raw.set_progress_handler(None, 0)
                    connection.rollback()
        except DatabaseError:
            raise
        except (SQLAlchemyError, sqlite3.Error, OSError, ValueError):
            raise DatabaseError(
                "Database operation failed or timed out. Check connection settings, "
                "read permissions, table/column names, and query complexity. "
                "MySQL requires a SELECT-only account and MAX_EXECUTION_TIME support."
            ) from None
        finally:
            if engine is not None:
                engine.dispose()

    def _read(
        self, connection: Connection, statement: str, parameters: dict[str, Any], limit: int
    ) -> dict[str, Any]:
        with connection.execution_options(stream_results=True, max_row_buffer=limit + 1).execute(
            text(statement), parameters
        ) as result:
            columns = [str(column) for column in result.keys()]
            rows = result.fetchmany(limit + 1)
        return {
            "columns": columns,
            "rows": [[_json_value(value) for value in row] for row in rows[:limit]],
            "row_count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "max_rows": limit,
        }

    def database_info(self) -> dict[str, Any]:
        with self._connect() as connection:
            self._read(connection, "SELECT 1", {}, 1)
        settings = self.settings
        return {
            "dialect": settings.db_engine,
            "database": (
                settings.sqlite_path.name
                if settings.db_engine == "sqlite"
                else settings.mysql_database
            ),
            "read_only": True,
            "query_timeout_seconds": settings.db_query_timeout_seconds,
            "max_rows": settings.db_max_rows,
        }

    def list_tables(self) -> dict[str, Any]:
        if self.settings.db_engine == "sqlite":
            statement = (
                "SELECT name, type FROM sqlite_schema "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name LIMIT :cap"
            )
            parameters = {"cap": HARD_MAX_ROWS + 1}
        else:
            statement = (
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :database ORDER BY TABLE_NAME LIMIT :cap"
            )
            parameters = {"database": self.settings.mysql_database, "cap": HARD_MAX_ROWS + 1}
        with self._connect() as connection:
            result = self._read(connection, statement, parameters, HARD_MAX_ROWS)
        return {
            "tables": [{"name": row[0], "type": row[1].lower()} for row in result["rows"]],
            "truncated": result["truncated"],
        }

    def describe_table(self, table_name: str) -> dict[str, Any]:
        if not table_name or len(table_name) > 128 or "\x00" in table_name:
            raise DatabaseError("Provide a table name from list_tables, at most 128 characters.")
        with self._connect() as connection:
            if self.settings.db_engine == "sqlite":
                found = self._read(
                    connection,
                    "SELECT name FROM sqlite_schema WHERE name = :name "
                    "AND type IN ('table', 'view') LIMIT 1",
                    {"name": table_name},
                    1,
                )
                if not found["rows"]:
                    raise DatabaseError("Table or view was not found in the configured database.")
                quoted = '"' + table_name.replace('"', '""') + '"'
                with connection.exec_driver_sql(f"PRAGMA table_info({quoted})") as result:
                    rows = result.fetchmany(HARD_MAX_ROWS + 1)
                columns = [
                    {
                        "name": row[1],
                        "type": row[2],
                        "nullable": not bool(row[3] or row[5]),
                        "primary_key": bool(row[5]),
                    }
                    for row in rows[:HARD_MAX_ROWS]
                ]
                truncated = len(rows) > HARD_MAX_ROWS
            else:
                result = self._read(
                    connection,
                    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :database "
                    "AND TABLE_NAME = :table ORDER BY ORDINAL_POSITION LIMIT :cap",
                    {
                        "database": self.settings.mysql_database,
                        "table": table_name,
                        "cap": HARD_MAX_ROWS + 1,
                    },
                    HARD_MAX_ROWS,
                )
                if not result["rows"]:
                    raise DatabaseError("Table or view was not found in the configured database.")
                columns = [
                    {
                        "name": row[0],
                        "type": row[1],
                        "nullable": row[2] == "YES",
                        "primary_key": row[3] == "PRI",
                    }
                    for row in result["rows"]
                ]
                truncated = result["truncated"]
        return {"table": table_name, "columns": columns, "truncated": truncated}

    def query(
        self, sql: str, parameters: dict[str, Any] | None = None, max_rows: int = 100
    ) -> dict[str, Any]:
        if type(max_rows) is not int or not 1 <= max_rows <= HARD_MAX_ROWS:
            raise DatabaseError("max_rows must be an integer from 1 to 200.")
        limit = min(max_rows, self.settings.db_max_rows)
        statement, params = _prepare_query(sql, parameters, self.settings.db_engine, limit + 1)
        with self._connect() as connection:
            return self._read(connection, statement, params, limit)


def initialize_demo(path: Path) -> dict[str, Any]:
    """Explicitly create a new example SQLite file; never overwrite an existing file."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        raise DatabaseError("Database file already exists; db-init never overwrites it.") from None
    try:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                    price NUMERIC NOT NULL CHECK(price >= 0),
                    stock INTEGER NOT NULL CHECK(stock >= 0)
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0), ordered_at TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "机械键盘", "电脑配件", 299.00, 40),
                    (2, "无线鼠标", "电脑配件", 129.00, 75),
                    (3, "USB-C 扩展坞", "电脑配件", 199.00, 30),
                    (4, "笔记本支架", "办公用品", 89.00, 60),
                    (5, "保温杯", "生活用品", 79.00, 100),
                ],
            )
            connection.executemany(
                "INSERT INTO orders VALUES (?, ?, ?, ?)",
                [
                    (1, 1, 2, "2026-09-01T09:30:00"),
                    (2, 2, 3, "2026-09-02T14:15:00"),
                    (3, 3, 1, "2026-09-03T11:00:00"),
                    (4, 1, 1, "2026-09-04T16:45:00"),
                    (5, 5, 4, "2026-09-05T10:20:00"),
                    (6, 4, 2, "2026-09-06T15:00:00"),
                ],
            )
    except (sqlite3.Error, OSError):
        raise DatabaseError("Could not initialize the new SQLite example database.") from None
    finally:
        # sqlite3's transaction context does not close the handle on Windows.
        if "connection" in locals():
            connection.close()
    return {
        "path": str(path),
        "tables": ["products", "orders"],
        "rows": {"products": 5, "orders": 6},
    }
