from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR_NAME = "generated-pingora-proxy"

REQUIRED_PYTHON_PACKAGES = [
    "openai",
    "langgraph",
]

REQUIRED_COMMANDS = [
    "python3",
    "cargo",
    "rustc",
    "cmake",
    "pkg-config",
    "gcc",
    "g++",
]

PACKAGE_INIT_DIRS = [
    "ai",
    "agents",
    "core",
    "orchestration",
]


class PreflightError(RuntimeError):
    def __init__(self, message: str, errors: list[dict[str, Any]]):
        super().__init__(message)
        self.errors = errors


class ConfigPreflightError(PreflightError):
    pass


def run_command(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def command_exists(command_name: str) -> bool:
    return shutil.which(command_name) is not None


def python_package_exists(package_name: str) -> bool:
    return importlib.util.find_spec(package_name) is not None


def ensure_init_files():
    for folder_name in PACKAGE_INIT_DIRS:
        folder = PROJECT_ROOT / folder_name
        init_file = folder / "__init__.py"

        if folder.exists() and not init_file.exists():
            init_file.write_text("")
            print(f"✅ Created missing package file: {init_file}")


def check_working_directory():
    cwd = Path.cwd().resolve()

    if cwd.name == GENERATED_DIR_NAME:
        return {
            "ok": False,
            "message": "You are running from inside generated-pingora-proxy.",
            "fix": (
                "Run from the project root instead:\n\n"
                f"cd {PROJECT_ROOT}\n"
                "source .venv/bin/activate\n"
                "python main.py"
            ),
        }

    return {
        "ok": True,
        "message": "Working directory is safe.",
        "fix": None,
    }


def check_python_environment():
    issues = []

    for package in REQUIRED_PYTHON_PACKAGES:
        if not python_package_exists(package):
            issues.append(
                {
                    "message": f"Missing Python package: {package}",
                    "fix": (
                        "Install it in the active environment:\n\n"
                        f"{sys.executable} -m pip install {package}"
                    ),
                }
            )

    return issues


def check_openai_key():
    if os.environ.get("OPENAI_API_KEY"):
        return {
            "ok": True,
            "message": "OPENAI_API_KEY is set.",
            "fix": None,
        }

    return {
        "ok": False,
        "message": "OPENAI_API_KEY is not set in this environment.",
        "fix": (
            "Set it in WSL:\n\n"
            "export OPENAI_API_KEY='your_api_key_here'\n\n"
            "For permanent setup:\n\n"
            "echo \"export OPENAI_API_KEY='your_api_key_here'\" >> ~/.bashrc\n"
            "source ~/.bashrc"
        ),
    }


def check_required_commands():
    issues = []

    for command in REQUIRED_COMMANDS:
        if not command_exists(command):
            if command in {"cargo", "rustc"}:
                fix = (
                    "Install Rust:\n\n"
                    "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\n"
                    "source ~/.cargo/env"
                )
            elif command in {"cmake", "pkg-config", "gcc", "g++"}:
                fix = (
                    "Install build tools:\n\n"
                    "sudo apt update\n"
                    "sudo apt install -y build-essential cmake pkg-config libssl-dev"
                )
            else:
                fix = f"Install missing command: {command}"

            issues.append(
                {
                    "message": f"Missing command: {command}",
                    "fix": fix,
                }
            )

    return issues


def check_openssl_pkg_config():
    if not command_exists("pkg-config"):
        return {
            "ok": False,
            "message": "pkg-config is missing, cannot check OpenSSL development files.",
            "fix": "sudo apt install -y pkg-config libssl-dev",
        }

    result = run_command(["pkg-config", "--exists", "openssl"])

    if result.returncode == 0:
        return {
            "ok": True,
            "message": "OpenSSL development files are available.",
            "fix": None,
        }

    return {
        "ok": False,
        "message": "OpenSSL development files are missing or not visible to pkg-config.",
        "fix": "sudo apt install -y libssl-dev pkg-config",
    }


def check_docker_available():
    if not command_exists("docker"):
        return {
            "ok": False,
            "message": "Docker command not found in this environment.",
            "fix": (
                "Install/start Docker Desktop on Windows and enable WSL integration:\n\n"
                "Docker Desktop → Settings → General → Use WSL 2 based engine\n"
                "Docker Desktop → Settings → Resources → WSL Integration → enable Ubuntu\n\n"
                "Then restart WSL:\n\n"
                "wsl --shutdown"
            ),
        }

    result = run_command(["docker", "ps"])

    if result.returncode == 0:
        return {
            "ok": True,
            "message": "Docker is available.",
            "fix": None,
        }

    stderr = result.stderr.lower()

    if "permission denied" in stderr:
        return {
            "ok": False,
            "message": "Docker permission denied for current WSL user.",
            "fix": (
                "Run:\n\n"
                "sudo usermod -aG docker $USER\n\n"
                "Then fully restart WSL from PowerShell:\n\n"
                "wsl --shutdown\n\n"
                "Then reopen WSL and test:\n\n"
                "docker ps"
            ),
        }

    if "cannot connect to the docker daemon" in stderr:
        return {
            "ok": False,
            "message": "Docker daemon is not running or not reachable.",
            "fix": (
                "Start Docker Desktop, then restart WSL:\n\n"
                "wsl --shutdown\n\n"
                "Then reopen WSL and run:\n\n"
                "docker ps"
            ),
        }

    return {
        "ok": False,
        "message": "Docker is not available to this Python environment.",
        "fix": result.stderr.strip() or "Start Docker Desktop and enable WSL integration.",
    }


def check_docker_compose_available():
    if not command_exists("docker"):
        return {
            "ok": False,
            "message": "Docker command not found, so Docker Compose cannot be checked.",
            "fix": "Install/start Docker Desktop and enable WSL integration.",
        }

    result = run_command(["docker", "compose", "version"])

    if result.returncode == 0:
        return {
            "ok": True,
            "message": "Docker Compose is available.",
            "fix": None,
        }

    return {
        "ok": False,
        "message": "Docker Compose is not available.",
        "fix": (
            "Use Docker Desktop with Docker Compose v2 enabled.\n"
            "Test with:\n\n"
            "docker compose version"
        ),
    }


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def check_port_available(port: int):
    if port_is_free(port):
        return {
            "ok": True,
            "message": f"Port {port} is free.",
            "fix": None,
        }

    return {
        "ok": False,
        "message": f"Port {port} is already in use.",
        "fix": (
            f"Find the process:\n\n"
            f"ss -ltnp | grep :{port}\n\n"
            "Then stop the process or choose another port.\n"
            "If it is an old Docker Compose stack:\n\n"
            "cd generated-pingora-proxy && docker compose down"
        ),
    }


def extract_port_from_upstream(upstream: Any) -> int | None:
    text = str(upstream or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    if ":" not in text:
        return None

    port_text = text.rsplit(":", 1)[-1]

    if not port_text.isdigit():
        return None

    port = int(port_text)

    if 1 <= port <= 65535:
        return port

    return None


def route_upstreams(route: dict[str, Any]) -> list[str]:
    upstreams: list[str] = []

    if route.get("upstream"):
        upstreams.append(str(route["upstream"]))

    if route.get("backend"):
        upstreams.append(str(route["backend"]))

    if route.get("target"):
        upstreams.append(str(route["target"]))

    raw_upstreams = route.get("upstreams")

    if isinstance(raw_upstreams, list):
        for item in raw_upstreams:
            if isinstance(item, str):
                upstreams.append(item)
            elif isinstance(item, dict):
                value = (
                    item.get("address")
                    or item.get("upstream")
                    or item.get("backend")
                    or item.get("target")
                    or item.get("url")
                )

                if value:
                    upstreams.append(str(value))

    load_balancer = route.get("load_balancer")

    if isinstance(load_balancer, dict):
        lb_upstreams = load_balancer.get("upstreams")

        if isinstance(lb_upstreams, list):
            for item in lb_upstreams:
                if isinstance(item, str):
                    upstreams.append(item)
                elif isinstance(item, dict):
                    value = (
                        item.get("address")
                        or item.get("upstream")
                        or item.get("backend")
                        or item.get("target")
                        or item.get("url")
                    )

                    if value:
                        upstreams.append(str(value))

    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []

    for upstream in upstreams:
        if upstream not in seen:
            unique.append(upstream)
            seen.add(upstream)

    return unique


def check_proxy_backend_port_conflict(config: dict[str, Any]):
    """
    Prevents this broken setup:

      gateway/listen port: 3000
      backend/upstream:    127.0.0.1:3000

    That causes Pingora and the backend to fight for the same port.

    This check must stay enabled in both create and update flows.
    """

    try:
        proxy_port = int(
            config.get("port")
            or config.get("listen_port")
            or config.get("proxy_port")
            or config.get("public_port")
            or 0
        )
    except Exception:
        return {
            "ok": True,
            "message": "Proxy port not available for conflict check.",
            "fix": None,
        }

    if not (1 <= proxy_port <= 65535):
        return {
            "ok": True,
            "message": "Proxy port is not valid, skipping backend conflict check.",
            "fix": None,
        }

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return {
            "ok": True,
            "message": "Routes are not a list, skipping backend conflict check.",
            "fix": None,
        }

    conflicts = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_path = str(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        for upstream in route_upstreams(route):
            upstream_port = extract_port_from_upstream(upstream)

            if upstream_port == proxy_port:
                conflicts.append(
                    {
                        "route": route_path,
                        "upstream": upstream,
                        "port": upstream_port,
                    }
                )

    if not conflicts:
        return {
            "ok": True,
            "message": "No proxy/backend port conflicts detected.",
            "fix": None,
        }

    conflict_lines = "\n".join(
        f"- {item['route']} -> {item['upstream']}"
        for item in conflicts
    )

    return {
        "ok": False,
        "message": (
            f"Proxy/backend port conflict detected. "
            f"The gateway is configured to listen on port {proxy_port}, "
            f"but one or more backends also use port {proxy_port}."
        ),
        "fix": (
            f"Conflicting route(s):\n\n"
            f"{conflict_lines}\n\n"
            f"Use a different gateway/listen port, for example 8088, "
            f"or use a different backend port.\n\n"
            f"Example:\n\n"
            f'python main.py "create proxy on port 8088 with / to backend {proxy_port}"'
        ),
    }


def print_failures(errors: list[dict[str, Any]]):
    print("")
    print("❌ Preflight failed")

    for error in errors:
        print("")
        print(f"- {error['message']}")
        print("")

        fix = error.get("fix")
        if fix:
            print(fix)

    print("")


def preflight_check(use_docker: bool = True, use_compose: bool = True):
    """
    Preflight Doctor.

    Runs before prompt/config generation.
    Detects environment problems before the generator starts.
    Auto-fixes only safe things, such as missing __init__.py files.
    """

    print("🧪 Running preflight checks...")

    ensure_init_files()

    errors = []

    cwd_result = check_working_directory()
    if not cwd_result["ok"]:
        errors.append(cwd_result)

    errors.extend(check_python_environment())

    openai_result = check_openai_key()
    if not openai_result["ok"]:
        errors.append(openai_result)

    errors.extend(check_required_commands())

    openssl_result = check_openssl_pkg_config()
    if not openssl_result["ok"]:
        errors.append(openssl_result)

    if use_docker or use_compose:
        docker_result = check_docker_available()
        if not docker_result["ok"]:
            errors.append(docker_result)

    if use_compose:
        compose_result = check_docker_compose_available()
        if not compose_result["ok"]:
            errors.append(compose_result)

    if errors:
        print_failures(errors)
        raise PreflightError("Preflight checks failed.", errors)

    print("✅ Preflight checks passed")


def preflight_check_config(
    config: dict[str, Any],
    use_docker: bool = True,
    use_compose: bool = True,
    check_listen_port_available: bool = True,
):
    """
    Runs after validation/security, when we know the requested proxy/backend ports.

    check_listen_port_available:
      True:
        create/local mode should fail if the gateway listen port is already used.

      False:
        update/blue-green mode may allow the public/edge port to already be live.

    Important:
      Proxy/backend same-port conflict is still checked even when
      check_listen_port_available=False.
    """

    print("🧪 Running config-level preflight checks...")

    errors = []

    try:
        proxy_port = int(
            config.get("port")
            or config.get("listen_port")
            or config.get("proxy_port")
            or config.get("public_port")
            or 0
        )
    except Exception:
        proxy_port = 0

    conflict_result = check_proxy_backend_port_conflict(config)

    if not conflict_result["ok"]:
        errors.append(conflict_result)

    if proxy_port:
        if check_listen_port_available:
            port_result = check_port_available(proxy_port)

            if not port_result["ok"]:
                errors.append(port_result)
        else:
            print(
                f"ℹ️ Skipping listen-port availability check for port {proxy_port} "
                f"because update/blue-green mode may already own it."
            )
    else:
        errors.append(
            {
                "message": "Gateway/proxy port is missing or invalid.",
                "fix": (
                    "Provide a valid gateway port.\n\n"
                    "Example:\n\n"
                    'python main.py "create proxy on port 8088 with / to backend 3000"'
                ),
            }
        )

    if errors:
        print_failures(errors)
        raise ConfigPreflightError("Config-level preflight checks failed.", errors)

    print("✅ Config-level preflight checks passed")
