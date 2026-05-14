from __future__ import annotations

import calendar
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_DB_PATH = "data/incidents.sqlite"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json_dumps(value: Optional[Dict[str, Any]]) -> str:
    if value is None:
        return "{}"

    try:
        return json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        return json.dumps({"repr": repr(value)}, sort_keys=True)


def _json_loads(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}

    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    except json.JSONDecodeError:
        return {"raw": value}


class IncidentStore:
    """
    SQLite-backed reliability store.

    Stores:
    - runs
    - incidents
    - repair attempts
    - known failures
    - MTTR metrics
    - deployment events
    - rollback events

    This class intentionally uses only Python standard-library modules so it can
    run locally, in CI, and in early production deployments without extra infra.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    flow TEXT NOT NULL,
                    prompt TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    stage TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    safe_to_retry INTEGER NOT NULL DEFAULT 0,
                    traffic_switched INTEGER NOT NULL DEFAULT 0,
                    rollback_required INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    mttr_seconds REAL,
                    resolution TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS repair_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS known_failures (
                    root_cause TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    last_stage TEXT,
                    last_error_type TEXT,
                    last_message TEXT
                );

                CREATE TABLE IF NOT EXISTS mttr_metrics (
                    metric_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    run_id TEXT,
                    root_cause TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    mttr_seconds REAL NOT NULL,
                    measured_at TEXT NOT NULL,
                    FOREIGN KEY(incident_id) REFERENCES incidents(incident_id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS deployment_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    color TEXT,
                    version TEXT,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS rollback_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    from_color TEXT,
                    to_color TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_incidents_run_id
                    ON incidents(run_id);

                CREATE INDEX IF NOT EXISTS idx_incidents_root_cause
                    ON incidents(root_cause);

                CREATE INDEX IF NOT EXISTS idx_incidents_created_at
                    ON incidents(created_at);

                CREATE INDEX IF NOT EXISTS idx_repair_attempts_incident_id
                    ON repair_attempts(incident_id);

                CREATE INDEX IF NOT EXISTS idx_mttr_root_cause
                    ON mttr_metrics(root_cause);
                """
            )

    def create_run(
        self,
        prompt: str,
        flow: str = "create",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        run_id = _new_id("run")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, flow, prompt, status, started_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    flow,
                    prompt,
                    "running",
                    _utc_now(),
                    _json_dumps(metadata),
                ),
            )

        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str = "success",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        finished_at = _utc_now()

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT started_at, metadata_json
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

            if row is None:
                return

            started_ts = self._parse_utc(row["started_at"])
            duration_ms = int(max(0.0, time.time() - started_ts) * 1000)

            existing_metadata = _json_loads(row["metadata_json"])
            if metadata:
                existing_metadata.update(metadata)

            conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    finished_at = ?,
                    duration_ms = ?,
                    metadata_json = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    finished_at,
                    duration_ms,
                    _json_dumps(existing_metadata),
                    run_id,
                ),
            )

    def create_incident(
        self,
        run_id: Optional[str],
        stage: str,
        root_cause: str,
        error_type: str,
        severity: str,
        confidence: float,
        message: str,
        evidence: Optional[Dict[str, Any]] = None,
        status: str = "open",
        safe_to_retry: bool = False,
        traffic_switched: bool = False,
        rollback_required: bool = False,
    ) -> str:
        incident_id = _new_id("inc")
        created_at = _utc_now()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO incidents (
                    incident_id,
                    run_id,
                    stage,
                    root_cause,
                    error_type,
                    severity,
                    confidence,
                    status,
                    message,
                    evidence_json,
                    safe_to_retry,
                    traffic_switched,
                    rollback_required,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    run_id,
                    stage,
                    root_cause,
                    error_type,
                    severity,
                    float(confidence),
                    status,
                    message,
                    _json_dumps(evidence),
                    int(safe_to_retry),
                    int(traffic_switched),
                    int(rollback_required),
                    created_at,
                ),
            )

            self._upsert_known_failure(
                conn=conn,
                root_cause=root_cause,
                stage=stage,
                error_type=error_type,
                message=message,
                now=created_at,
            )

        return incident_id

    def resolve_incident(
        self,
        incident_id: str,
        resolution: str = "resolved",
    ) -> Optional[float]:
        resolved_at = _utc_now()

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, created_at, root_cause, stage, severity
                FROM incidents
                WHERE incident_id = ?
                """,
                (incident_id,),
            ).fetchone()

            if row is None:
                return None

            mttr_seconds = max(0.0, time.time() - self._parse_utc(row["created_at"]))

            conn.execute(
                """
                UPDATE incidents
                SET status = 'resolved',
                    resolved_at = ?,
                    mttr_seconds = ?,
                    resolution = ?
                WHERE incident_id = ?
                """,
                (
                    resolved_at,
                    mttr_seconds,
                    resolution,
                    incident_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO mttr_metrics (
                    metric_id,
                    incident_id,
                    run_id,
                    root_cause,
                    stage,
                    severity,
                    mttr_seconds,
                    measured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("mttr"),
                    incident_id,
                    row["run_id"],
                    row["root_cause"],
                    row["stage"],
                    row["severity"],
                    mttr_seconds,
                    resolved_at,
                ),
            )

        return mttr_seconds

    def record_repair_attempt(
        self,
        incident_id: str,
        action: str,
        status: str,
        details: Optional[Dict[str, Any]] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> str:
        attempt_id = _new_id("repair")
        started = started_at or _utc_now()
        finished = finished_at

        duration_ms = None
        if finished:
            duration_ms = int(
                max(0.0, self._parse_utc(finished) - self._parse_utc(started))
                * 1000
            )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repair_attempts (
                    attempt_id,
                    incident_id,
                    action,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    incident_id,
                    action,
                    status,
                    started,
                    finished,
                    duration_ms,
                    _json_dumps(details),
                ),
            )

        return attempt_id

    def record_deployment_event(
        self,
        event_type: str,
        status: str,
        run_id: Optional[str] = None,
        color: Optional[str] = None,
        version: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        event_id = _new_id("dep")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deployment_events (
                    event_id,
                    run_id,
                    event_type,
                    color,
                    version,
                    status,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    event_type,
                    color,
                    version,
                    status,
                    _json_dumps(details),
                    _utc_now(),
                ),
            )

        return event_id

    def record_rollback_event(
        self,
        status: str,
        run_id: Optional[str] = None,
        from_color: Optional[str] = None,
        to_color: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        event_id = _new_id("rollback")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rollback_events (
                    event_id,
                    run_id,
                    from_color,
                    to_color,
                    status,
                    reason,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    from_color,
                    to_color,
                    status,
                    reason,
                    _json_dumps(details),
                    _utc_now(),
                ),
            )

        return event_id

    def get_recent_incidents(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM incidents
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_open_incidents(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM incidents
                WHERE status != 'resolved'
                ORDER BY created_at DESC
                """
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def summarize_mttr(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.root_cause,
                    COUNT(*) AS incidents,
                    SUM(CASE WHEN i.status = 'resolved' THEN 1 ELSE 0 END)
                        AS resolved,
                    AVG(i.mttr_seconds) AS avg_mttr_seconds,
                    MAX(i.mttr_seconds) AS max_mttr_seconds,
                    k.occurrence_count AS known_occurrences,
                    k.last_seen_at AS last_seen_at
                FROM incidents i
                LEFT JOIN known_failures k
                    ON k.root_cause = i.root_cause
                GROUP BY i.root_cause
                ORDER BY incidents DESC, i.root_cause ASC
                """
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

        return self._row_to_dict(row) if row else None

    def _upsert_known_failure(
        self,
        conn: sqlite3.Connection,
        root_cause: str,
        stage: str,
        error_type: str,
        message: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO known_failures (
                root_cause,
                first_seen_at,
                last_seen_at,
                occurrence_count,
                last_stage,
                last_error_type,
                last_message
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(root_cause) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                occurrence_count = occurrence_count + 1,
                last_stage = excluded.last_stage,
                last_error_type = excluded.last_error_type,
                last_message = excluded.last_message
            """,
            (
                root_cause,
                now,
                now,
                stage,
                error_type,
                message[:2000],
            ),
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)

        for key in ("metadata_json", "evidence_json", "details_json"):
            if key in data:
                data[key.replace("_json", "")] = _json_loads(data.pop(key))

        return data

    @staticmethod
    def _parse_utc(value: str) -> float:
        try:
            return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
        except Exception:
            return time.time()