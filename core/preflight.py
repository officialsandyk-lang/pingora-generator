import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


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


def print_failures(errors):
    print("")
    print("❌ Preflight failed")

    for error in errors:
        print("")
        print(f"- {error['message']}")
        print("")
        print(error["fix"])

    print("")


def preflight_check(use_docker: bool = True, use_compose: bool = True):
    """
    Agent 0 — Preflight Doctor

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
        raise RuntimeError("Preflight checks failed.")

    print("✅ Preflight checks passed")


def preflight_check_config(config: dict, use_docker: bool = True, use_compose: bool = True):
    """
    Runs after validation/security, when we know the requested proxy/backend ports.
    """

    print("🧪 Running config-level preflight checks...")

    errors = []

    proxy_port = config["port"]

    # In Compose/predeploy mode, old compose stacks are usually removed automatically.
    # Still warn clearly if something else is occupying the public proxy port.
    port_result = check_port_available(proxy_port)

    if not port_result["ok"]:
        errors.append(port_result)

    if errors:
        print_failures(errors)
        raise RuntimeError("Config-level preflight checks failed.")

    print("✅ Config-level preflight checks passed")