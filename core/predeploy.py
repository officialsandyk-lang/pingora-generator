import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from agents.debug_agent import fix_rust_code


BLOCKED_LOG_PATTERNS = [
    "panic",
    "thread 'main' panicked",
    "address already in use",
    "connection refused",
    "permission denied",
    "failed to start",
    "error:",
    "fatal",
]


def run_command(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def compose_down(project_dir):
    run_command(
        ["docker", "compose", "down", "--remove-orphans"],
        cwd=project_dir,
    )


def is_rust_build_error(output: str) -> bool:
    """
    Detect Rust compile errors that happen inside Docker Compose build.

    These can safely be sent to Debug Agent because Debug Agent knows
    how to fix generated Rust code.
    """

    rust_error_signals = [
        "error[E",
        "could not compile",
        "src/main.rs",
        "generated-pingora-proxy",
        "rustc --explain",
    ]

    return any(signal in output for signal in rust_error_signals)


def apply_debug_agent_fix(project_dir, error_output: str):
    """
    Agent 2C — Docker Build Debug Fix

    Docker Compose build runs cargo build --release inside Docker.
    If that Rust build fails, send the generated Rust code and build
    error to the Debug Agent, then retry the Docker build.
    """

    print("🧠 Debug Agent is fixing Docker/Rust build error...")

    project_dir = Path(project_dir)

    main_rs_path = project_dir / "src" / "main.rs"
    cargo_toml_path = project_dir / "Cargo.toml"

    if not main_rs_path.exists():
        raise FileNotFoundError(f"Could not find {main_rs_path}")

    if not cargo_toml_path.exists():
        raise FileNotFoundError(f"Could not find {cargo_toml_path}")

    fixed_code = fix_rust_code(
        rust_code=main_rs_path.read_text(),
        cargo_toml=cargo_toml_path.read_text(),
        error_output=error_output,
    )

    main_rs_path.write_text(fixed_code)

    print("✅ Debug Agent applied Docker/Rust build fix")


def compose_build(project_dir, max_attempts=2):
    print("🐳 Pre-deploy: building Docker Compose stack...")

    last_output = ""

    for attempt in range(1, max_attempts + 1):
        print(f"🔍 Docker Compose build attempt {attempt}/{max_attempts}")

        result = run_command(
            ["docker", "compose", "--progress=plain", "build", "--no-cache"],
            cwd=project_dir,
        )

        if result.returncode == 0:
            print("✅ Pre-deploy build passed")
            return True

        build_output = ""
        build_output += "\nSTDOUT:\n"
        build_output += result.stdout or ""
        build_output += "\nSTDERR:\n"
        build_output += result.stderr or ""

        last_output = build_output

        print("❌ Docker Compose build failed")

        if attempt < max_attempts and is_rust_build_error(build_output):
            apply_debug_agent_fix(project_dir, build_output)
            print("🔁 Retrying Docker Compose build after Debug Agent fix...")
            continue

        break

    raise RuntimeError(
        "Pre-deploy Docker Compose build failed:\n"
        + last_output
    )


def compose_up(project_dir):
    print("🐳 Pre-deploy: starting Docker Compose sandbox...")

    result = run_command(
        ["docker", "compose", "up", "-d", "--remove-orphans"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        startup_output = ""
        startup_output += "\nSTDOUT:\n"
        startup_output += result.stdout or ""
        startup_output += "\nSTDERR:\n"
        startup_output += result.stderr or ""

        raise RuntimeError(
            "Pre-deploy Docker Compose startup failed:\n"
            + startup_output
        )

    print("✅ Pre-deploy sandbox started")


def compose_ps(project_dir):
    result = run_command(
        ["docker", "compose", "ps"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        output = ""
        output += "\nSTDOUT:\n"
        output += result.stdout or ""
        output += "\nSTDERR:\n"
        output += result.stderr or ""

        raise RuntimeError(
            "Could not inspect Docker Compose services:\n"
            + output
        )

    print("")
    print("📦 Compose services:")
    print(result.stdout)


def compose_logs(project_dir):
    result = run_command(
        ["docker", "compose", "logs", "--no-color"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        return result.stderr or result.stdout or ""

    return result.stdout or ""


def route_url(proxy_port: int, path: str) -> str:
    check_path = path

    if check_path != "/" and not check_path.endswith("/"):
        check_path += "/"

    return f"http://127.0.0.1:{proxy_port}{check_path}"


def verify_route(url: str, retries=45, delay=2, require_200=True):
    print(f"🩺 Pre-deploy route check: {url}")

    last_error = "Unknown error"

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if require_200:
                    if response.status == 200:
                        print(f"✅ Route passed: {url}")
                        return True

                    last_error = f"HTTP {response.status}"
                else:
                    if response.status < 500:
                        print(f"✅ Route reachable: {url} returned HTTP {response.status}")
                        return True

        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}"

            if not require_200 and e.code < 500:
                print(f"✅ Route reachable: {url} returned HTTP {e.code}")
                return True

        except Exception as e:
            last_error = str(e)

        print(f"⏳ Waiting for route... {attempt}/{retries}")
        time.sleep(delay)

    raise RuntimeError(
        f"Pre-deploy route check failed: {url}\n"
        f"Last error: {last_error}"
    )


def verify_all_routes(config: dict, require_200=True):
    proxy_port = config["port"]

    for route in config["routes"]:
        url = route_url(proxy_port, route["path"])
        verify_route(url, require_200=require_200)


def verify_logs_clean(project_dir):
    print("🔍 Pre-deploy: scanning container logs...")

    logs = compose_logs(project_dir)
    lower_logs = logs.lower()

    found = []

    for pattern in BLOCKED_LOG_PATTERNS:
        if pattern in lower_logs:
            found.append(pattern)

    if found:
        raise RuntimeError(
            "Pre-deploy log scan failed.\n"
            f"Found suspicious log patterns: {found}\n\n"
            f"Logs:\n{logs}"
        )

    print("✅ Log scan passed")


def predeploy_verify(project_dir, config, keep_running=True, require_200=True):
    """
    Agent 4D — Pre-Deploy Sandbox Verification

    Builds and runs the generated infrastructure in Docker Compose,
    validates every route, checks logs, and only passes if the stack
    looks safe to deploy.

    If Docker Compose build fails because Rust fails inside Docker,
    the error is sent to Debug Agent and the build is retried.
    """

    print("")
    print("🚦 Running pre-deploy sandbox verification...")

    try:
        compose_down(project_dir)
        compose_build(project_dir)
        compose_up(project_dir)

        compose_ps(project_dir)

        verify_all_routes(config, require_200=require_200)
        verify_logs_clean(project_dir)

        print("")
        print("✅ Pre-deploy sandbox verification passed")
        print(f"Live sandbox URL: http://127.0.0.1:{config['port']}")

        if not keep_running:
            print("🧹 Stopping pre-deploy sandbox...")
            compose_down(project_dir)

        return True

    except Exception:
        print("")
        print("❌ Pre-deploy sandbox verification failed")

        logs = compose_logs(project_dir)
        print("")
        print("Container logs:")
        print(logs)

        if not keep_running:
            compose_down(project_dir)

        raise