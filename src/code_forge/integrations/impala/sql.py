"""Fail-closed SELECT validation and outer row cap using SQLGlot's Hive AST.

Hive is a conservative Impala subset, not an Impala syntax validator. Unsupported
syntax is rejected; Impala remains the authority for names, types and privileges.
"""

from __future__ import annotations

import re

from code_forge.contracts import DomainError, ErrorCode
from code_forge.integrations.impala.config import QueryPolicy


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise DomainError(ErrorCode.INVALID_REQUEST, "Invalid database/table identifier")
    return f"`{value}`"


def bounded_select(sql: str, rows: int, policy: QueryPolicy, database: str = "default") -> str:
    from sqlglot import exp, parse
    from sqlglot.dialects.hive import Hive
    from sqlglot.errors import ParseError, SqlglotError

    class StrictHive(Hive):
        class Parser(Hive.Parser):
            def _warn_unsupported(self) -> None:
                # SQLGlot otherwise logs the complete unsupported SQL at WARNING.
                raise ParseError("Unsupported query syntax")

    if not isinstance(sql, str) or not 1 <= len(sql.encode("utf-8")) <= 32000:
        raise DomainError(ErrorCode.INVALID_REQUEST, "SQL must be 1-32000 UTF-8 bytes")
    if type(rows) is not int or not 1 <= rows <= policy.max_rows:
        raise DomainError(ErrorCode.INVALID_REQUEST, "Query row limit exceeds policy")
    try:
        statements = parse(sql, read=StrictHive)
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
            raise ValueError("Only one SELECT statement is allowed")
        tree = statements[0]
        forbidden = (
            exp.DDL,
            exp.DML,
            exp.Command,
            exp.Into,
            exp.Lock,
        )
        if any(isinstance(node, forbidden) for node in tree.walk()):
            raise ValueError("Write or control statement in query")
        for node in tree.find_all(exp.Anonymous):
            if node.name.lower() not in {name.lower() for name in policy.allowed_udfs}:
                raise ValueError("Unapproved function")
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise ValueError("External/table functions are not allowed")
        for select in tree.find_all(exp.Select):
            if select.args.get("hint"):
                raise ValueError("Query hints are not accepted")
        for table_name, column in policy.required_filters:
            for table in tree.find_all(exp.Table):
                name = ".".join(part.name for part in table.parts).lower()
                if not table.db:
                    name = database.lower() + "." + name
                configured_name = table_name.lower()
                if "." not in configured_name:
                    configured_name = database.lower() + "." + configured_name
                if name != configured_name:
                    continue
                select = table.find_ancestor(exp.Select)
                where = select.args.get("where") if select else None

                def restrictive(predicate: exp.Expression) -> bool:
                    if isinstance(predicate, (exp.Where, exp.Paren)):
                        return restrictive(predicate.this)
                    if isinstance(predicate, exp.And):
                        return restrictive(predicate.this) or restrictive(predicate.expression)
                    if isinstance(predicate, exp.Or):
                        return restrictive(predicate.this) and restrictive(predicate.expression)
                    if not isinstance(
                        predicate, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In)
                    ):
                        return False
                    if isinstance(predicate, exp.In) and predicate.args.get("query"):
                        return False
                    columns = list(predicate.find_all(exp.Column))
                    if len(columns) != 1 or columns[0].name.lower() != column.lower():
                        return False
                    owner = columns[0].table.lower()
                    if owner and owner != table.alias_or_name.lower():
                        return False
                    if not owner and (select.args.get("joins") or []):
                        return False
                    # Only simple comparisons against constants qualify. Expressions such
                    # as dt=dt, dt=coalesce(dt,..), or dt='x' OR 1=1 do not.
                    leaves = [
                        node for node in predicate.walk() if not list(node.iter_expressions())
                    ]
                    return any(isinstance(node, exp.Literal) for node in leaves) and not any(
                        isinstance(node, (exp.Func, exp.Subquery, exp.Query))
                        for node in predicate.walk()
                    )

                if where is None or not restrictive(where):
                    raise ValueError("Required partition filter is missing")
        limit = tree.args.get("limit")
        amount = rows + 1  # One sentinel row detects truncation without counting the full result.
        if limit is not None:
            value = limit.expression
            if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0:
                raise ValueError("LIMIT must be a nonnegative integer")
            amount = min(amount, int(value.this))
        return tree.limit(amount).sql(dialect="hive", comments=False)
    except (SqlglotError, ValueError, AttributeError) as exc:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            "Query rejected: require one read-only SELECT, literal LIMIT, approved functions "
            "and configured partition filters",
        ) from exc
