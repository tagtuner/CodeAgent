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


def _safe_val(v):
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _get_conn(host: str, port: str, service: str, username: str, password: str):
    if not HAS_ORACLE:
        raise RuntimeError("oracledb not installed — pip install oracledb")
    dsn = f"{host}:{port}/{service}"
    return oracledb.connect(user=username, password=password, dsn=dsn)


class OracleQueryTool(BaseTool):
    name = "oracle_query"
    description = "Execute a read-only SELECT query against Oracle database. Only SELECT statements allowed. Results limited to 200 rows."
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Oracle DB host"},
            "port": {"type": "string", "description": "Oracle DB port"},
            "service": {"type": "string", "description": "Oracle service name"},
            "username": {"type": "string", "description": "Oracle username"},
            "password": {"type": "string", "description": "Oracle password"},
            "sql": {"type": "string", "description": "SELECT SQL query"},
        },
        "required": ["host", "port", "service", "username", "password", "sql"],
    }

    async def execute(self, host: str, port: str, service: str, username: str, password: str, sql: str) -> str:
        trimmed = sql.strip().rstrip(";")
        if not re.match(r"^\s*select\b", trimmed, re.IGNORECASE):
            return "ERROR: Only SELECT queries allowed."
        try:
            conn = _get_conn(host, port, service, username, password)
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
            "host": {"type": "string", "description": "Oracle DB host"},
            "port": {"type": "string", "description": "Oracle DB port"},
            "service": {"type": "string", "description": "Oracle service name"},
            "username": {"type": "string", "description": "Oracle username"},
            "password": {"type": "string", "description": "Oracle password"},
            "table_name": {"type": "string", "description": "Table name to describe"},
        },
        "required": ["host", "port", "service", "username", "password", "table_name"],
    }

    async def execute(self, host: str, port: str, service: str, username: str, password: str, table_name: str) -> str:
        try:
            conn = _get_conn(host, port, service, username, password)
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
    description = "Validate SQL syntax against Oracle using EXPLAIN PLAN. Returns whether SQL is valid or the error."
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Oracle DB host"},
            "port": {"type": "string", "description": "Oracle DB port"},
            "service": {"type": "string", "description": "Oracle service name"},
            "username": {"type": "string", "description": "Oracle username"},
            "password": {"type": "string", "description": "Oracle password"},
            "sql": {"type": "string", "description": "SQL to validate"},
        },
        "required": ["host", "port", "service", "username", "password", "sql"],
    }

    async def execute(self, host: str, port: str, service: str, username: str, password: str, sql: str) -> str:
        trimmed = sql.strip().rstrip(";")
        try:
            conn = _get_conn(host, port, service, username, password)
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
            "host": {"type": "string", "description": "Oracle DB host"},
            "port": {"type": "string", "description": "Oracle DB port"},
            "service": {"type": "string", "description": "Oracle service name"},
            "username": {"type": "string", "description": "Oracle username"},
            "password": {"type": "string", "description": "Oracle password"},
            "sql": {"type": "string", "description": "SQL query to explain"},
        },
        "required": ["host", "port", "service", "username", "password", "sql"],
    }

    async def execute(self, host: str, port: str, service: str, username: str, password: str, sql: str) -> str:
        trimmed = sql.strip().rstrip(";")
        try:
            conn = _get_conn(host, port, service, username, password)
            cur = conn.cursor()
            cur.execute(f"EXPLAIN PLAN FOR {trimmed}")
            cur.execute("SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'ALL'))")
            plan = [r[0] for r in cur.fetchall()]
            cur.close()
            conn.close()
            return "\n".join(plan)
        except Exception as e:
            return f"Explain error: {e}"
