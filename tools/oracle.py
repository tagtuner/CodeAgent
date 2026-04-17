from __future__ import annotations
import re
from datetime import date, datetime
from decimal import Decimal
from .base import BaseTool

try:
    import oracledb
    HAS_ORACLE = True
except ImportError:
    HAS_ORACLE = False

_CONNECTIONS: dict[str, dict] = {}
_DEFAULT_DB: str = ""


def set_oracle_connections(cfg: dict):
    global _CONNECTIONS, _DEFAULT_DB
    _DEFAULT_DB = cfg.get("default_connection", "")
    conns = cfg.get("connections", {})
    if conns:
        _CONNECTIONS = conns
    else:
        legacy = {}
        for k in ("default_host", "default_port", "default_service", "default_username", "default_password"):
            if cfg.get(k):
                legacy[k.replace("default_", "")] = cfg[k]
        if legacy:
            _CONNECTIONS = {"default": legacy}
            _DEFAULT_DB = "default"


def get_available_dbs() -> list[str]:
    return list(_CONNECTIONS.keys())


def _safe_val(v):
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _get_conn(db: str = ""):
    if not HAS_ORACLE:
        raise RuntimeError("oracledb not installed — pip install oracledb")

    db_name = db or _DEFAULT_DB
    if not db_name or db_name not in _CONNECTIONS:
        available = ", ".join(_CONNECTIONS.keys()) if _CONNECTIONS else "none"
        raise RuntimeError(
            f"Database '{db_name}' not found. Available connections: {available}. "
            f"Set in config.yaml under tools.oracle.connections"
        )

    c = _CONNECTIONS[db_name]
    host = c.get("host", "")
    port = c.get("port", "1521")
    service = c.get("service", "")
    username = c.get("username", "")
    password = c.get("password", "")

    missing = []
    if not host: missing.append("host")
    if not service: missing.append("service")
    if not username: missing.append("username")
    if not password: missing.append("password")
    if missing:
        label = c.get("label", db_name)
        raise RuntimeError(f"Connection '{label}' missing: {', '.join(missing)}")

    dsn = f"{host}:{port}/{service}"
    return oracledb.connect(user=username, password=password, dsn=dsn)


def _db_description() -> str:
    names = ", ".join(_CONNECTIONS.keys()) if _CONNECTIONS else "none configured"
    return f"Connection name (e.g. {names}). Leave empty for default: '{_DEFAULT_DB}'."


class OracleQueryTool(BaseTool):
    name = "oracle_query"
    description = "Execute a read-only SELECT query against Oracle database. Connection auto-resolved from config by name."
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SELECT SQL query to execute"},
            "db": {"type": "string", "description": "Connection name from config (e.g. dev, prod). Defaults to config default."},
        },
        "required": ["sql"],
    }

    async def execute(self, sql: str, db: str = "") -> str:
        trimmed = sql.strip().rstrip(";")
        if not re.match(r"^\s*select\b", trimmed, re.IGNORECASE):
            return "ERROR: Only SELECT queries allowed."
        try:
            conn = _get_conn(db)
            cur = conn.cursor()
            cur.execute(trimmed)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [[_safe_val(c) for c in r] for r in cur.fetchmany(200)]
            cur.close()
            conn.close()
            if not rows:
                return "Query returned 0 rows."
            lines = [f"Columns: {', '.join(cols)}", f"Rows: {len(rows)}", ""]
            widths = [max(len(str(c)), max((len(str(r[i] or "")) for r in rows[:20]), default=0)) for i, c in enumerate(cols)]
            lines.append(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
            lines.append("-+-".join("-" * w for w in widths))
            for row in rows[:50]:
                lines.append(" | ".join(str(v if v is not None else "NULL").ljust(w) for v, w in zip(row, widths)))
            if len(rows) > 50:
                lines.append(f"... ({len(rows) - 50} more rows)")
            return "\n".join(lines)
        except Exception as e:
            return f"Query error: {e}"


class OracleSchemaTool(BaseTool):
    name = "oracle_schema"
    description = "Get table columns, data types, and nullable info from Oracle database."
    parameters = {
        "type": "object",
        "properties": {
            "table_name": {"type": "string", "description": "Table name to describe"},
            "db": {"type": "string", "description": "Connection name from config (e.g. dev, prod). Defaults to config default."},
        },
        "required": ["table_name"],
    }

    async def execute(self, table_name: str, db: str = "") -> str:
        try:
            conn = _get_conn(db)
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name, data_type, data_length, nullable
                FROM all_tab_columns
                WHERE UPPER(table_name) = UPPER(:tn)
                ORDER BY column_id
            """, {"tn": table_name})
            cols = cur.fetchall()
            cur.close()
            conn.close()
            if not cols:
                return f"Table '{table_name}' not found or no columns."
            lines = [f"Table: {table_name.upper()} ({len(cols)} columns)", ""]
            for c in cols:
                nl = "NULL" if c[3] == "Y" else "NOT NULL"
                lines.append(f"  {c[0]:<35} {c[1]:<15} {nl}")
            return "\n".join(lines)
        except Exception as e:
            return f"Schema error: {e}"


class SqlValidateTool(BaseTool):
    name = "sql_validate"
    description = "Validate SQL syntax against Oracle using EXPLAIN PLAN."
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SQL to validate"},
            "db": {"type": "string", "description": "Connection name from config (e.g. dev, prod). Defaults to config default."},
        },
        "required": ["sql"],
    }

    async def execute(self, sql: str, db: str = "") -> str:
        trimmed = sql.strip().rstrip(";")
        try:
            conn = _get_conn(db)
            cur = conn.cursor()
            cur.execute(f"EXPLAIN PLAN FOR {trimmed}")
            cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'BASIC'))")
            plan = [r[0] for r in cur.fetchall()]
            cur.close()
            conn.close()
            return f"SQL is VALID.\n\n" + "\n".join(plan)
        except Exception as e:
            return f"SQL is INVALID: {e}"


class OracleExplainTool(BaseTool):
    name = "oracle_explain"
    description = "Run EXPLAIN PLAN on Oracle SQL and show execution plan for performance analysis."
    parameters = {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "SQL query to explain"},
            "db": {"type": "string", "description": "Connection name from config (e.g. dev, prod). Defaults to config default."},
        },
        "required": ["sql"],
    }

    async def execute(self, sql: str, db: str = "") -> str:
        trimmed = sql.strip().rstrip(";")
        try:
            conn = _get_conn(db)
            cur = conn.cursor()
            cur.execute(f"EXPLAIN PLAN FOR {trimmed}")
            cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'ALL'))")
            plan = [r[0] for r in cur.fetchall()]
            cur.close()
            conn.close()
            return "\n".join(plan)
        except Exception as e:
            return f"Explain error: {e}"
