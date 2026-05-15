from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / ".runtime" / "demo-backends"
LOG_DIR = PROJECT_ROOT / "tmp" / "logs"


def normalize_upstream_address(value: Any) -> str | None:
    if value is None:
        return None

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


def port_from_address(address: str) -> int | None:
    try:
        return int(str(address).rsplit(":", 1)[-1])
    except Exception:
        return None


def collect_ports_from_config(config: dict[str, Any]) -> list[int]:
    ports: list[int] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return ports

    for route in routes:
        if not isinstance(route, dict):
            continue

        # Static routes are served by Pingora itself.
        if str(route.get("type") or "").lower() == "static":
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
                items = [part.strip() for part in value.split(",") if part.strip()]
            else:
                items = [value]

            for item in items:
                address = normalize_upstream_address(item)

                if not address:
                    continue

                port = port_from_address(address)

                if port is not None and port not in ports:
                    ports.append(port)

    return ports


def load_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path).resolve()

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_file(port: int) -> Path:
    return RUNTIME_DIR / f"demo-backend-{port}.pid"


def log_file(port: int) -> Path:
    return LOG_DIR / f"demo-backend-{port}.log"


def read_pid(port: int) -> int | None:
    path = pid_file(port)

    if not path.exists():
        return None

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        path.unlink(missing_ok=True)
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def stop_port(port: int) -> dict[str, Any]:
    pid = read_pid(port)

    if pid is None:
        return {
            "port": port,
            "status": "not_managed",
            "stopped": False,
        }

    try:
        if pid_alive(pid):
            os.kill(pid, signal.SIGTERM)

            deadline = time.time() + 5

            while time.time() < deadline:
                if not pid_alive(pid):
                    break

                time.sleep(0.2)

            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)

        pid_file(port).unlink(missing_ok=True)

        return {
            "port": port,
            "pid": pid,
            "status": "stopped",
            "stopped": True,
        }

    except Exception as exc:
        return {
            "port": port,
            "pid": pid,
            "status": "stop_failed",
            "stopped": False,
            "error": str(exc),
        }


def start_port(port: int) -> dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if tcp_open(port):
        return {
            "port": port,
            "status": "already_running",
            "started": False,
            "url": f"http://127.0.0.1:{port}/",
        }

    log_path = log_file(port)

    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                str(port),
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            start_new_session=True,
        )

    pid_file(port).write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 8

    while time.time() < deadline:
        if process.poll() is not None:
            return {
                "port": port,
                "status": "exited",
                "started": False,
                "pid": process.pid,
                "url": f"http://127.0.0.1:{port}/",
                "log_file": str(log_path),
            }

        if tcp_open(port):
            return {
                "port": port,
                "status": "started",
                "started": True,
                "pid": process.pid,
                "url": f"http://127.0.0.1:{port}/",
                "log_file": str(log_path),
            }

        time.sleep(0.2)

    return {
        "port": port,
        "status": "timeout",
        "started": False,
        "pid": process.pid,
        "url": f"http://127.0.0.1:{port}/",
        "log_file": str(log_path),
    }


def make_handler(port: int):
    class DemoBackendHandler(BaseHTTPRequestHandler):
        server_version = "DemoBackend/1.0"

        def do_HEAD(self):
            self._send_response(include_body=False)

        def do_GET(self):
            self._send_response(include_body=True)

        def do_POST(self):
            self._send_response(include_body=True)

        def do_PUT(self):
            self._send_response(include_body=True)

        def do_PATCH(self):
            self._send_response(include_body=True)

        def do_DELETE(self):
            self._send_response(include_body=True)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")
            self.end_headers()

        def _send_response(self, include_body: bool = True):
            body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Demo Backend {port}</title>
</head>
<body>
  <h1>✅ Demo Backend {port}</h1>
  <p>This response came from backend port <strong>{port}</strong>.</p>
  <p>Request path: <code>{self.path}</code></p>
  <p>Generated by AI Pingora Gateway.</p>
</body>
</html>
""".encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Demo-Backend-Port", str(port))
            self.end_headers()

            if include_body:
                self.wfile.write(body)

        def log_message(self, format: str, *args):
            print(f"[demo-backend:{port}] {self.address_string()} - {format % args}")

    return DemoBackendHandler


def serve(port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(port))
    print(f"Demo backend serving on http://127.0.0.1:{port}/", flush=True)

    try:
        server.serve_forever()
    finally:
        server.server_close()


def command_start(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    ports = collect_ports_from_config(config)

    results = [start_port(port) for port in ports]

    return {
        "ports": ports,
        "started": [item for item in results if item.get("started")],
        "already_running": [
            item for item in results if item.get("status") == "already_running"
        ],
        "results": results,
    }


def command_stop(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    ports = collect_ports_from_config(config)

    results = [stop_port(port) for port in ports]

    return {
        "ports": ports,
        "results": results,
    }


def command_restart(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    ports = collect_ports_from_config(config)

    stopped = [stop_port(port) for port in ports]

    # Give sockets a moment to release.
    time.sleep(0.5)

    started = [start_port(port) for port in ports]

    return {
        "ports": ports,
        "stopped": stopped,
        "started": started,
    }


def command_status(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    ports = collect_ports_from_config(config)

    results = []

    for port in ports:
        results.append(
            {
                "port": port,
                "listening": tcp_open(port),
                "pid": read_pid(port),
                "url": f"http://127.0.0.1:{port}/",
            }
        )

    return {
        "ports": ports,
        "results": results,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage:\n"
            "  python scripts/demo_backends.py start <config.json>\n"
            "  python scripts/demo_backends.py stop <config.json>\n"
            "  python scripts/demo_backends.py restart <config.json>\n"
            "  python scripts/demo_backends.py status <config.json>\n"
            "  python scripts/demo_backends.py serve <port>",
            file=sys.stderr,
        )
        return 2

    command = argv[1].strip().lower()

    if command == "serve":
        if len(argv) < 3:
            print("Missing port.", file=sys.stderr)
            return 2

        serve(int(argv[2]))
        return 0

    if len(argv) < 3:
        print("Missing config path.", file=sys.stderr)
        return 2

    config_path = argv[2]

    if command == "start":
        result = command_start(config_path)
    elif command == "stop":
        result = command_stop(config_path)
    elif command == "restart":
        result = command_restart(config_path)
    elif command == "status":
        result = command_status(config_path)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))