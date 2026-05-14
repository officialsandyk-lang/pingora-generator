from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn

        return decorator


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


def _text(*values: Any) -> str:
    parts: list[str] = []

    for value in values:
        if value is None:
            continue

        if isinstance(value, dict):
            parts.append(str(value))
            continue

        if isinstance(value, list):
            parts.append("\n".join(str(item) for item in value))
            continue

        parts.append(str(value))

    return "\n".join(parts)


def _lower(*values: Any) -> str:
    return _text(*values).lower()


def _contains_any(text: str, markers: list[str]) -> bool:
    return any(marker.lower() in text for marker in markers)


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

    if "bluegreen" in lower or "blue/green" in lower:
        return "bluegreen_deploy"

    if "local_runtime" in lower or "local runtime" in lower:
        return "local_runtime"

    if "docker compose" in lower or "compose" in lower:
        return "compose"

    if "health" in lower or "readiness" in lower:
        return "readiness"

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
        or "error:" in lower and "rustc" in lower
    )


def _has_port_conflict(text: str) -> bool:
    lower = text.lower()

    return (
        "address already in use" in lower
        or "addrinuse" in lower
        or "bind() failed" in lower
        or "port is already in use" in lower
        or "0.0.0.0:" in lower and "still in use" in lower
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


@traceable(name="root_cause_agent_classify", run_type="chain")
def classify_root_cause(
    stage: str | None = None,
    error: Any = None,
    output: Any = None,
    stderr: Any = None,
    stdout: Any = None,
    logs: Any = None,
    context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Deterministic root-cause classifier.

    It avoids vague answers like:
      bluegreen_or_compose_deployment_failure

    and tries to return specific causes like:
      docker_unavailable
      docker_compose_unavailable
      proxy_backend_same_port_conflict
      health_probe_proxied_to_unavailable_upstream
      host_docker_internal_unreachable
      local_runtime_startup_failure
      cargo_check_failure
      missing_bluegreen_compose_artifact
    """

    raw_text = _text(stage, error, output, stderr, stdout, logs, context, kwargs)
    lower = raw_text.lower()
    detected_stage = _stage_from_text(stage, raw_text)

    traffic_switched = _traffic_switched_from_text(raw_text)

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
        return _report(
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

    if _contains_any(
        lower,
        [
            "docker compose is not available",
            "docker: 'compose' is not a docker command",
            "compose is not available",
        ],
    ):
        return _report(
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

    if "preflight failed" in lower:
        return _report(
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

    # ------------------------------------------------------------------
    # Cargo check
    # ------------------------------------------------------------------

    if _cargo_check_failed(raw_text):
        return _report(
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

    # ------------------------------------------------------------------
    # Local runtime
    # ------------------------------------------------------------------

    if detected_stage == "local_runtime" or "local runtime" in lower:
        if _has_port_conflict(raw_text):
            return _report(
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

        if _contains_any(
            lower,
            [
                "generated pingora process exited before becoming ready",
                "local_runtime_startup_failure",
                "process exited before becoming ready",
            ],
        ):
            return _report(
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

        if _contains_any(
            lower,
            [
                "local_readiness_timeout",
                "did not become ready",
                "readiness timeout",
            ],
        ):
            return _report(
                root_cause="local_readiness_timeout",
                confidence=0.92,
                severity="high",
                safe_to_retry=True,
                traffic_switched=False,
                rollback_required=False,
                summary="The local Pingora process started but did not become ready before timeout.",
                repair_hint="Check whether port 8088 is open, whether `/` responds, and whether generated health endpoints are implemented locally.",
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

        return _report(
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
        return _report(
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

        return _report(
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

    if _contains_any(
        lower,
        [
            "generated rust dropped one or more load-balancer upstreams",
            "ignored route['upstreams']",
            "missing from src/main.rs",
            "docker/compose helper rewrote only route['upstream']",
        ],
    ):
        return _report(
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
        return _report(
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

    # ------------------------------------------------------------------
    # Port conflicts
    # ------------------------------------------------------------------

    if _is_proxy_backend_same_port_conflict(raw_text):
        return _report(
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

    if _has_port_conflict(raw_text):
        return _report(
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
        return _report(
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
            return _report(
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

        return _report(
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
        return _report(
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

    # ------------------------------------------------------------------
    # Blue/green generic fallback
    # ------------------------------------------------------------------

    if detected_stage == "bluegreen_deploy" or "bluegreen" in lower or "blue/green" in lower:
        return _report(
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

    # ------------------------------------------------------------------
    # Final fallback
    # ------------------------------------------------------------------

    return _report(
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


def format_root_cause_report(report: dict[str, Any]) -> str:
    evidence = report.get("evidence") or []

    evidence_text = ""
    if evidence:
        evidence_text = "\nEvidence:\n" + "\n".join(f"- {item}" for item in evidence)

    return (
        "🧠 Reliability report\n\n"
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