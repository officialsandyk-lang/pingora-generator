from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from core.edge_router_writer import public_port_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
RUN_DIR = PROJECT_ROOT / "runtime"
PID_FILE = RUN_DIR / "local_gateway.pid"
LOG_FILE = RUN_DIR / "local_gateway.log"


class LocalRunnerError(RuntimeError):
    pass


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def stop_local_gateway() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    if not PID_FILE.exists():
        return

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return

    if not _pid_running(pid):
        PID_FILE.unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass

    deadline = time.time() + 8

    while time.time() < deadline:
        if not _pid_running(pid):
            PID_FILE.unlink(missing_ok=True)
            return

        time.sleep(0.25)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass

    PID_FILE.unlink(missing_ok=True)


def tcp_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout_seconds: int = 30) -> bool:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if tcp_port_open(host, port):
            return True

        time.sleep(0.5)

    return False


def start_local_gateway(
    config: dict[str, Any],
    *,
    project_dir: str | Path | None = None,
    stop_existing: bool = True,
) -> dict[str, Any]:
    project_path = Path(project_dir) if project_dir is not None else DEFAULT_PROJECT_DIR
    project_path = project_path.resolve()

    if not project_path.exists():
        raise LocalRunnerError(f"Generated project does not exist: {project_path}")

    cargo_toml = project_path / "Cargo.toml"
    src_main = project_path / "src" / "main.rs"

    if not cargo_toml.exists() or not src_main.exists():
        raise LocalRunnerError(
            f"Generated Rust project is incomplete: {project_path}"
        )

    RUN_DIR.mkdir(parents=True, exist_ok=True)

    if stop_existing:
        stop_local_gateway()

    port = public_port_from_config(config)

    if tcp_port_open("127.0.0.1", port):
        raise LocalRunnerError(
            f"Port {port} is already in use. Stop the process or choose another port."
        )

    log_handle = LOG_FILE.open("a", encoding="utf-8")

    log_handle.write("\n\n=== Starting local Pingora gateway ===\n")
    log_handle.flush()

    process = subprocess.Popen(
        ["cargo", "run"],
        cwd=str(project_path),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    PID_FILE.write_text(str(process.pid), encoding="utf-8")

    if not wait_for_port("127.0.0.1", port, timeout_seconds=45):
        return_code = process.poll()

        logs = ""
        try:
            logs = LOG_FILE.read_text(encoding="utf-8")[-4000:]
        except Exception:
            pass

        if return_code is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                pass

        PID_FILE.unlink(missing_ok=True)

        raise LocalRunnerError(
            f"Local gateway did not become ready on 127.0.0.1:{port}.\n\n"
            f"Logs:\n{logs}"
        )

    live_url = f"http://127.0.0.1:{port}"

    return {
        "success": True,
        "pid": process.pid,
        "live_url": live_url,
        "log_file": str(LOG_FILE),
        "project_dir": str(project_path),
    }
