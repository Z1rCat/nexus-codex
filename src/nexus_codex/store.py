from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    job_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_path TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    workspace_path TEXT,
                    branch_name TEXT,
                    base_head TEXT,
                    codex_thread_id TEXT,
                    parent_run_id INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT,
                    artifact_dir TEXT NOT NULL,
                    final_message_path TEXT,
                    events_path TEXT,
                    stderr_path TEXT,
                    verifier_log_path TEXT,
                    verifier_failed_command TEXT,
                    job_snapshot TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id INTEGER,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    dedupe_key TEXT,
                    title TEXT,
                    source_ref TEXT,
                    payload_path TEXT,
                    parent_task_id INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    last_run_id INTEGER,
                    error_message TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    next_wake_at TEXT,
                    wake_count INTEGER NOT NULL DEFAULT 0,
                    idle_count INTEGER NOT NULL DEFAULT 0,
                    last_task_id INTEGER,
                    last_run_id INTEGER,
                    last_outcome TEXT,
                    source_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                    lease_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_state (
                    job_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    daily_wake_date TEXT,
                    daily_wake_count INTEGER NOT NULL DEFAULT 0,
                    last_run_id INTEGER,
                    last_status TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    job_id TEXT PRIMARY KEY,
                    repo_path TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    branch_name TEXT,
                    base_head TEXT,
                    codex_thread_id TEXT NOT NULL,
                    last_run_id INTEGER,
                    last_status TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT,
                    fingerprint TEXT,
                    cluster_key TEXT,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    tags_json TEXT NOT NULL,
                    source_task_id INTEGER,
                    source_run_id INTEGER,
                    source_session_id TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "base_head" not in existing_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN base_head TEXT")
            if "codex_thread_id" not in existing_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN codex_thread_id TEXT")
            if "task_id" not in existing_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN task_id INTEGER")
            task_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "goal_id" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN goal_id INTEGER")
            if "priority" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 100")
            if "dedupe_key" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN dedupe_key TEXT")
            memory_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(memories)").fetchall()
            }
            if "fingerprint" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN fingerprint TEXT")
            if "cluster_key" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN cluster_key TEXT")
            if "occurrence_count" not in memory_columns:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"
                )
            if "last_seen_at" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN last_seen_at TEXT")

    def create_task(
        self,
        *,
        goal_id: int | None = None,
        job_id: str,
        kind: str,
        trigger: str,
        status: str,
        priority: int = 100,
        dedupe_key: str | None = None,
        title: str | None = None,
        source_ref: str | None = None,
        payload_path: Path | None = None,
        parent_task_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (
                    goal_id,
                    job_id,
                    kind,
                    trigger,
                    status,
                    priority,
                    dedupe_key,
                    title,
                    source_ref,
                    payload_path,
                    parent_task_id,
                    created_at,
                    updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    job_id,
                    kind,
                    trigger,
                    status,
                    priority,
                    dedupe_key,
                    title,
                    source_ref,
                    str(payload_path) if payload_path else None,
                    parent_task_id,
                    now,
                    now,
                    json.dumps(metadata or {}, indent=2, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def create_goal(
        self,
        *,
        job_id: str,
        status: str,
        title: str,
        metadata: dict[str, Any] | None = None,
        next_wake_at: str | None = None,
    ) -> int:
        now = utc_now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO goals (
                    job_id,
                    status,
                    title,
                    next_wake_at,
                    created_at,
                    updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    status,
                    title,
                    next_wake_at,
                    now,
                    now,
                    json.dumps(metadata or {}, indent=2, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def create_run(
        self,
        *,
        task_id: int | None,
        job_id: str,
        phase: str,
        trigger: str,
        status: str,
        config_path: Path,
        repo_path: Path,
        artifact_dir: Path,
        job_snapshot: dict[str, Any],
        parent_run_id: int | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runs (
                    task_id,
                    job_id,
                    phase,
                    trigger,
                    status,
                    config_path,
                    repo_path,
                    artifact_dir,
                    created_at,
                    parent_run_id,
                    job_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    job_id,
                    phase,
                    trigger,
                    status,
                    str(config_path),
                    str(repo_path),
                    str(artifact_dir),
                    utc_now().isoformat(),
                    parent_run_id,
                    json.dumps(job_snapshot, indent=2, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def update_task_status(
        self,
        task_id: int,
        *,
        status: str,
        last_run_id: int | None = None,
        error_message: str | None = None,
        started: bool = False,
        finished: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    last_run_id = COALESCE(?, last_run_id),
                    error_message = ?,
                    started_at = CASE
                        WHEN ? THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN ? THEN ?
                        ELSE finished_at
                    END,
                    metadata_json = COALESCE(?, metadata_json),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    last_run_id,
                    error_message,
                    1 if started else 0,
                    now,
                    1 if finished else 0,
                    now,
                    (
                        json.dumps(metadata, indent=2, sort_keys=True)
                        if metadata is not None
                        else None
                    ),
                    now,
                    task_id,
                ),
            )

    def mark_running(
        self,
        run_id: int,
        *,
        workspace_path: Path,
        branch_name: str | None,
        base_head: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, started_at = ?, workspace_path = ?, branch_name = ?, base_head = ?
                WHERE id = ?
                """,
                (
                    "running",
                    utc_now().isoformat(),
                    str(workspace_path),
                    branch_name,
                    base_head,
                    run_id,
                ),
            )

    def bind_codex_thread_id(self, run_id: int, thread_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET codex_thread_id = ? WHERE id = ?",
                (thread_id, run_id),
            )

    def bind_artifact_dir(self, run_id: int, artifact_dir: Path) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET artifact_dir = ? WHERE id = ?",
                (str(artifact_dir), run_id),
            )

    def mark_completed(
        self,
        run_id: int,
        *,
        status: str,
        final_message_path: Path | None,
        events_path: Path | None,
        stderr_path: Path | None,
        verifier_log_path: Path | None = None,
        verifier_failed_command: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    finished_at = ?,
                    final_message_path = ?,
                    events_path = ?,
                    stderr_path = ?,
                    verifier_log_path = ?,
                    verifier_failed_command = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now().isoformat(),
                    str(final_message_path) if final_message_path else None,
                    str(events_path) if events_path else None,
                    str(stderr_path) if stderr_path else None,
                    str(verifier_log_path) if verifier_log_path else None,
                    verifier_failed_command,
                    error_message,
                    run_id,
                ),
            )

    def update_status(self, run_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    def get_run(self, run_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run {run_id} does not exist")
        return row

    def get_task(self, task_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} does not exist")
        return row

    def get_goal(self, goal_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise KeyError(f"goal {goal_id} does not exist")
        return row

    def list_tasks(self, limit: int = 20, *, job_id: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if job_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM tasks
                    WHERE job_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM tasks
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return rows

    def count_tasks_by_status(self, job_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                WHERE job_id = ?
                GROUP BY status
                """,
                (job_id,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_goals(self, limit: int = 20, *, job_id: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if job_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM goals
                    WHERE job_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM goals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return rows

    def get_active_goal(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM goals
                WHERE job_id = ?
                  AND status NOT IN ('completed', 'stopped')
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row

    def get_latest_goal(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM goals
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row

    def update_goal(
        self,
        goal_id: int,
        *,
        status: str | None = None,
        next_wake_at: str | None = None,
        wake_count: int | None = None,
        idle_count: int | None = None,
        last_task_id: int | None = None,
        last_run_id: int | None = None,
        last_outcome: str | None = None,
        source_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        completed: bool = False,
    ) -> None:
        now = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE goals
                SET status = COALESCE(?, status),
                    next_wake_at = ?,
                    wake_count = COALESCE(?, wake_count),
                    idle_count = COALESCE(?, idle_count),
                    last_task_id = COALESCE(?, last_task_id),
                    last_run_id = COALESCE(?, last_run_id),
                    last_outcome = COALESCE(?, last_outcome),
                    source_session_id = COALESCE(?, source_session_id),
                    metadata_json = COALESCE(?, metadata_json),
                    updated_at = ?,
                    completed_at = CASE
                        WHEN ? THEN COALESCE(completed_at, ?)
                        ELSE completed_at
                    END
                WHERE id = ?
                """,
                (
                    status,
                    next_wake_at,
                    wake_count,
                    idle_count,
                    last_task_id,
                    last_run_id,
                    last_outcome,
                    source_session_id,
                    (
                        json.dumps(metadata, indent=2, sort_keys=True)
                        if metadata is not None
                        else None
                    ),
                    now,
                    1 if completed else 0,
                    now,
                    goal_id,
                ),
            )

    def find_active_task_by_dedupe(self, job_id: str, dedupe_key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE job_id = ?
                  AND dedupe_key = ?
                  AND status NOT IN ('completed', 'failed', 'verification_failed', 'skipped')
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_id, dedupe_key),
            ).fetchone()
        return row

    def next_ready_task(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tasks
                WHERE job_id = ?
                  AND status IN ('ready', 'queued')
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    def get_latest_run_for_job(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM runs
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row

    def create_memory(
        self,
        *,
        job_id: str,
        kind: str,
        summary: str,
        detail: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        cluster_key: str | None = None,
        source_task_id: int | None = None,
        source_run_id: int | None = None,
        source_session_id: str | None = None,
    ) -> int:
        now = utc_now().isoformat()
        fingerprint = self._memory_fingerprint(kind, summary)
        normalized_cluster_key = cluster_key.strip().lower() if cluster_key else None
        merged_tags = sorted(set(tags or []))
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE job_id = ?
                  AND kind = ?
                  AND COALESCE(cluster_key, '') = COALESCE(?, '')
                  AND COALESCE(fingerprint, '') = COALESCE(?, '')
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    job_id,
                    kind,
                    normalized_cluster_key,
                    fingerprint,
                ),
            ).fetchone()
            if existing is not None:
                existing_tags = self._decode_tags(existing["tags_json"])
                next_tags = sorted(set(existing_tags).union(merged_tags))
                next_detail = self._merge_memory_detail(str(existing["detail"] or ""), detail)
                next_summary = self._merge_memory_summary(str(existing["summary"]), summary)
                conn.execute(
                    """
                    UPDATE memories
                    SET summary = ?,
                        detail = ?,
                        tags_json = ?,
                        occurrence_count = ?,
                        source_task_id = COALESCE(?, source_task_id),
                        source_run_id = COALESCE(?, source_run_id),
                        source_session_id = COALESCE(?, source_session_id),
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_summary,
                        next_detail,
                        json.dumps(next_tags),
                        int(existing["occurrence_count"] or 1) + 1,
                        source_task_id,
                        source_run_id,
                        source_session_id,
                        now,
                        now,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO memories (
                    job_id,
                    kind,
                    summary,
                    detail,
                    fingerprint,
                    cluster_key,
                    occurrence_count,
                    tags_json,
                    source_task_id,
                    source_run_id,
                    source_session_id,
                    created_at,
                    last_seen_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    summary,
                    detail,
                    fingerprint,
                    normalized_cluster_key,
                    1,
                    json.dumps(merged_tags),
                    source_task_id,
                    source_run_id,
                    source_session_id,
                    now,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_memory(self, memory_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"memory {memory_id} does not exist")
        return row

    def list_memories(self, limit: int = 20, *, job_id: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if job_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM memories
                    WHERE job_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM memories
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return rows

    def search_memories(
        self,
        *,
        job_id: str,
        query: str,
        limit: int = 5,
        candidate_limit: int = 100,
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, candidate_limit),
            ).fetchall()
        tokens = self._tokenize(query)
        if not rows:
            return []
        if not tokens:
            return rows[:limit]
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            score = self._memory_score(row, tokens)
            if score > 0:
                scored.append((score, row))
        if not scored:
            return rows[:limit]
        scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        return [row for _, row in scored[:limit]]

    def ensure_job_state(self, job_id: str) -> None:
        now = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO job_state (job_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, now),
            )

    def get_job_state(self, job_id: str) -> sqlite3.Row:
        self.ensure_job_state(job_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_state WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"job state for {job_id} does not exist")
        return row

    def set_job_pause(self, job_id: str, *, paused: bool, reason: str | None) -> None:
        self.ensure_job_state(job_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_state
                SET paused = ?, pause_reason = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (1 if paused else 0, reason, utc_now().isoformat(), job_id),
            )

    def write_job_state(self, job_id: str, state: dict[str, object]) -> None:
        self.ensure_job_state(job_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE job_state
                SET paused = ?,
                    pause_reason = ?,
                    consecutive_failures = ?,
                    daily_wake_date = ?,
                    daily_wake_count = ?,
                    last_run_id = ?,
                    last_status = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    1 if bool(state.get("paused")) else 0,
                    state.get("pause_reason"),
                    int(state.get("consecutive_failures", 0) or 0),
                    state.get("daily_wake_date"),
                    int(state.get("daily_wake_count", 0) or 0),
                    state.get("last_run_id"),
                    state.get("last_status"),
                    utc_now().isoformat(),
                    job_id,
                ),
            )

    def record_job_run(
        self,
        job_id: str,
        *,
        run_id: int,
        status: str,
        local_date: str,
    ) -> None:
        self.ensure_job_state(job_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_state WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"job state for {job_id} does not exist")
            wake_count = int(row["daily_wake_count"])
            wake_date = row["daily_wake_date"]
            if wake_date != local_date:
                wake_count = 0
            wake_count += 1
            consecutive_failures = int(row["consecutive_failures"])
            if status in {"failed", "verification_failed"}:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            conn.execute(
                """
                UPDATE job_state
                SET daily_wake_date = ?,
                    daily_wake_count = ?,
                    consecutive_failures = ?,
                    last_run_id = ?,
                    last_status = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    local_date,
                    wake_count,
                    consecutive_failures,
                    run_id,
                    status,
                    utc_now().isoformat(),
                    job_id,
                ),
            )

    def get_latest_resumable_run(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM runs
                WHERE job_id = ?
                  AND codex_thread_id IS NOT NULL
                  AND workspace_path IS NOT NULL
                  AND status IN ('completed', 'verification_failed', 'failed')
                ORDER BY id DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row

    def upsert_session(
        self,
        job_id: str,
        *,
        repo_path: Path,
        workspace_path: Path,
        branch_name: str | None,
        base_head: str | None,
        codex_thread_id: str,
        last_run_id: int,
        last_status: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    job_id,
                    repo_path,
                    workspace_path,
                    branch_name,
                    base_head,
                    codex_thread_id,
                    last_run_id,
                    last_status,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    repo_path = excluded.repo_path,
                    workspace_path = excluded.workspace_path,
                    branch_name = excluded.branch_name,
                    base_head = excluded.base_head,
                    codex_thread_id = excluded.codex_thread_id,
                    last_run_id = excluded.last_run_id,
                    last_status = excluded.last_status,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    str(repo_path),
                    str(workspace_path),
                    branch_name,
                    base_head,
                    codex_thread_id,
                    last_run_id,
                    last_status,
                    utc_now().isoformat(),
                ),
            )

    def get_session(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return row

    def list_sessions(self, limit: int = 20, *, job_id: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if job_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM sessions
                    WHERE job_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM sessions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return rows

    def acquire_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> bool:
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id, expires_at FROM leases WHERE lease_key = ?",
                (lease_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO leases (lease_key, owner_id, expires_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lease_key, owner_id, expires_at.isoformat(), now.isoformat()),
                )
                return True
            lease_expiry = datetime.fromisoformat(row["expires_at"])
            if lease_expiry <= now:
                conn.execute(
                    """
                    UPDATE leases
                    SET owner_id = ?, expires_at = ?, updated_at = ?
                    WHERE lease_key = ?
                    """,
                    (owner_id, expires_at.isoformat(), now.isoformat(), lease_key),
                )
                return True
            return False

    def release_lease(self, lease_key: str, owner_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM leases WHERE lease_key = ? AND owner_id = ?",
                (lease_key, owner_id),
            )

    def renew_lease(self, lease_key: str, owner_id: str, ttl_seconds: int) -> bool:
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE leases
                SET expires_at = ?, updated_at = ?
                WHERE lease_key = ? AND owner_id = ?
                """,
                (expires_at.isoformat(), now.isoformat(), lease_key, owner_id),
            )
            return cursor.rowcount > 0

    def _memory_score(self, row: sqlite3.Row, tokens: list[str]) -> float:
        summary = str(row["summary"] or "").lower()
        detail = str(row["detail"] or "").lower()
        kind = str(row["kind"] or "").lower()
        cluster_key = str(row["cluster_key"] or "").lower()
        tags = [tag.lower() for tag in self._decode_tags(row["tags_json"])]
        occurrence_count = max(1, int(row["occurrence_count"] or 1))
        score = 0.0
        for token in tokens:
            if token in summary:
                score += 4.0
            if token in detail:
                score += 2.0
            if token in kind:
                score += 1.0
            if token in cluster_key:
                score += 2.5
            if token in tags:
                score += 3.0
        score += min(3.0, 0.75 * (occurrence_count - 1))
        score += self._memory_recency_boost(row)
        return score

    def _tokenize(self, query: str) -> list[str]:
        return [token for token in re.split(r"[^a-zA-Z0-9_]+", query.lower()) if len(token) >= 3]

    def _memory_fingerprint(self, kind: str, summary: str) -> str:
        normalized = " ".join(summary.lower().split())
        return f"{kind.lower()}::{normalized}"

    def _decode_tags(self, raw_tags: object) -> list[str]:
        try:
            tags = json.loads(str(raw_tags or "[]"))
        except json.JSONDecodeError:
            return []
        return [str(tag) for tag in tags if str(tag).strip()]

    def _merge_memory_summary(self, existing: str, incoming: str) -> str:
        return incoming if len(incoming.strip()) > len(existing.strip()) else existing

    def _merge_memory_detail(self, existing: str, incoming: str | None) -> str | None:
        incoming_text = (incoming or "").strip()
        existing_text = existing.strip()
        if not existing_text:
            return incoming_text or None
        if not incoming_text:
            return existing_text
        if incoming_text == existing_text:
            return existing_text
        if len(incoming_text) > len(existing_text):
            return incoming_text
        return existing_text

    def _memory_recency_boost(self, row: sqlite3.Row) -> float:
        seen_at = str(row["last_seen_at"] or row["updated_at"] or "")
        if not seen_at:
            return 0.0
        try:
            seen = datetime.fromisoformat(seen_at)
        except ValueError:
            return 0.0
        age_seconds = max(0.0, (utc_now() - seen).total_seconds())
        if age_seconds <= 3600:
            return 1.5
        if age_seconds <= 86400:
            return 0.75
        return 0.1
