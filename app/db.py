import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from app.config import settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                drive_folder_id TEXT NOT NULL,
                is_shared_drive INTEGER NOT NULL DEFAULT 0,
                impersonate_user TEXT NOT NULL DEFAULT '',
                service_account_file TEXT NOT NULL DEFAULT '',
                b2_prefix TEXT NOT NULL,
                schedule_cron TEXT NOT NULL DEFAULT '0 2 * * *',
                export_formats TEXT NOT NULL DEFAULT 'docx,xlsx,pptx',
                max_delete INTEGER NOT NULL DEFAULT 500,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                last_status TEXT DEFAULT 'never',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backup_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                files_transferred INTEGER DEFAULT 0,
                files_checked INTEGER DEFAULT 0,
                files_deleted INTEGER DEFAULT 0,
                bytes_transferred INTEGER DEFAULT 0,
                duration_seconds REAL DEFAULT 0.0,
                error_message TEXT,
                log_output TEXT,
                FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
            );
            """
        )


def list_targets() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM targets ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


def get_target(target_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return dict(row) if row else None


def create_target(data: dict[str, Any]) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO targets (
                name, drive_folder_id, is_shared_drive, impersonate_user,
                service_account_file, b2_prefix, schedule_cron, export_formats,
                max_delete, enabled, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                data["drive_folder_id"],
                1 if data.get("is_shared_drive") else 0,
                data.get("impersonate_user", ""),
                data.get("service_account_file", ""),
                data["b2_prefix"],
                data.get("schedule_cron", "0 2 * * *"),
                data.get("export_formats", settings.gdrive_export_formats),
                int(data.get("max_delete", settings.max_delete_threshold)),
                1 if data.get("enabled", True) else 0,
                utc_now_iso(),
            ),
        )
        return int(cursor.lastrowid or 0)


def delete_target(target_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return cursor.rowcount > 0


def create_run(target_id: int, snapshot_id: str, dry_run: bool = False) -> int:
    now = utc_now_iso()
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO backup_runs (target_id, started_at, status, snapshot_id, dry_run)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (target_id, now, snapshot_id, 1 if dry_run else 0),
        )
        conn.execute(
            "UPDATE targets SET last_run_at = ?, last_status = 'running' WHERE id = ?",
            (now, target_id),
        )
        return int(cursor.lastrowid or 0)


def finish_run(
    run_id: int,
    target_id: int,
    status: str,
    files_transferred: int = 0,
    files_checked: int = 0,
    files_deleted: int = 0,
    bytes_transferred: int = 0,
    duration_seconds: float = 0.0,
    error_message: str | None = None,
    log_output: str = "",
) -> None:
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE backup_runs
            SET finished_at = ?,
                status = ?,
                files_transferred = ?,
                files_checked = ?,
                files_deleted = ?,
                bytes_transferred = ?,
                duration_seconds = ?,
                error_message = ?,
                log_output = ?
            WHERE id = ?
            """,
            (
                now,
                status,
                files_transferred,
                files_checked,
                files_deleted,
                bytes_transferred,
                round(duration_seconds, 2),
                error_message,
                log_output,
                run_id,
            ),
        )
        conn.execute(
            "UPDATE targets SET last_status = ?, last_run_at = ? WHERE id = ?",
            (status, now, target_id),
        )


def list_runs(limit: int = 30, target_id: int | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if target_id is not None:
            rows = conn.execute(
                """
                SELECT r.*, t.name as target_name
                FROM backup_runs r
                JOIN targets t ON t.id = r.target_id
                WHERE r.target_id = ?
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (target_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT r.*, t.name as target_name
                FROM backup_runs r
                JOIN targets t ON t.id = r.target_id
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT r.*, t.name as target_name
            FROM backup_runs r
            JOIN targets t ON t.id = r.target_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        return dict(row) if row else None
