from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from agents.debug_agent import fix_rust_code

try:
    from agents.runtime_agent import fix_runtime_config
except ImportError:
    from agents.runtime_agent import fix_runtime_config_with_ai as fix_runtime_config

from core.healthcheck import wait_for_backend, health_check_all_routes
from core.logger import log_run
from core.project_writer import write_project
from core.validator import get_upstream_port, validate_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


def call_project_writer(config: dict[str, Any], project_dir: Path = PROJECT_DIR) -> None:
    """
    Supports both possible project_writer signatures:

      write_project(config)
      write_project(config, project_dir=...)
      write_project(project_dir, config)
    """
    try:
        write_project(config, project_dir=project_dir)
        return
    except TypeError:
        pass

    try:
        write_project(project_dir, config)
        return
    except TypeError:
        pass

    write_project(config)


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def cargo_check(
    prompt: str,
    config: dict[str, Any],
    max_attempts: int = 3,
    project_dir: Path = PROJECT_DIR,
) -> bool:
    """
    Run cargo check against generated-pingora-proxy.

    Agent ownership:
      - cargo/Rust compile errors → Debug Agent
      - runtime crashes/health failures → Runtime Agent, handled elsewhere
    """
    project_dir = Path(project_dir).resolve()

    cargo_toml_path = project_dir / "Cargo.toml"
    main_rs_path = project_dir / "src" / "main.rs"

    if not cargo_toml_path.exists():
        print(f"❌ Cargo.toml not found: {cargo_toml_path}")
        return False

    if not main_rs_path.exists():
        print(f"❌ src/main.rs not found: {main_rs_path}")
        return False

    for attempt in range(1, max_attempts + 1):
        print(f"🔍 Running cargo check... attempt {attempt}/{max_attempts}")

        result = subprocess.run(
            ["cargo", "check"],
            cwd=str(project_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if result.returncode == 0:
            print("✅ cargo check passed")
            return True

        error_output = "\n".join(
            part for part in [result.stdout, result.stderr] if part
        )

        print("❌ cargo check failed")
        print(error_output)

        if attempt == max_attempts:
            log_run(
                prompt=prompt,
                config=config,
                success=False,
                error=error_output,
            )
            return False

        print("🧠 Debug Agent is fixing Rust code...")

        try:
            fixed_code = fix_rust_code(
                rust_code=read_file(main_rs_path),
                cargo_toml=read_file(cargo_toml_path),
                error_output=error_output,
            )

            if not fixed_code or not isinstance(fixed_code, str):
                raise RuntimeError("Debug Agent returned empty Rust code.")

            write_file(main_rs_path, fixed_code)

            print("✅ Debug Agent applied Rust fix")

        except Exception as exc:
            print("❌ Debug Agent failed while fixing Rust code")
            print(exc)

            log_run(
                prompt=prompt,
                config=config,
                success=False,
                error=f"Debug Agent failed: {exc}\n\nOriginal cargo error:\n{error_output}",
            )

            return False

    return False


def start_backend(port: int, project_dir: Path = PROJECT_DIR) -> subprocess.Popen:
    print(f"🚀 Starting backend server on port {port}...")

    return subprocess.Popen(
        ["python", "-m", "http.server", str(port)],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return

    try:
        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        pass


def stop_processes(
    proxy: subprocess.Popen | None,
    backend_processes: list[subprocess.Popen],
) -> None:
    stop_process(proxy)

    for backend in backend_processes:
        stop_process(backend)


def collect_proxy_stderr(proxy: subprocess.Popen | None) -> str:
    if proxy is None:
        return ""

    if proxy.stderr is None:
        return ""

    try:
        if proxy.poll() is not None:
            return proxy.stderr.read() or ""
    except Exception:
        return "Proxy process exited unexpectedly."

    return ""


def run_servers(
    prompt: str,
    config: dict[str, Any],
    runtime_attempt: int = 1,
    max_runtime_attempts: int = 3,
    project_dir: Path = PROJECT_DIR,
) -> None:
    """
    Legacy local runtime path.

    This is no longer the live production path after blue/green.
    It remains useful for local/non-Docker testing.

    Agent ownership:
      - Runtime health failures → Runtime Agent
      - Rust compile failures during retry → Debug Agent through cargo_check()
    """
    project_dir = Path(project_dir).resolve()

    config = validate_config(config)

    proxy_port = int(config["port"])
    routes = config["routes"]

    backend_ports = sorted(
        set(get_upstream_port(route["upstream"]) for route in routes)
    )

    backend_processes: list[subprocess.Popen] = []
    proxy: subprocess.Popen | None = None

    try:
        for backend_port in backend_ports:
            backend = start_backend(backend_port, project_dir=project_dir)
            backend_processes.append(backend)

        for backend_port in backend_ports:
            if not wait_for_backend(backend_port):
                raise RuntimeError(f"Backend on port {backend_port} did not start.")

        print(f"🚀 Starting Pingora proxy on port {proxy_port}...")

        proxy = subprocess.Popen(
            ["cargo", "run"],
            cwd=str(project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        print("⏳ Waiting for Pingora to finish compiling and start...")
        time.sleep(15)

        health_result = health_check_all_routes(config)
        health_ok = bool(health_result.get("success"))

        proxy_stderr = collect_proxy_stderr(proxy)

        if proxy.poll() is not None:
            health_result = {
                "success": False,
                "error": f"Proxy crashed before health check.\n{proxy_stderr}",
                "status": None,
                "url": None,
            }

            health_ok = False

        if not health_ok and runtime_attempt < max_runtime_attempts:
            print("🧠 Runtime Agent is fixing runtime issue...")

            main_rs_path = project_dir / "src" / "main.rs"

            runtime_error_details = f"""
Health check failed

URL:
{health_result.get("url")}

HTTP Status:
{health_result.get("status")}

Error:
{health_result.get("error")}

Proxy stderr:
{proxy_stderr}
"""

            try:
                fixed_config = fix_runtime_config(
                    config=config,
                    health_error=runtime_error_details,
                    main_rs=read_file(main_rs_path),
                )
            except TypeError:
                fixed_config = fix_runtime_config(
                    config,
                    runtime_error_details,
                    read_file(main_rs_path),
                )

            print("✅ Runtime Agent proposed fixed config:")
            print(json.dumps(fixed_config, indent=2))

            fixed_config = validate_config(fixed_config)

            stop_processes(proxy, backend_processes)
            proxy = None
            backend_processes = []

            print("♻️ Regenerating project with fixed config...")
            call_project_writer(fixed_config, project_dir=project_dir)

            if cargo_check(prompt, fixed_config, project_dir=project_dir):
                print("🔁 Retrying server startup...")
                return run_servers(
                    prompt,
                    fixed_config,
                    runtime_attempt=runtime_attempt + 1,
                    max_runtime_attempts=max_runtime_attempts,
                    project_dir=project_dir,
                )

            print("❌ Cargo check failed after Runtime Agent config repair.")
            return

        log_run(
            prompt=prompt,
            config=config,
            success=health_ok,
            error=None if health_ok else health_result.get("error"),
        )

        print("")
        print("✅ Servers running" if health_ok else "⚠️ Servers started, but health check failed")

        print("")
        print("Backends:")
        for backend_port in backend_ports:
            print(f"- http://127.0.0.1:{backend_port}")

        print("")
        print(f"Proxy: http://127.0.0.1:{proxy_port}")

        print("")
        print("Routes:")
        for route in routes:
            route_path = route["path"]
            display_path = route_path if route_path == "/" else f"{route_path}/"
            print(f"- http://127.0.0.1:{proxy_port}{display_path} -> {route['upstream']}")

        print("")
        print("Press Ctrl+C to stop all servers.")

        if proxy is not None:
            proxy.wait()

    except KeyboardInterrupt:
        print("\n🛑 Stopping servers...")

    finally:
        stop_processes(proxy, backend_processes)
        print("✅ Servers stopped")