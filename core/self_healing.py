from __future__ import annotations
import re

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
DEFAULT_RELIABILITY_DB = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


# ---------------------------------------------------------------------
# Optional project imports
# ---------------------------------------------------------------------


try:
    from agents.root_cause_agent import (
        classify_root_cause,
        format_root_cause_report,
        init_reliability_db,
    )
except Exception:
    classify_root_cause = None
    format_root_cause_report = None
    init_reliability_db = None


try:
    from agents.config_repair_agent import repair_config
except Exception:
    repair_config = None


try:
    from agents.runtime_agent import (
        repair_runtime_error,
        run_cargo_check,
        run_local_gateway,
    )
except Exception:
    repair_runtime_error = None
    run_cargo_check = None
    run_local_gateway = None


try:
    from core.validator import validate_config
except Exception:
    validate_config = None


try:
    from core.project_writer import write_project
except Exception:
    write_project = None


# ---------------------------------------------------------------------
# Known root-cause groups
# ---------------------------------------------------------------------


CONFIG_REPAIR_ROOT_CAUSES = {
    "nlp_route_algorithm_leak",
    "load_balance_alias_not_canonicalized",
    "weighted_round_robin_weights_stripped",
    "static_route_requires_backend_regression",
    "load_balancer_upstreams_dropped_during_generation",
    "invalid_docker_compose_yaml",
}

LOCAL_BACKEND_ROOT_CAUSES = {
    "old_simplehttp_demo_backend_404",
    "demo_backend_not_running_or_unreachable",
    "upstream_backend_unavailable",
}

LOCAL_RUNTIME_ROOT_CAUSES = {
    "local_gateway_port_conflict",
    "port_conflict",
    "proxy_backend_same_port_conflict",
    "local_gateway_live_but_application_probe_unclean",
    "local_runtime_startup_failure",
    "local_readiness_timeout",
    "health_probe_proxied_to_unavailable_upstream",
    "readiness_probe_failed",
    "local_runtime_failure",
}

CARGO_ROOT_CAUSES = {
    "cargo_check_failure",
}

DOCKER_MANUAL_ROOT_CAUSES = {
    "docker_unavailable",
    "docker_compose_unavailable",
    "docker_registry_or_network_failure",
    "host_docker_internal_unreachable",
    "missing_bluegreen_compose_artifact",
    "missing_compose_artifact",
    "bluegreen_deployment_failure_unclassified",
    "environment_preflight_failed",
}


# ---------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------


@dataclass
class CommandResult:
    command: list[str]
    cwd: str | None
    returncode: int
    stdout: str
    stderr: str


@dataclass
class SmokeCheck:
    route: str
    url: str
    status: int | None
    ok: bool
    backend: str | None
    summary: str


@dataclass
class HealingResult:
    heal_id: str
    created_at_utc: str
    completed_at_utc: str | None
    success: bool
    action: str
    root_cause: str
    stage: str
    incident_id: str | None
    project_dir: str
    config_path: str | None
    reliability_db: str
    before_report: dict[str, Any]
    after_report: dict[str, Any] | None
    smoke_checks: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    notes: list[str]
    error: str | None


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def make_heal_id(raw_text: str) -> str:
    return hash_text(f"{utc_now()}|{raw_text}")[:24]


def as_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def find_config_path(project_dir: str | Path | None = None) -> Path:
    project_path = Path(project_dir or DEFAULT_PROJECT_DIR).resolve()

    candidates = [
        project_path / "config.json",
        DEFAULT_PROJECT_DIR / "config.json",
        PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return project_path / "config.json"


def load_config(
    *,
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    project_dir: str | Path | None = None,
) -> tuple[dict[str, Any], Path | None]:
    if isinstance(config, dict):
        return config, Path(config_path).resolve() if config_path else None

    path = Path(config_path).resolve() if config_path else find_config_path(project_dir)

    if path.exists():
        return load_json(path), path

    return {}, path


def gateway_port(config: dict[str, Any], default: int = 8088) -> int:
    try:
        return int(
            config.get("port")
            or config.get("listen_port")
            or config.get("proxy_port")
            or default
        )
    except Exception:
        return default


def is_static_route(route: dict[str, Any]) -> bool:
    return str(route.get("type") or "").strip().lower() in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def normalize_upstream_address(value: Any) -> str | None:
    if isinstance(value, dict):
        value = (
            value.get("address")
            or value.get("upstream")
            or value.get("backend")
            or value.get("target")
            or value.get("url")
        )

    if value is None:
        return None

    text = str(value).strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    if ":" not in text:
        return None

    host, port_text = text.rsplit(":", 1)

    try:
        port = int(port_text)
    except Exception:
        return None

    if not (1 <= port <= 65535):
        return None

    if host == "localhost":
        host = "127.0.0.1"

    return f"{host}:{port}"


def port_from_address(address: str | None) -> int | None:
    if not address:
        return None

    try:
        return int(str(address).rsplit(":", 1)[-1])
    except Exception:
        return None


def collect_dynamic_backend_ports(config: dict[str, Any]) -> list[int]:
    ports: list[int] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return ports

    for route in routes:
        if not isinstance(route, dict):
            continue

        if is_static_route(route):
            continue

        values: list[Any] = []

        for key in ("upstream", "backend", "target"):
            if route.get(key):
                values.append(route.get(key))

        for key in ("upstreams", "backends", "backend_upstreams"):
            raw = route.get(key)

            if isinstance(raw, list):
                values.extend(raw)
            elif raw:
                values.append(raw)

        for value in values:
            if isinstance(value, str) and "," in value:
                items = [item.strip() for item in value.split(",") if item.strip()]
            else:
                items = [value]

            for item in items:
                address = normalize_upstream_address(item)
                port = port_from_address(address)

                if port is not None and port not in ports:
                    ports.append(port)

    return ports


def tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def http_get(url: str, timeout: float = 3.0) -> tuple[int | None, str, dict[str, str]]:
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ai-pingora-self-healing/1.0",
            },
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in response.headers.items()}

            return int(response.status), body, headers

    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)

        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return int(exc.code), body, headers

    except Exception as exc:
        return None, str(exc), {}


def run_command(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 120,
) -> CommandResult:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    return CommandResult(
        command=command,
        cwd=str(cwd) if cwd else None,
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def command_to_dict(result: CommandResult) -> dict[str, Any]:
    return asdict(result)


def kill_port(port: int) -> CommandResult:
    command = [
        "bash",
        "-lc",
        f"lsof -tiTCP:{int(port)} -sTCP:LISTEN | xargs -r kill -9",
    ]

    return run_command(command, cwd=PROJECT_ROOT, timeout=20)


# ---------------------------------------------------------------------
# SQLite persistence for healing attempts
# ---------------------------------------------------------------------


def init_self_healing_db(db_path: str | Path | None = None) -> Path:
    path = Path(db_path or DEFAULT_RELIABILITY_DB).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    if init_reliability_db is not None:
        try:
            init_reliability_db(path)
        except Exception:
            pass

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS self_healing_runs (
                heal_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                success INTEGER NOT NULL,
                action TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                stage TEXT NOT NULL,
                incident_id TEXT,
                project_dir TEXT NOT NULL,
                config_path TEXT,
                before_report_json TEXT NOT NULL,
                after_report_json TEXT,
                smoke_checks_json TEXT NOT NULL,
                commands_json TEXT NOT NULL,
                notes_json TEXT NOT NULL,
                error TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_self_healing_runs_created_at
            ON self_healing_runs(created_at_utc)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_self_healing_runs_root_cause
            ON self_healing_runs(root_cause)
            """
        )

        conn.commit()

    return path


def save_healing_result(result: HealingResult) -> None:
    db_path = init_self_healing_db(result.reliability_db)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO self_healing_runs (
                heal_id,
                created_at_utc,
                completed_at_utc,
                success,
                action,
                root_cause,
                stage,
                incident_id,
                project_dir,
                config_path,
                before_report_json,
                after_report_json,
                smoke_checks_json,
                commands_json,
                notes_json,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.heal_id,
                result.created_at_utc,
                result.completed_at_utc,
                int(bool(result.success)),
                result.action,
                result.root_cause,
                result.stage,
                result.incident_id,
                result.project_dir,
                result.config_path,
                json.dumps(result.before_report, indent=2, default=str),
                json.dumps(result.after_report, indent=2, default=str)
                if result.after_report is not None
                else None,
                json.dumps(result.smoke_checks, indent=2, default=str),
                json.dumps(result.commands, indent=2, default=str),
                json.dumps(result.notes, indent=2, default=str),
                result.error,
            ),
        )

        conn.commit()


def list_self_healing_runs(
    *,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    path = init_self_healing_db(db_path)

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT *
            FROM self_healing_runs
            ORDER BY created_at_utc DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    output: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        item["success"] = bool(item.get("success"))

        for key in (
            "before_report_json",
            "after_report_json",
            "smoke_checks_json",
            "commands_json",
            "notes_json",
        ):
            value = item.get(key)

            if value is None:
                item[key.replace("_json", "")] = None
            else:
                try:
                    item[key.replace("_json", "")] = json.loads(value)
                except Exception:
                    item[key.replace("_json", "")] = value

            item.pop(key, None)

        output.append(item)

    return output


def self_healing_summary(
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> str:
    runs = list_self_healing_runs(limit=limit, db_path=db_path)

    if not runs:
        return "No self-healing runs stored yet."

    lines = ["🩺 Recent self-healing runs"]

    for run in runs:
        lines.append(
            "- "
            f"{run.get('created_at_utc')} | "
            f"success={run.get('success')} | "
            f"{run.get('stage')} | "
            f"{run.get('root_cause')} | "
            f"{run.get('action')} | "
            f"{run.get('heal_id')}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Core repair actions
# ---------------------------------------------------------------------


def classify_failure(
    *,
    stage: str | None,
    error: Any = None,
    output: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    logs: Any = None,
    context: Any = None,
    persist: bool = True,
    db_path: str | Path | None = None,
    source: str = "self_healing",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if classify_root_cause is None:
        return {
            "root_cause": "root_cause_agent_unavailable",
            "confidence": 0.0,
            "severity": "high",
            "safe_to_retry": False,
            "traffic_switched": False,
            "rollback_required": False,
            "summary": "agents.root_cause_agent could not be imported.",
            "repair_hint": "Fix agents/root_cause_agent.py import errors.",
            "evidence": [],
            "stage": stage or "unknown",
            "stored": False,
        }

    return classify_root_cause(
        stage=stage,
        error=error,
        output=output,
        stdout=stdout,
        stderr=stderr,
        logs=logs,
        context=context,
        persist=persist,
        db_path=db_path,
        source=source,
        metadata=metadata,
    )


def repair_config_and_regenerate(
    *,
    config: dict[str, Any],
    prompt: str | None,
    project_dir: Path,
    config_path: Path | None,
    notes: list[str],
) -> dict[str, Any]:
    if repair_config is None:
        raise RuntimeError("agents.config_repair_agent.repair_config is unavailable.")

    if validate_config is None:
        raise RuntimeError("core.validator.validate_config is unavailable.")

    if write_project is None:
        raise RuntimeError("core.project_writer.write_project is unavailable.")

    repaired = repair_config(config, prompt=prompt)
    notes.append("config_repair_agent.repair_config completed")

    validated = validate_config(repaired)
    notes.append("core.validator.validate_config completed")

    write_project(validated, project_dir=project_dir)
    notes.append("core.project_writer.write_project regenerated project")

    if config_path is not None:
        write_json(config_path, validated)
        notes.append(f"locked config written to {config_path}")

    return validated


def run_cargo_check_for_project(
    *,
    project_dir: Path,
    commands: list[dict[str, Any]],
    notes: list[str],
) -> bool:
    if run_cargo_check is not None:
        result = run_cargo_check(project_dir, attempts=3)

        notes.append("agents.runtime_agent.run_cargo_check executed")

        if isinstance(result, dict):
            commands.append(
                {
                    "tool": "run_cargo_check",
                    "result": result,
                }
            )
            return bool(result.get("success") or result.get("ok"))

        return bool(result)

    result = run_command(["cargo", "check"], cwd=project_dir, timeout=180)
    commands.append(command_to_dict(result))
    notes.append("fallback cargo check command executed")

    return result.returncode == 0


def apply_runtime_repair(
    *,
    stage: str,
    error_text: str,
    project_dir: Path,
    commands: list[dict[str, Any]],
    notes: list[str],
) -> None:
    if repair_runtime_error is None:
        notes.append("runtime repair skipped because repair_runtime_error is unavailable")
        return

    report = repair_runtime_error(
        stage=stage,
        error=error_text,
        project_dir=str(project_dir),
    )

    commands.append(
        {
            "tool": "repair_runtime_error",
            "stage": stage,
            "result": report,
        }
    )

    notes.append("agents.runtime_agent.repair_runtime_error executed")


def restart_demo_backends(
    *,
    config_path: Path,
    commands: list[dict[str, Any]],
    notes: list[str],
    restart: bool = True,
) -> bool:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        notes.append("demo backend restart skipped because scripts/demo_backends.py is missing")
        return False

    action = "restart" if restart else "start"

    result = run_command(
        [sys.executable, str(script), action, str(config_path)],
        cwd=PROJECT_ROOT,
        timeout=60,
    )

    commands.append(command_to_dict(result))
    notes.append(f"demo_backends.py {action} executed")

    return result.returncode == 0


def start_local_gateway(
    *,
    config: dict[str, Any],
    project_dir: Path,
    commands: list[dict[str, Any]],
    notes: list[str],
    stop_existing: bool = True,
) -> bool:
    port = gateway_port(config)

    if run_local_gateway is not None:
        result = run_local_gateway(
            project_dir=project_dir,
            port=port,
            startup_timeout_seconds=90,
            stop_existing=stop_existing,
        )

        commands.append(
            {
                "tool": "run_local_gateway",
                "result": result,
            }
        )

        notes.append("agents.runtime_agent.run_local_gateway executed")

        if isinstance(result, dict):
            return bool(result.get("success") or result.get("ok") or result.get("runtime_ok"))

        return bool(result)

    if stop_existing:
        commands.append(command_to_dict(kill_port(port)))
        notes.append(f"fallback killed gateway port {port}")

    result = run_command(
        ["cargo", "run"],
        cwd=project_dir,
        timeout=2,
    )

    commands.append(command_to_dict(result))
    notes.append("fallback cargo run attempted")

    return tcp_open("127.0.0.1", port)


def stop_gateway_port(
    *,
    config: dict[str, Any],
    commands: list[dict[str, Any]],
    notes: list[str],
) -> None:
    port = gateway_port(config)
    result = kill_port(port)
    commands.append(command_to_dict(result))
    notes.append(f"killed anything listening on gateway port {port}")


# ---------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------


def smoke_test_gateway(
    *,
    config: dict[str, Any],
    host: str = "127.0.0.1",
    timeout: float = 3.0,
) -> list[SmokeCheck]:
    port = gateway_port(config)
    checks: list[SmokeCheck] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return [
            SmokeCheck(
                route="/",
                url=f"http://{host}:{port}/",
                status=None,
                ok=False,
                backend=None,
                summary="config routes is not a list",
            )
        ]

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = normalize_path(route.get("path") or "/")
        url_path = path

        if not url_path.endswith("/"):
            url_path += "/"

        url = f"http://{host}:{port}{url_path}"

        status, body, headers = http_get(url, timeout=timeout)
        backend = headers.get("x-demo-backend-port")

        if backend is None:
            match = re.search(r"Demo Backend\s+(\d+)", body)
            if match:
                backend = match.group(1)

        if is_static_route(route):
            ok = status is not None and 200 <= status < 500
            expected = "static"
        else:
            ok = status is not None and 200 <= status < 500 and status != 502
            expected = "proxy"

        if status is None:
            summary = f"{expected} route did not respond: {body[:160]}"
        elif ok:
            summary = f"{expected} route responded with HTTP {status}"
        else:
            summary = f"{expected} route responded with HTTP {status}: {body[:160]}"

        checks.append(
            SmokeCheck(
                route=path,
                url=url,
                status=status,
                ok=ok,
                backend=backend,
                summary=summary,
            )
        )

    return checks


def smoke_checks_ok(checks: list[SmokeCheck]) -> bool:
    if not checks:
        return False

    return all(check.ok for check in checks)


def smoke_checks_to_text(checks: list[SmokeCheck]) -> str:
    lines: list[str] = []

    for check in checks:
        backend = f" backend={check.backend}" if check.backend else ""
        lines.append(
            f"{check.route} status={check.status} ok={check.ok}{backend} url={check.url} {check.summary}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Self-healing orchestration
# ---------------------------------------------------------------------


def choose_action(root_cause: str) -> str:
    if root_cause in CONFIG_REPAIR_ROOT_CAUSES:
        return "repair_config_regenerate_cargo_restart_retest"

    if root_cause in LOCAL_BACKEND_ROOT_CAUSES:
        return "restart_demo_backends_retest"

    if root_cause in LOCAL_RUNTIME_ROOT_CAUSES:
        return "repair_or_restart_local_runtime_retest"

    if root_cause in CARGO_ROOT_CAUSES:
        return "runtime_repair_cargo_regenerate_restart_retest"

    if root_cause in DOCKER_MANUAL_ROOT_CAUSES:
        return "manual_required"

    if root_cause == "stable_local_runtime_verified":
        return "no_action_verified_good"

    return "generic_local_repair_attempt"


def heal_failure(
    *,
    stage: str | None = None,
    error: Any = None,
    output: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    logs: Any = None,
    prompt: str | None = None,
    config: dict[str, Any] | None = None,
    config_path: str | Path | None = None,
    project_dir: str | Path | None = None,
    runtime: str = "local",
    db_path: str | Path | None = None,
    source: str = "self_healing",
    metadata: dict[str, Any] | None = None,
    auto_restart_demo_backends: bool = True,
    auto_restart_gateway: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    project_path = Path(project_dir or DEFAULT_PROJECT_DIR).resolve()
    reliability_db = init_self_healing_db(db_path)

    loaded_config, loaded_config_path = load_config(
        config=config,
        config_path=config_path,
        project_dir=project_path,
    )

    raw_text = "\n".join(
        str(item)
        for item in [
            stage,
            error,
            output,
            stdout,
            stderr,
            logs,
            json.dumps(loaded_config, indent=2, default=str),
        ]
        if item is not None
    )

    heal_id = make_heal_id(raw_text)
    created_at = utc_now()

    commands: list[dict[str, Any]] = []
    notes: list[str] = []
    smoke_checks: list[SmokeCheck] = []
    after_report: dict[str, Any] | None = None
    success = False
    error_text: str | None = None

    before_report = classify_failure(
        stage=stage,
        error=error,
        output=output,
        stdout=stdout,
        stderr=stderr,
        logs=logs,
        context={
            "prompt": prompt,
            "config": loaded_config,
            "project_dir": str(project_path),
            "runtime": runtime,
        },
        persist=persist,
        db_path=reliability_db,
        source=source,
        metadata=metadata,
    )

    root_cause = str(before_report.get("root_cause") or "unknown_failure")
    detected_stage = str(before_report.get("stage") or stage or "unknown")
    incident_id = before_report.get("incident_id")
    action = choose_action(root_cause)

    try:
        if action == "no_action_verified_good":
            notes.append("No repair required because runtime is already verified good.")
            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

        elif action == "manual_required":
            notes.append(
                "No automatic repair applied because this root cause requires environment, Docker, network, or blue/green operator action."
            )
            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = False

        elif action == "restart_demo_backends_retest":
            if loaded_config_path is None:
                raise RuntimeError("Cannot restart demo backends without config path.")

            if auto_restart_demo_backends:
                restart_demo_backends(
                    config_path=loaded_config_path,
                    commands=commands,
                    notes=notes,
                    restart=True,
                )

            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

        elif action == "repair_or_restart_local_runtime_retest":
            if root_cause in {"local_gateway_port_conflict", "port_conflict", "proxy_backend_same_port_conflict"}:
                stop_gateway_port(
                    config=loaded_config,
                    commands=commands,
                    notes=notes,
                )

            if root_cause in {
                "local_runtime_startup_failure",
                "local_readiness_timeout",
                "local_runtime_failure",
                "health_probe_proxied_to_unavailable_upstream",
                "readiness_probe_failed",
            }:
                apply_runtime_repair(
                    stage=detected_stage,
                    error_text=raw_text,
                    project_dir=project_path,
                    commands=commands,
                    notes=notes,
                )

            cargo_ok = run_cargo_check_for_project(
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            if not cargo_ok:
                raise RuntimeError("cargo check still failed after runtime repair")

            if auto_restart_gateway:
                start_local_gateway(
                    config=loaded_config,
                    project_dir=project_path,
                    commands=commands,
                    notes=notes,
                    stop_existing=True,
                )

            if auto_restart_demo_backends and loaded_config_path is not None:
                restart_demo_backends(
                    config_path=loaded_config_path,
                    commands=commands,
                    notes=notes,
                    restart=False,
                )

            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

        elif action == "runtime_repair_cargo_regenerate_restart_retest":
            apply_runtime_repair(
                stage=detected_stage,
                error_text=raw_text,
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            cargo_ok = run_cargo_check_for_project(
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            if not cargo_ok:
                raise RuntimeError("cargo check still failed after runtime repair")

            if auto_restart_gateway:
                start_local_gateway(
                    config=loaded_config,
                    project_dir=project_path,
                    commands=commands,
                    notes=notes,
                    stop_existing=True,
                )

            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

        elif action == "repair_config_regenerate_cargo_restart_retest":
            repaired_config = repair_config_and_regenerate(
                config=loaded_config,
                prompt=prompt,
                project_dir=project_path,
                config_path=loaded_config_path,
                notes=notes,
            )

            loaded_config = repaired_config

            cargo_ok = run_cargo_check_for_project(
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            if not cargo_ok:
                raise RuntimeError("cargo check failed after config repair/regeneration")

            if auto_restart_gateway:
                start_local_gateway(
                    config=loaded_config,
                    project_dir=project_path,
                    commands=commands,
                    notes=notes,
                    stop_existing=True,
                )

            if auto_restart_demo_backends and loaded_config_path is not None:
                restart_demo_backends(
                    config_path=loaded_config_path,
                    commands=commands,
                    notes=notes,
                    restart=False,
                )

            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

        else:
            notes.append("Generic repair started.")

            if repair_config is not None and validate_config is not None and write_project is not None:
                loaded_config = repair_config_and_regenerate(
                    config=loaded_config,
                    prompt=prompt,
                    project_dir=project_path,
                    config_path=loaded_config_path,
                    notes=notes,
                )

            apply_runtime_repair(
                stage=detected_stage,
                error_text=raw_text,
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            run_cargo_check_for_project(
                project_dir=project_path,
                commands=commands,
                notes=notes,
            )

            if auto_restart_gateway:
                start_local_gateway(
                    config=loaded_config,
                    project_dir=project_path,
                    commands=commands,
                    notes=notes,
                    stop_existing=True,
                )

            if auto_restart_demo_backends and loaded_config_path is not None:
                restart_demo_backends(
                    config_path=loaded_config_path,
                    commands=commands,
                    notes=notes,
                    restart=False,
                )

            smoke_checks = smoke_test_gateway(config=loaded_config)
            success = smoke_checks_ok(smoke_checks)

    except Exception as exc:
        success = False
        error_text = str(exc)
        notes.append(f"self-healing failed: {exc}")

    after_output = (
        f"self_healing_success={success}\n"
        f"root_cause={root_cause}\n"
        f"action={action}\n"
        f"notes={json.dumps(notes, indent=2)}\n"
        f"smoke_checks=\n{smoke_checks_to_text(smoke_checks)}\n"
        f"error={error_text or ''}"
    )

    after_report = classify_failure(
        stage="self_healing",
        output=after_output,
        context={
            "prompt": prompt,
            "config": loaded_config,
            "before_report": before_report,
            "smoke_checks": [asdict(check) for check in smoke_checks],
        },
        persist=persist,
        db_path=reliability_db,
        source=f"{source}_after",
        metadata={
            "heal_id": heal_id,
            "root_cause": root_cause,
            "action": action,
            "success": success,
        },
    )

    result = HealingResult(
        heal_id=heal_id,
        created_at_utc=created_at,
        completed_at_utc=utc_now(),
        success=success,
        action=action,
        root_cause=root_cause,
        stage=detected_stage,
        incident_id=incident_id,
        project_dir=str(project_path),
        config_path=str(loaded_config_path) if loaded_config_path else None,
        reliability_db=str(reliability_db),
        before_report=before_report,
        after_report=after_report,
        smoke_checks=[asdict(check) for check in smoke_checks],
        commands=commands,
        notes=notes,
        error=error_text,
    )

    save_healing_result(result)

    return asdict(result)


def format_healing_result(result: dict[str, Any]) -> str:
    checks = result.get("smoke_checks") or []
    commands = result.get("commands") or []
    notes = result.get("notes") or []

    lines = [
        "🩺 Self-healing result",
        "",
        f"Heal ID: {result.get('heal_id')}",
        f"Success: {str(result.get('success')).lower()}",
        f"Stage: {result.get('stage')}",
        f"Root cause: {result.get('root_cause')}",
        f"Action: {result.get('action')}",
        f"Incident ID: {result.get('incident_id')}",
        f"DB: {result.get('reliability_db')}",
    ]

    if result.get("error"):
        lines.append(f"Error: {result.get('error')}")

    if notes:
        lines.append("")
        lines.append("Notes:")
        for note in notes:
            lines.append(f"- {note}")

    if checks:
        lines.append("")
        lines.append("Smoke checks:")
        for check in checks:
            backend = f" backend={check.get('backend')}" if check.get("backend") else ""
            lines.append(
                f"- {check.get('route')} status={check.get('status')} ok={check.get('ok')}{backend}"
            )

    if commands:
        lines.append("")
        lines.append(f"Commands/tools recorded: {len(commands)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------


def heal_exception(
    exc: BaseException,
    *,
    stage: str,
    prompt: str | None = None,
    config: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    return heal_failure(
        stage=stage,
        error=str(exc),
        prompt=prompt,
        config=config,
        project_dir=project_dir,
        config_path=config_path,
        source="heal_exception",
    )


def heal_from_report(
    report: dict[str, Any],
    *,
    prompt: str | None = None,
    config: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    return heal_failure(
        stage=str(report.get("stage") or "unknown"),
        error=report.get("summary"),
        output=json.dumps(report, indent=2, default=str),
        prompt=prompt,
        config=config,
        project_dir=project_dir,
        config_path=config_path,
        source="heal_from_report",
    )


# Compatibility aliases
def self_heal(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return heal_failure(*args, **kwargs)


def run_self_healing(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return heal_failure(*args, **kwargs)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI Pingora Gateway self-healing orchestrator."
    )

    parser.add_argument("--stage", default="unknown")
    parser.add_argument("--error", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--error-file", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT_DIR))
    parser.add_argument("--config-path", default=None)
    parser.add_argument("--db-path", default=str(DEFAULT_RELIABILITY_DB))
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.summary:
        print(self_healing_summary(db_path=args.db_path))
        return 0

    error_text = args.error
    output_text = args.output
    prompt_text = args.prompt

    if args.error_file:
        error_text = read_text(Path(args.error_file))

    if args.prompt_file:
        prompt_text = read_text(Path(args.prompt_file))

    result = heal_failure(
        stage=args.stage,
        error=error_text,
        output=output_text,
        prompt=prompt_text,
        project_dir=args.project_dir,
        config_path=args.config_path,
        db_path=args.db_path,
        source="self_healing_cli",
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_healing_result(result))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())