"""PostgreSQL: connection pool, schema, small query helpers.

The panel works as role "panel" with search_path panel,public: its own tables live in schema "panel",
Asterisk's tables (cdr, cel, queue_log) in "public" and are read-only for the panel.
"""
from __future__ import annotations

import json
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")


class DB:
    def __init__(self, url: str, size: int = 8):
        self.pool = ConnectionPool(url, min_size=1, max_size=size, open=True,
                                   kwargs={"row_factory": dict_row, "autocommit": True,
                                           "options": "-c search_path=panel,public -c timezone=UTC"})

    def migrate(self) -> None:
        with self.pool.connection() as c:
            c.execute(SCHEMA.read_text(encoding="utf-8"))

    def all(self, sql: str, args=()) -> list[dict]:
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> dict | None:
        with self.pool.connection() as c:
            return c.execute(sql, args).fetchone()

    def val(self, sql: str, args=()):
        row = self.one(sql, args)
        return next(iter(row.values())) if row else None

    def run(self, sql: str, args=()) -> int:
        with self.pool.connection() as c:
            return c.execute(sql, args).rowcount

    def event(self, kind: str, detail: dict | None = None, actor: str | None = None, actor_ip: str | None = None) -> None:
        self.run("INSERT INTO events (kind, detail, actor, actor_ip) VALUES (%s, %s, %s, %s)",
                 (kind, Jsonb(detail) if detail is not None else None, actor, actor_ip))

    def close(self) -> None:
        self.pool.close()


def jsonb(v) -> Jsonb:
    return Jsonb(v)


def loads(v):
    return json.loads(v) if isinstance(v, str) else v
