"""SQLite 持久化层（标准库 sqlite3）。

- 所有表只追加 / 就地做受控状态迁移，契约内容永不修改；
- 评审记录是当时声明、豁免与结论的不可变快照，撤回候选不影响后续评审；
- 进程内写锁串行化写入，WAL 模式保证读写并发。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id                 TEXT PRIMARY KEY,
    service_id         TEXT NOT NULL REFERENCES services(id),
    seq                INTEGER NOT NULL,
    parent_id          TEXT REFERENCES versions(id),
    status             TEXT NOT NULL,
    contract_json      TEXT NOT NULL,
    contract_hash      TEXT NOT NULL,
    submitter          TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    published_at       TEXT,
    publish_record_id  TEXT,
    withdraw_reason    TEXT,
    UNIQUE(service_id, seq)
);

CREATE TABLE IF NOT EXISTS reviews (
    id             TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES versions(id),
    service_id     TEXT NOT NULL REFERENCES services(id),
    baseline_id    TEXT REFERENCES versions(id),
    decision       TEXT NOT NULL,           -- CANDIDATE / REJECTED
    reason_stage   TEXT NOT NULL,           -- admission / publish_recheck
    evidence_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_version ON reviews(version_id);

CREATE TABLE IF NOT EXISTS publish_records (
    id            TEXT PRIMARY KEY,
    service_id    TEXT NOT NULL REFERENCES services(id),
    version_id    TEXT NOT NULL REFERENCES versions(id),
    evidence_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS declarations (
    id            TEXT PRIMARY KEY,
    service_id    TEXT NOT NULL REFERENCES services(id),
    consumer      TEXT NOT NULL,
    scope_json    TEXT NOT NULL,
    deadline      TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decl_lookup
    ON declarations(service_id, consumer, active);

CREATE TABLE IF NOT EXISTS exemptions (
    id                 TEXT PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,
    service_id         TEXT NOT NULL REFERENCES services(id),
    affected_consumers TEXT NOT NULL,      -- JSON 数组：受影响调用方白名单
    restricted_changes TEXT,               -- JSON 数组或 NULL：限定变更
    reason             TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,      -- 强制到期时间
    active             INTEGER NOT NULL DEFAULT 1,
    revoked_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_exempt_service ON exemptions(service_id);
"""

_write_lock = threading.RLock()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get(
        "CONTRACT_REGISTRY_DB", str(Path(__file__).resolve().parent.parent
                                    / "data" / "registry.db"))
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()


def write_lock() -> threading.RLock:
    return _write_lock


# ---- 小型行映射工具 -------------------------------------------------------

def version_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "service_id": row["service_id"], "seq": row["seq"],
        "parent_id": row["parent_id"], "status": row["status"],
        "contract": json.loads(row["contract_json"]),
        "contract_hash": row["contract_hash"],
        "submitter": row["submitter"], "created_at": row["created_at"],
        "published_at": row["published_at"],
        "publish_record_id": row["publish_record_id"],
        "withdraw_reason": row["withdraw_reason"],
    }


def declaration_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "service_id": row["service_id"],
        "consumer": row["consumer"], "scope": json.loads(row["scope_json"]),
        "deadline": row["deadline"], "active": bool(row["active"]),
        "superseded_by": row["superseded_by"],
        "created_at": row["created_at"],
    }


def exemption_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "code": row["code"], "service_id": row["service_id"],
        "affected_consumers": json.loads(row["affected_consumers"]),
        "restricted_changes": (json.loads(row["restricted_changes"])
                               if row["restricted_changes"] else None),
        "reason": row["reason"], "created_by": row["created_by"],
        "created_at": row["created_at"], "expires_at": row["expires_at"],
        "active": bool(row["active"]), "revoked_at": row["revoked_at"],
    }
