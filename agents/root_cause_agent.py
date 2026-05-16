from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn

        return decorator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELIABILITY_DB = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


@dataclass
class RootCauseReport:
    root_cause: str
    confidence: float
    severity: str
    safe_to_retry: bool
    traffic_switched: bool
    rollback_required: bool
    summary: str
    repair_hint: str
    evidence: list[str]
    stage: str = "unknown"
    incident_id: str | None = None
    created_at_utc: str | None = None
    stored: bool = False
    db_path: str | None = None


# ---------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------


def _text(*values: Any) -> str:
    parts: list[str] = []

    for value in values:
        if value is None:
            continue

        if isinstance(value, dict):
            parts.append(json.dumps(value, indent=2, sort_keys=True, default=str))
            continue

        if isinstance(value, list):
            parts.append("\n".join(str(item) for item in value))
            continue

        parts.append(str(value))

    return "\n".join(parts)


def _lower(*values: Any) -> str:
    return _text(*values).lower()


def _contains_any(text: str, markers: list[str]) -> bool:
    lower = text.lower()
    return any(marker.lower() in lower for marker in markers)


def _extract_evidence(text: str, markers: list[str], max_items: int = 8) -> list[str]:
    evidence: list[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines:
        lower = line.lower()

        if any(marker.lower() in lower for marker in markers):
            cleaned = line[-500:]

            if cleaned not in evidence:
                evidence.append(cleaned)

        if len(evidence) >= max_items:
            break

    return evidence


def _raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _incident_id(report: dict[str, Any], raw_text: str) -> str:
    seed = "|".join(
        [
            str(report.get("stage", "unknown")),
            str(report.get("root_cause", "unknown_failure")),
            _raw_hash(raw_text)[:24],
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------


def _db_path(value: str | Path | None = None) -> Path:
    if value is None:
        return DEFAULT_RELIABILITY_DB

    return Path(value).expanduser().resolve()


def init_reliability_db(db_path: str | Path | None = None) -> Path:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reliability_reports (
                incident_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                stage TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                confidence REAL NOT NULL,
                severity TEXT NOT NULL,
                safe_to_retry INTEGER NOT NULL,
                traffic_switched INTEGER NOT NULL,
                rollback_required INTEGER NOT NULL,
                summary TEXT NOT NULL,
                repair_hint TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                raw_text_hash TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                source TEXT,
                metadata_json TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reliability_reports_created_at
            ON reliability_reports(created_at_utc)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reliability_reports_root_cause
            ON reliability_reports(root_cause)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reliability_reports_stage
            ON reliability_reports(stage)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reliability_knowledge (
                pattern_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                category TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                summary TEXT NOT NULL,
                repair_hint TEXT NOT NULL,
                markers_json TEXT NOT NULL
            )
            """
        )

        conn.commit()

    seed_reliability_knowledge_base(path)
    return path


def seed_reliability_knowledge_base(db_path: str | Path | None = None) -> None:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    patterns = [
        {
            "pattern_id": "local_static_webserver_verified",
            "category": "milestone",
            "root_cause": "stable_local_runtime_verified",
            "summary": "Local runtime supports built-in Pingora static webserver plus dynamic proxy/load-balancer routes.",
            "repair_hint": "Use this as the baseline expected-good state before Docker/blue-green testing.",
            "markers": [
                "AI Pingora Webserver is running",
                "/ static",
                "/api random",
                "/orders weighted_round_robin",
                "/payments ip_hash",
            ],
        },
        {
            "pattern_id": "nlp_algorithm_leak",
            "category": "config_repair",
            "root_cause": "nlp_route_algorithm_leak",
            "summary": "One route algorithm, often weighted_round_robin from /orders, leaked into other routes.",
            "repair_hint": "Parse algorithms per route segment and prevent full-prompt algorithm fallback from overwriting route-specific intent.",
            "markers": [
                "/api weighted_round_robin",
                "/users weighted_round_robin",
                "/payments weighted_round_robin",
                "load_balance",
                "balancing",
            ],
        },
        {
            "pattern_id": "load_balance_alias_not_canonicalized",
            "category": "config_repair",
            "root_cause": "load_balance_alias_not_canonicalized",
            "summary": "LLM emitted route-specific load_balance but canonical balancing kept an incorrect value.",
            "repair_hint": "Treat load_balance as a high-priority alias, copy it into balancing, then remove load_balance from final config.",
            "markers": [
                '"load_balance": "random"',
                '"balancing": "weighted_round_robin"',
                '"load_balance": "ip_hash"',
            ],
        },
        {
            "pattern_id": "old_simplehttp_backend_404",
            "category": "local_demo_backend",
            "root_cause": "old_simplehttp_demo_backend_404",
            "summary": "Backend returned Python SimpleHTTP 404 because old demo servers served only files and did not respond to arbitrary paths.",
            "repair_hint": "Kill old backend ports and restart wildcard scripts/demo_backends.py that responds with DemoBackend/1.0 on any path.",
            "markers": [
                "SimpleHTTP/0.6 Python",
                "404 File not found",
                "Message: File not found",
            ],
        },
        {
            "pattern_id": "static_route_needs_no_backend",
            "category": "static_webserver",
            "root_cause": "static_route_requires_backend_regression",
            "summary": "A static route was incorrectly treated like a proxy route requiring upstreams.",
            "repair_hint": "Keep type=static routes separate in validator/project_writer; static routes need root/index, not upstreams.",
            "markers": [
                '"type": "static"',
                "upstream",
                "backend",
                "static route",
            ],
        },
        {
            "pattern_id": "weighted_weights_stripped",
            "category": "load_balancing",
            "root_cause": "weighted_round_robin_weights_stripped",
            "summary": "Weighted route lost weight objects and behaved like even round robin.",
            "repair_hint": "Preserve upstreams as objects with address and weight when balancing=weighted_round_robin.",
            "markers": [
                "26 Demo Backend 9201",
                "27 Demo Backend 9202",
                "27 Demo Backend 9203",
                "weighted_round_robin",
            ],
        },
        {
            "pattern_id": "ip_hash_not_sticky",
            "category": "load_balancing",
            "root_cause": "ip_hash_not_sticky",
            "summary": "IP hash route distributed a stable local client across multiple backends.",
            "repair_hint": "Hash stable client IP only, not per-request client source port.",
            "markers": [
                "ip_hash",
                "Demo Backend 9301",
                "Demo Backend 9302",
            ],
        },
    ]

    with sqlite3.connect(path) as conn:
        for pattern in patterns:
            conn.execute(
                """
                INSERT OR REPLACE INTO reliability_knowledge (
                    pattern_id,
                    created_at_utc,
                    category,
                    root_cause,
                    summary,
                    repair_hint,
                    markers_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pattern["pattern_id"],
                    _utc_now(),
                    pattern["category"],
                    pattern["root_cause"],
                    pattern["summary"],
                    pattern["repair_hint"],
                    json.dumps(pattern["markers"], indent=2),
                ),
            )

        conn.commit()


def save_reliability_report(
    report: dict[str, Any],
    *,
    raw_text: str = "",
    db_path: str | Path | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = init_reliability_db(db_path)

    stored_report = dict(report)
    stored_report["created_at_utc"] = stored_report.get("created_at_utc") or _utc_now()
    stored_report["incident_id"] = stored_report.get("incident_id") or _incident_id(
        stored_report,
        raw_text,
    )

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reliability_reports (
                incident_id,
                created_at_utc,
                stage,
                root_cause,
                confidence,
                severity,
                safe_to_retry,
                traffic_switched,
                rollback_required,
                summary,
                repair_hint,
                evidence_json,
                raw_text_hash,
                raw_text,
                source,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_report["incident_id"],
                stored_report["created_at_utc"],
                str(stored_report.get("stage", "unknown")),
                str(stored_report.get("root_cause", "unknown_failure")),
                float(stored_report.get("confidence", 0.0)),
                str(stored_report.get("severity", "medium")),
                int(bool(stored_report.get("safe_to_retry", False))),
                int(bool(stored_report.get("traffic_switched", False))),
                int(bool(stored_report.get("rollback_required", False))),
                str(stored_report.get("summary", "")),
                str(stored_report.get("repair_hint", "")),
                json.dumps(stored_report.get("evidence", []), indent=2, default=str),
                _raw_hash(raw_text),
                raw_text,
                source,
                json.dumps(metadata or {}, indent=2, sort_keys=True, default=str),
            ),
        )

        conn.commit()

    stored_report["stored"] = True
    stored_report["db_path"] = str(path)
    return stored_report


def list_reliability_reports(
    *,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    path = init_reliability_db(db_path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                incident_id,
                created_at_utc,
                stage,
                root_cause,
                confidence,
                severity,
                safe_to_retry,
                traffic_switched,
                rollback_required,
                summary,
                repair_hint,
                evidence_json,
                source,
                metadata_json
            FROM reliability_reports
            ORDER BY created_at_utc DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    reports: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        item["safe_to_retry"] = bool(item["safe_to_retry"])
        item["traffic_switched"] = bool(item["traffic_switched"])
        item["rollback_required"] = bool(item["rollback_required"])

        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        except Exception:
            item["evidence"] = []

        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}

        reports.append(item)

    return reports


def get_reliability_report(
    incident_id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    path = init_reliability_db(db_path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT *
            FROM reliability_reports
            WHERE incident_id = ?
            """,
            (incident_id,),
        ).fetchone()

    if row is None:
        return None

    item = dict(row)
    item["safe_to_retry"] = bool(item["safe_to_retry"])
    item["traffic_switched"] = bool(item["traffic_switched"])
    item["rollback_required"] = bool(item["rollback_required"])

    try:
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    except Exception:
        item["evidence"] = []

    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except Exception:
        item["metadata"] = {}

    return item


# ---------------------------------------------------------------------
# Report creation helpers
# ---------------------------------------------------------------------


def _report(
    *,
    root_cause: str,
    confidence: float,
    severity: str,
    safe_to_retry: bool,
    traffic_switched: bool,
    rollback_required: bool,
    summary: str,
    repair_hint: str,
    evidence: list[str],
    stage: str,
) -> dict[str, Any]:
    return asdict(
        RootCauseReport(
            root_cause=root_cause,
            confidence=confidence,
            severity=severity,
            safe_to_retry=safe_to_retry,
            traffic_switched=traffic_switched,
            rollback_required=rollback_required,
            summary=summary,
            repair_hint=repair_hint,
            evidence=evidence,
            stage=stage,
        )
    )


def _finalize_report(
    report: dict[str, Any],
    *,
    raw_text: str,
    persist: bool,
    db_path: str | Path | None,
    source: str | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    report = dict(report)
    report["created_at_utc"] = report.get("created_at_utc") or _utc_now()
    report["incident_id"] = report.get("incident_id") or _incident_id(report, raw_text)

    if persist:
        try:
            return save_reliability_report(
                report,
                raw_text=raw_text,
                db_path=db_path,
                source=source,
                metadata=metadata,
            )
        except Exception as exc:
            report["stored"] = False
            report["db_error"] = str(exc)
            return report

    report["stored"] = False
    report["db_path"] = str(_db_path(db_path))
    return report


def _traffic_switched_from_text(text: str) -> bool:
    lower = text.lower()

    if "traffic switched: yes" in lower:
        return True

    if "traffic switched: true" in lower:
        return True

    if "update deployed with blue/green switching" in lower:
        return True

    return False


def _stage_from_text(stage: str | None, text: str) -> str:
    if stage:
        return str(stage)

    lower = text.lower()

    if "environment_preflight" in lower or "preflight failed" in lower:
        return "environment_preflight"

    if "cargo check" in lower:
        return "cargo_check"

    if "project_generation" in lower or "generated rust" in lower:
        return "project_generation"

    if "config_repair" in lower or "repairing config" in lower:
        return "config_repair"

    if "bluegreen" in lower or "blue/green" in lower:
        return "bluegreen_deploy"

    if "local_runtime" in lower or "local runtime" in lower:
        return "local_runtime"

    if "docker compose" in lower or "compose" in lower:
        return "compose"

    if "health" in lower or "readiness" in lower:
        return "readiness"

    if "demo backend" in lower or "simplehttp" in lower:
        return "local_demo_backend"

    return "unknown"


def _cargo_check_passed(text: str) -> bool:
    lower = text.lower()
    return "cargo check passed" in lower or "✅ cargo check passed" in lower


def _cargo_check_failed(text: str) -> bool:
    lower = text.lower()

    if _cargo_check_passed(text):
        return False

    return (
        "cargo check failed" in lower
        or "failed to compile" in lower
        or "could not compile" in lower
        or ("error:" in lower and "rustc" in lower)
    )


def _has_port_conflict(text: str) -> bool:
    lower = text.lower()

    return (
        "address already in use" in lower
        or "addrinuse" in lower
        or "bind() failed" in lower
        or "port is already in use" in lower
        or ("0.0.0.0:" in lower and "still in use" in lower)
    )


def _extract_port_conflict_evidence(text: str) -> list[str]:
    evidence = _extract_evidence(
        text,
        [
            "address already in use",
            "AddrInUse",
            "bind() failed",
            "still in use",
            "port is already in use",
        ],
    )

    port_matches = re.findall(r"0\.0\.0\.0:(\d+)|127\.0\.0\.1:(\d+)|port\s+(\d+)", text)
    ports: list[str] = []

    for match in port_matches:
        for item in match:
            if item and item not in ports:
                ports.append(item)

    if ports:
        evidence.append(f"Ports mentioned: {', '.join(ports)}")

    return evidence


def _is_proxy_backend_same_port_conflict(text: str) -> bool:
    lower = text.lower()

    backend_match = re.search(r"starting backend on port (\d+)", lower)
    proxy_match = re.search(r"starting pingora proxy on port (\d+)", lower)

    if backend_match and proxy_match and backend_match.group(1) == proxy_match.group(1):
        return True

    return (
        "starting backend on port 3000" in lower
        and "starting pingora proxy on port 3000" in lower
        and "address already in use" in lower
    )


def _looks_like_stable_local_runtime(text: str) -> bool:
    lower = text.lower()

    return (
        "ai pingora webserver is running" in lower
        and "demo backend 9101" in lower
        and "demo backend 9102" in lower
        and "demo backend 9103" in lower
        and "demo backend 9201" in lower
        and "demo backend 9202" in lower
        and "demo backend 9203" in lower
        and "demo backend 9301" in lower
        and "demo backend 9401" in lower
    )


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


@traceable(name="root_cause_agent_classify", run_type="chain")
def classify_root_cause(
    stage: str | None = None,
    error: Any = None,
    output: Any = None,
    stderr: Any = None,
    stdout: Any = None,
    logs: Any = None,
    context: Any = None,
    persist: bool = True,
    db_path: str | Path | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Deterministic root-cause classifier with SQLite reliability persistence.

    It stores each report in:
      data/reliability_reports.sqlite3

    Important local milestone patterns included:
    - static webserver served directly by Pingora
    - random, round_robin, weighted_round_robin, ip_hash, and single backend routes
    - algorithm leak across routes
    - load_balance alias conflict
    - old SimpleHTTP demo backend 404
    - static route incorrectly requiring upstreams
    """

    raw_text = _text(stage, error, output, stderr, stdout, logs, context, kwargs)
    lower = raw_text.lower()
    detected_stage = _stage_from_text(stage, raw_text)
    traffic_switched = _traffic_switched_from_text(raw_text)

    def done(report: dict[str, Any]) -> dict[str, Any]:
        return _finalize_report(
            report,
            raw_text=raw_text,
            persist=persist,
            db_path=db_path,
            source=source,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Verified-good milestone
    # ------------------------------------------------------------------

    if _looks_like_stable_local_runtime(raw_text):
        return done(
            _report(
                root_cause="stable_local_runtime_verified",
                confidence=0.99,
                severity="info",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary=(
                    "Local direct runtime is verified: built-in static webserver plus "
                    "random, round_robin, weighted_round_robin, ip_hash, and single-backend routes work."
                ),
                repair_hint=(
                    "Use this as the known-good baseline before Docker/blue-green testing. "
                    "If future behavior regresses, compare generated config and src/main.rs against this state."
                ),
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "AI Pingora Webserver is running",
                        "Demo Backend 9101",
                        "Demo Backend 9102",
                        "Demo Backend 9103",
                        "Demo Backend 9201",
                        "Demo Backend 9202",
                        "Demo Backend 9203",
                        "Demo Backend 9301",
                        "Demo Backend 9401",
                    ],
                ),
                stage="local_runtime",
            )
        )

    # ------------------------------------------------------------------
    # New local webserver/LB milestone errors
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            '"load_balance": "random"',
            '"load_balance": "round_robin"',
            '"load_balance": "ip_hash"',
        ],
    ) and '"balancing": "weighted_round_robin"' in lower:
        return done(
            _report(
                root_cause="load_balance_alias_not_canonicalized",
                confidence=0.98,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="The LLM produced route-specific load_balance, but canonical balancing was overwritten or left conflicting.",
                repair_hint=(
                    "In config_repair_agent.py, validator.py, graph.py, and project_writer.py, "
                    "treat load_balance as a high-priority alias, copy it into balancing, then remove load_balance from final config."
                ),
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "load_balance",
                        "balancing",
                        "weighted_round_robin",
                        "random",
                        "round_robin",
                        "ip_hash",
                    ],
                ),
                stage="config_repair",
            )
        )

    if (
        "/api" in lower
        and "/users" in lower
        and "/payments" in lower
        and "weighted_round_robin" in lower
        and ("random" in lower or "round_robin" in lower or "ip_hash" in lower)
    ):
        if _contains_any(
            lower,
            [
                "/api none weighted_round_robin",
                "/users none weighted_round_robin",
                "/payments none weighted_round_robin",
            ],
        ):
            return done(
                _report(
                    root_cause="nlp_route_algorithm_leak",
                    confidence=0.97,
                    severity="high",
                    safe_to_retry=False,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="A route-specific algorithm leaked across other routes, commonly weighted_round_robin from /orders.",
                    repair_hint=(
                        "Parse prompt clauses per route; never infer algorithm from the full prompt when updating an individual route. "
                        "Use route-specific lock_prompt_route_intent() after repair, validation, security, and runtime addressing."
                    ),
                    evidence=_extract_evidence(
                        raw_text,
                        [
                            "/api",
                            "/users",
                            "/payments",
                            "weighted_round_robin",
                            "random",
                            "round_robin",
                            "ip_hash",
                        ],
                    ),
                    stage="config_repair",
                )
            )

    if _contains_any(
        lower,
        [
            "server: simplehttp/0.6 python",
            "simplehttp/0.6 python",
        ],
    ) and _contains_any(
        lower,
        [
            "404 file not found",
            "message: file not found",
            "error code explanation: 404",
        ],
    ):
        return done(
            _report(
                root_cause="old_simplehttp_demo_backend_404",
                confidence=0.99,
                severity="medium",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="The gateway reached an old Python SimpleHTTP backend that only serves files and returns 404 for dynamic paths.",
                repair_hint=(
                    "Kill backend ports and restart wildcard scripts/demo_backends.py. "
                    "Expected backend header is DemoBackend/1.0 and it should respond to any path such as /api/."
                ),
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "SimpleHTTP/0.6 Python",
                        "404 File not found",
                        "Message: File not found",
                        "DemoBackend/1.0",
                    ],
                ),
                stage="local_demo_backend",
            )
        )

    if _contains_any(
        lower,
        [
            "http/1.1 502 bad gateway",
            "502 bad gateway",
        ],
    ) and _contains_any(
        lower,
        [
            "/api",
            "/users",
            "/orders",
            "/payments",
            "/inventory",
        ],
    ):
        return done(
            _report(
                root_cause="demo_backend_not_running_or_unreachable",
                confidence=0.93,
                severity="high",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="Pingora route exists, but the backend application for that dynamic route is not reachable.",
                repair_hint="Run `python scripts/demo_backends.py start generated-pingora-proxy/config.json` and verify backend ports with status.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "502 Bad Gateway",
                        "Bad Gateway",
                        "Demo Backend",
                        "connection refused",
                    ],
                ),
                stage="local_runtime",
            )
        )

    if _contains_any(
        lower,
        [
            "route works",
            "ai pingora webserver is running",
            '"type": "static"',
        ],
    ) and _contains_any(
        lower,
        [
            "static route",
            "upstream required",
            "config must contain at least one valid route",
            "generated rust is missing upstream",
        ],
    ):
        return done(
            _report(
                root_cause="static_route_requires_backend_regression",
                confidence=0.94,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="A static webserver route was incorrectly treated as a proxy/load-balancer route.",
                repair_hint="Keep static routes separate: type=static, root, index. Static routes must not require upstream or upstreams.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "type",
                        "static",
                        "upstream",
                        "Generated Rust is missing upstream",
                        "valid route",
                    ],
                ),
                stage="project_generation",
            )
        )

    if _contains_any(
        lower,
        [
            "26 demo backend 9201",
            "27 demo backend 9202",
            "27 demo backend 9203",
            "26 demo backend",
            "27 demo backend",
        ],
    ) and "weighted_round_robin" in lower:
        return done(
            _report(
                root_cause="weighted_round_robin_weights_stripped",
                confidence=0.96,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Weighted round robin behaved like even distribution because backend weights were stripped.",
                repair_hint="Preserve upstreams as objects with address and weight for weighted_round_robin in validator.py and project_writer.py.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "weighted_round_robin",
                        "26 Demo Backend",
                        "27 Demo Backend",
                        "weight",
                    ],
                ),
                stage="load_balancing",
            )
        )

    if "ip_hash" in lower and _contains_any(
        lower,
        [
            "demo backend 9301",
            "demo backend 9302",
        ],
    ):
        if "demo backend 9301" in lower and "demo backend 9302" in lower:
            return done(
                _report(
                    root_cause="ip_hash_not_sticky",
                    confidence=0.90,
                    severity="medium",
                    safe_to_retry=False,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="IP hash distributed one stable local client across multiple backends.",
                    repair_hint="Hash only stable client IP, not source port. Strip changing port from session.client_addr().",
                    evidence=_extract_evidence(
                        raw_text,
                        [
                            "ip_hash",
                            "Demo Backend 9301",
                            "Demo Backend 9302",
                        ],
                    ),
                    stage="load_balancing",
                )
            )

    if _contains_any(
        lower,
        [
            "pingora is listening, but http probe did not return a clean application response",
            "this can happen when '/' is not routed or upstream backends are not running",
        ],
    ):
        return done(
            _report(
                root_cause="local_gateway_live_but_application_probe_unclean",
                confidence=0.88,
                severity="medium",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="Pingora is listening, but the HTTP probe did not receive a clean app response.",
                repair_hint=(
                    "Check whether `/` is a static route. If only dynamic routes exist, start backend servers. "
                    "For static webserver mode, verify public/index.html exists."
                ),
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Pingora is listening",
                        "HTTP probe",
                        "not routed",
                        "upstream backends are not running",
                    ],
                ),
                stage="local_runtime",
            )
        )

    # ------------------------------------------------------------------
    # Environment preflight
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "docker is not available",
            "docker could not be found",
            "cannot connect to the docker daemon",
            "is the docker daemon running",
        ],
    ):
        return done(
            _report(
                root_cause="docker_unavailable",
                confidence=0.97,
                severity="medium",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Docker is not available to the current Python/WSL environment.",
                repair_hint="Start Docker Desktop, enable WSL integration for this distro, then verify with `docker version`.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Docker is not available",
                        "docker could not be found",
                        "Cannot connect to the Docker daemon",
                        "Docker Desktop",
                        "WSL integration",
                    ],
                ),
                stage="environment_preflight",
            )
        )

    if _contains_any(
        lower,
        [
            "docker compose is not available",
            "docker: 'compose' is not a docker command",
            "compose is not available",
        ],
    ):
        return done(
            _report(
                root_cause="docker_compose_unavailable",
                confidence=0.96,
                severity="medium",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Docker Compose is not available.",
                repair_hint="Enable Docker Compose v2 in Docker Desktop and verify with `docker compose version`.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Docker Compose is not available",
                        "docker compose version",
                        "not a docker command",
                    ],
                ),
                stage="environment_preflight",
            )
        )

    if "preflight failed" in lower:
        return done(
            _report(
                root_cause="environment_preflight_failed",
                confidence=0.90,
                severity="medium",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Environment preflight failed before project generation or deployment.",
                repair_hint="Fix the missing local dependency or environment setting shown in the preflight output, then retry.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Preflight failed",
                        "not available",
                        "missing",
                        "Start Docker",
                        "docker compose",
                    ],
                ),
                stage="environment_preflight",
            )
        )

    # ------------------------------------------------------------------
    # Cargo check
    # ------------------------------------------------------------------

    if _cargo_check_failed(raw_text):
        return done(
            _report(
                root_cause="cargo_check_failure",
                confidence=0.94,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Generated Rust failed cargo check.",
                repair_hint="Inspect the cargo compiler error and patch project_writer.py or runtime_agent.py so valid Rust is generated.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "cargo check failed",
                        "could not compile",
                        "failed to compile",
                        "error:",
                        "rustc",
                    ],
                ),
                stage="cargo_check",
            )
        )

    # ------------------------------------------------------------------
    # Local runtime
    # ------------------------------------------------------------------

    if detected_stage == "local_runtime" or "local runtime" in lower:
        if _has_port_conflict(raw_text):
            return done(
                _report(
                    root_cause="local_gateway_port_conflict",
                    confidence=0.95,
                    severity="medium",
                    safe_to_retry=False,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="The local Pingora gateway could not start because its listen port is already in use.",
                    repair_hint="Stop the existing process using the gateway port or choose a different gateway port.",
                    evidence=_extract_port_conflict_evidence(raw_text),
                    stage="local_runtime",
                )
            )

        if _contains_any(
            lower,
            [
                "generated pingora process exited before becoming ready",
                "local_runtime_startup_failure",
                "process exited before becoming ready",
            ],
        ):
            return done(
                _report(
                    root_cause="local_runtime_startup_failure",
                    confidence=0.93,
                    severity="high",
                    safe_to_retry=False,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="Cargo check passed, but the generated Pingora process exited during local startup.",
                    repair_hint="Run `RUST_BACKTRACE=1 cargo run` inside generated-pingora-proxy and inspect tmp/logs/local-pingora-PORT.log.",
                    evidence=_extract_evidence(
                        raw_text,
                        [
                            "process exited",
                            "local_runtime_startup_failure",
                            "RUST_BACKTRACE",
                            "panic",
                        ],
                    ),
                    stage="local_runtime",
                )
            )

        if _contains_any(
            lower,
            [
                "local_readiness_timeout",
                "did not become ready",
                "readiness timeout",
            ],
        ):
            return done(
                _report(
                    root_cause="local_readiness_timeout",
                    confidence=0.92,
                    severity="high",
                    safe_to_retry=True,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="The local Pingora process started but did not become ready before timeout.",
                    repair_hint="Check whether the gateway port is open, whether `/` responds, and whether generated routes are correct.",
                    evidence=_extract_evidence(
                        raw_text,
                        [
                            "did not become ready",
                            "readiness",
                            "root_status",
                            "health_status",
                            "tcp_open",
                        ],
                    ),
                    stage="local_runtime",
                )
            )

        return done(
            _report(
                root_cause="local_runtime_failure",
                confidence=0.88,
                severity="high",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="Cargo check passed, but local runtime orchestration failed.",
                repair_hint="Use runtime_agent.run_local_gateway after cargo check and avoid routing `--runtime local` into blue/green deploy.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "local",
                        "runtime",
                        "cargo check passed",
                        "failed",
                    ],
                ),
                stage="local_runtime",
            )
        )

    # ------------------------------------------------------------------
    # Docker registry / image build
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "failed to resolve source metadata",
            "failed to do request",
            "registry-1.docker.io",
            "docker.io/library/rust",
            "pull access denied",
            "toomanyrequests",
            "connection reset by peer",
        ],
    ):
        return done(
            _report(
                root_cause="docker_registry_or_network_failure",
                confidence=0.94,
                severity="medium",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="Docker failed while pulling or resolving an image from the registry.",
                repair_hint="Run `docker pull rust:1-bookworm`, check internet/Docker Hub availability, then retry deployment.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "failed to resolve source metadata",
                        "registry-1.docker.io",
                        "docker.io/library/rust",
                        "failed to do request",
                        "pull access denied",
                        "TooManyRequests",
                    ],
                ),
                stage="docker_build",
            )
        )

    # ------------------------------------------------------------------
    # Compose artifact / compose writer
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "no such file or directory",
            "docker-compose.bluegreen.yml",
            "expected compose file",
            "compose file not found",
        ],
    ):
        if "docker-compose.bluegreen.yml" in lower:
            root = "missing_bluegreen_compose_artifact"
            summary = "Blue/green deployment expected a docker-compose.bluegreen.yml file that was not generated."
            hint = "Make project generation create blue/green compose artifacts, or make the deployer use the generated docker-compose.yml."
        else:
            root = "missing_compose_artifact"
            summary = "Deployment expected a Compose file that does not exist."
            hint = "Check project_writer.py, compose_writer.py, and bluegreen_deployer.py for path mismatch."

        return done(
            _report(
                root_cause=root,
                confidence=0.93,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary=summary,
                repair_hint=hint,
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "No such file or directory",
                        "docker-compose.bluegreen.yml",
                        "Expected compose file",
                        "compose file",
                    ],
                ),
                stage="bluegreen_deploy",
            )
        )

    if _contains_any(
        lower,
        [
            "generated rust dropped one or more load-balancer upstreams",
            "ignored route['upstreams']",
            "missing from src/main.rs",
            "docker/compose helper rewrote only route['upstream']",
        ],
    ):
        return done(
            _report(
                root_cause="load_balancer_upstreams_dropped_during_generation",
                confidence=0.98,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="A helper rewrote or generated only the primary route upstream and dropped one or more load-balancer upstreams.",
                repair_hint="Patch compose_writer.py/project_writer.py to read every route['upstreams'] item, not only route['upstream'].",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Generated Rust dropped",
                        "Missing from src/main.rs",
                        "ignored route['upstreams']",
                        "rewrote only route['upstream']",
                    ],
                ),
                stage="project_generation",
            )
        )

    if _contains_any(
        lower,
        [
            "services.proxy.depends_on contains an invalid type",
            "yaml",
            "mapping values are not allowed",
            "services",
            "docker compose config",
        ],
    ) and "compose" in lower:
        return done(
            _report(
                root_cause="invalid_docker_compose_yaml",
                confidence=0.90,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="Generated Docker Compose YAML is invalid.",
                repair_hint="Run `docker compose -f generated-pingora-proxy/docker-compose.yml config` and patch compose_writer.py.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "docker compose config",
                        "yaml",
                        "invalid",
                        "services",
                        "mapping values",
                    ],
                ),
                stage="compose_validation",
            )
        )

    # ------------------------------------------------------------------
    # Port conflicts
    # ------------------------------------------------------------------

    if _is_proxy_backend_same_port_conflict(raw_text):
        return done(
            _report(
                root_cause="proxy_backend_same_port_conflict",
                confidence=0.98,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="The generated container started a backend and the Pingora proxy on the same port.",
                repair_hint="Use separate ports: backend on its own port and Pingora on the gateway/proxy port. Do not derive proxy listen port from backend port.",
                evidence=_extract_port_conflict_evidence(raw_text),
                stage="runtime_startup",
            )
        )

    if _has_port_conflict(raw_text):
        return done(
            _report(
                root_cause="port_conflict",
                confidence=0.94,
                severity="medium",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="A required port is already in use.",
                repair_hint="Stop the process/container using the port or select a different port.",
                evidence=_extract_port_conflict_evidence(raw_text),
                stage=detected_stage,
            )
        )

    # ------------------------------------------------------------------
    # Docker-host reachability
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "host.docker.internal",
            "no route to host",
            "could not resolve host",
            "temporary failure in name resolution",
            "failed to lookup address information",
            "name or service not known",
        ],
    ):
        return done(
            _report(
                root_cause="host_docker_internal_unreachable",
                confidence=0.91,
                severity="high",
                safe_to_retry=False,
                traffic_switched=False,
                rollback_required=False,
                summary="The Docker container could not resolve or reach host.docker.internal.",
                repair_hint="Add `extra_hosts: ['host.docker.internal:host-gateway']` to the proxy service or use local/demo mode.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "host.docker.internal",
                        "could not resolve host",
                        "name or service not known",
                        "failed to lookup address information",
                        "no route to host",
                    ],
                ),
                stage="docker_host_runtime",
            )
        )

    # ------------------------------------------------------------------
    # Readiness/health failures
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "health url:",
            "fallback url:",
            "last status: 502",
            "__pingora_health",
            "__edge_probe",
            "did not become ready",
        ],
    ):
        if "tcp open" in lower and "true" in lower and "last status: 502" in lower:
            return done(
                _report(
                    root_cause="health_probe_proxied_to_unavailable_upstream",
                    confidence=0.94,
                    severity="high",
                    safe_to_retry=False,
                    traffic_switched=False,
                    rollback_required=False,
                    summary="The gateway process is running and the port is open, but readiness probes return 502.",
                    repair_hint="Handle `/__pingora_health` and `/__edge_probe` locally in generated Pingora before proxy routing. Also verify upstream backends are reachable.",
                    evidence=_extract_evidence(
                        raw_text,
                        [
                            "TCP open",
                            "Health URL",
                            "Fallback URL",
                            "last status: 502",
                            "__pingora_health",
                            "__edge_probe",
                        ],
                    ),
                    stage="readiness",
                )
            )

        return done(
            _report(
                root_cause="readiness_probe_failed",
                confidence=0.88,
                severity="high",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="The deployment started but did not pass readiness checks.",
                repair_hint="Inspect container logs, health URL, fallback URL, and upstream reachability.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "Health URL",
                        "Fallback URL",
                        "did not become ready",
                        "readiness",
                        "__pingora_health",
                        "__edge_probe",
                    ],
                ),
                stage="readiness",
            )
        )

    # ------------------------------------------------------------------
    # Backend unavailable / 502
    # ------------------------------------------------------------------

    if _contains_any(
        lower,
        [
            "connection refused",
            "couldn't connect to server",
            "bad gateway",
            "502",
            "upstream connect error",
        ],
    ):
        return done(
            _report(
                root_cause="upstream_backend_unavailable",
                confidence=0.88,
                severity="high",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="The gateway could not reach one or more configured upstream backend services.",
                repair_hint="Start the backend servers or switch to a self-contained demo mode that creates backend services automatically.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "connection refused",
                        "Couldn't connect to server",
                        "Bad Gateway",
                        "502",
                        "upstream",
                    ],
                ),
                stage=detected_stage,
            )
        )

    # ------------------------------------------------------------------
    # Blue/green generic fallback
    # ------------------------------------------------------------------

    if detected_stage == "bluegreen_deploy" or "bluegreen" in lower or "blue/green" in lower:
        return done(
            _report(
                root_cause="bluegreen_deployment_failure_unclassified",
                confidence=0.70,
                severity="high",
                safe_to_retry=False,
                traffic_switched=traffic_switched,
                rollback_required=traffic_switched,
                summary="Blue/green deployment failed, but no more specific deterministic signature matched.",
                repair_hint="Print and inspect the exact Compose command, stderr, container logs, health URL status, and generated compose file path.",
                evidence=_extract_evidence(
                    raw_text,
                    [
                        "blue",
                        "green",
                        "compose",
                        "container",
                        "health",
                        "error",
                        "failed",
                    ],
                ),
                stage="bluegreen_deploy",
            )
        )

    # ------------------------------------------------------------------
    # Final fallback
    # ------------------------------------------------------------------

    return done(
        _report(
            root_cause="unknown_failure",
            confidence=0.50,
            severity="medium",
            safe_to_retry=False,
            traffic_switched=traffic_switched,
            rollback_required=traffic_switched,
            summary="The failure did not match a known deterministic root-cause signature.",
            repair_hint="Capture command, stdout, stderr, generated config, container logs, and stage name, then add a new deterministic rule.",
            evidence=_extract_evidence(
                raw_text,
                [
                    "error",
                    "failed",
                    "panic",
                    "exception",
                    "traceback",
                    "502",
                    "compose",
                    "docker",
                ],
            ),
            stage=detected_stage,
        )
    )


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------


def format_root_cause_report(report: dict[str, Any]) -> str:
    evidence = report.get("evidence") or []

    evidence_text = ""
    if evidence:
        evidence_text = "\nEvidence:\n" + "\n".join(f"- {item}" for item in evidence)

    incident_text = ""
    if report.get("incident_id"):
        incident_text += f"Incident ID: {report.get('incident_id')}\n"

    if report.get("stored"):
        incident_text += f"Stored: true\nDB: {report.get('db_path')}\n"
    elif report.get("db_error"):
        incident_text += f"Stored: false\nDB error: {report.get('db_error')}\n"

    return (
        "🧠 Reliability report\n\n"
        f"{incident_text}"
        f"Stage: {report.get('stage', 'unknown')}\n"
        f"Root cause: {report.get('root_cause', 'unknown_failure')}\n"
        f"Confidence: {report.get('confidence', 0)}\n"
        f"Severity: {report.get('severity', 'medium')}\n"
        f"Safe to retry: {str(report.get('safe_to_retry', False)).lower()}\n"
        f"Traffic switched: {str(report.get('traffic_switched', False)).lower()}\n"
        f"Rollback required: {str(report.get('rollback_required', False)).lower()}\n\n"
        f"Summary: {report.get('summary', '')}\n"
        f"Repair hint: {report.get('repair_hint', '')}"
        f"{evidence_text}"
    )


def reliability_report_summary(
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> str:
    reports = list_reliability_reports(limit=limit, db_path=db_path)

    if not reports:
        return "No reliability reports stored yet."

    lines = ["🧠 Recent reliability reports"]

    for item in reports:
        lines.append(
            "- "
            f"{item.get('created_at_utc')} | "
            f"{item.get('stage')} | "
            f"{item.get('root_cause')} | "
            f"{item.get('severity')} | "
            f"{item.get('incident_id')}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Compatibility aliases
# ---------------------------------------------------------------------


def analyze_root_cause(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)


def infer_root_cause(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)


def diagnose_failure(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)


def classify_failure(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)


def run_root_cause_agent(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)


def root_cause_agent(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return classify_root_cause(*args, **kwargs)