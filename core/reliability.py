from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from agents.root_cause_agent import (
    RootCauseClassification,
    classify_failure,
    format_reliability_report,
)
from core.incident_store import IncidentStore


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
]


@dataclass(frozen=True)
class ReliabilityResult:
    run_id: Optional[str]
    incident_id: str
    classification: RootCauseClassification
    report: str


class ReliabilityBrain:
    """
    Deterministic reliability facade for MTTR tracking.

    Responsibilities:
    - start runs
    - finish runs
    - classify failures
    - persist incidents
    - record repair attempts
    - record deployment events
    - record rollback events
    - print operator-friendly reliability reports

    It does not deploy, switch traffic, or roll back by itself.
    """

    def __init__(
        self,
        store: Optional[IncidentStore] = None,
        db_path: str = "data/incidents.sqlite",
    ) -> None:
        self.store = store or IncidentStore(db_path=db_path)

    def start_run(
        self,
        prompt: str,
        flow: str = "create",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self.store.create_run(
            prompt=redact_secrets(prompt),
            flow=flow,
            metadata=redact_secrets(metadata or {}),
        )

    def finish_run(
        self,
        run_id: Optional[str],
        status: str = "success",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not run_id:
            return

        self.store.finish_run(
            run_id=run_id,
            status=status,
            metadata=redact_secrets(metadata or {}),
        )

    def record_failure(
        self,
        run_id: Optional[str],
        stage: str,
        error: BaseException | str,
        evidence: Optional[Dict[str, Any]] = None,
        traffic_switched: bool = False,
        finish_run: bool = True,
    ) -> ReliabilityResult:
        safe_evidence = redact_secrets(evidence or {})
        safe_error = redact_secrets(str(error))

        classification = classify_failure(
            stage=stage,
            error=safe_error,
            evidence=safe_evidence,
        )

        rollback_required = bool(classification.rollback_required or traffic_switched)

        incident_id = self.store.create_incident(
            run_id=run_id,
            stage=stage,
            root_cause=classification.root_cause,
            error_type=classification.error_type,
            severity=classification.severity,
            confidence=classification.confidence,
            message=safe_error,
            evidence=safe_evidence,
            status="open",
            safe_to_retry=classification.safe_to_retry,
            traffic_switched=traffic_switched,
            rollback_required=rollback_required,
        )

        if finish_run and run_id:
            self.store.finish_run(
                run_id,
                status="failed",
                metadata={
                    "failure_stage": stage,
                    "root_cause": classification.root_cause,
                    "incident_id": incident_id,
                },
            )

        report = format_reliability_report(
            run_id=run_id,
            incident_id=incident_id,
            stage=stage,
            classification=classification,
            traffic_switched=traffic_switched,
            rollback_required=rollback_required,
        )

        return ReliabilityResult(
            run_id=run_id,
            incident_id=incident_id,
            classification=classification,
            report=report,
        )

    def resolve_incident(
        self,
        incident_id: str,
        resolution: str = "resolved",
    ) -> Optional[float]:
        return self.store.resolve_incident(
            incident_id=incident_id,
            resolution=resolution,
        )

    def record_repair_attempt(
        self,
        incident_id: str,
        action: str,
        status: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self.store.record_repair_attempt(
            incident_id=incident_id,
            action=action,
            status=status,
            details=redact_secrets(details or {}),
        )

    def record_deployment_event(
        self,
        event_type: str,
        status: str,
        run_id: Optional[str] = None,
        color: Optional[str] = None,
        version: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self.store.record_deployment_event(
            event_type=event_type,
            status=status,
            run_id=run_id,
            color=color,
            version=version,
            details=redact_secrets(details or {}),
        )

    def record_rollback_event(
        self,
        status: str,
        run_id: Optional[str] = None,
        from_color: Optional[str] = None,
        to_color: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self.store.record_rollback_event(
            status=status,
            run_id=run_id,
            from_color=from_color,
            to_color=to_color,
            reason=redact_secrets(reason) if reason else None,
            details=redact_secrets(details or {}),
        )


def redact_secrets(value: Any) -> Any:
    """
    Redact common secret patterns from strings, lists, tuples, and dictionaries.

    This protects:
    - prompts persisted to SQLite
    - evidence stored with incidents
    - deployment/rollback event details
    - repair attempt details
    """

    if value is None:
        return None

    if isinstance(value, str):
        redacted = value

        for pattern in _SECRET_PATTERNS:
            if pattern.pattern.startswith("sk-"):
                redacted = pattern.sub("sk-REDACTED", redacted)
            else:
                redacted = pattern.sub(
                    lambda match: f"{match.group(1)}REDACTED",
                    redacted,
                )

        return redacted

    if isinstance(value, dict):
        result: Dict[Any, Any] = {}

        for key, item in value.items():
            key_text = str(key).lower()

            if any(
                token in key_text
                for token in ["secret", "token", "password", "api_key", "apikey"]
            ):
                result[key] = "REDACTED"
            else:
                result[key] = redact_secrets(item)

        return result

    if isinstance(value, list):
        return [redact_secrets(item) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)

    return value