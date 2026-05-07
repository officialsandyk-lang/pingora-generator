import subprocess
import time
import urllib.error
import urllib.request


def run_command(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def compose_available() -> bool:
    result = run_command(["docker", "compose", "version"])

    if result.returncode != 0:
        print("❌ Docker Compose check failed")
        print(result.stderr)
        return False

    return True


def compose_build(project_dir):
    print("🐳 Building Docker Compose services...")

    result = run_command(
        ["docker", "compose", "build"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        print("❌ Docker Compose build failed")
        print(result.stderr)
        return False

    print("✅ Docker Compose build passed")
    return True


def compose_up(project_dir):
    print("🐳 Starting Docker Compose stack...")

    result = run_command(
        ["docker", "compose", "up", "-d", "--remove-orphans"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        print("❌ Docker Compose up failed")
        print(result.stderr)
        return False

    print("✅ Docker Compose stack started")
    return True


def compose_logs(project_dir):
    result = run_command(
        ["docker", "compose", "logs", "--no-color"],
        cwd=project_dir,
    )

    if result.returncode != 0:
        return result.stderr

    return result.stdout


def compose_health_check(config: dict, retries=45, delay=2):
    proxy_port = config["port"]

    for route in config["routes"]:
        path = route["path"]

        if path != "/" and not path.endswith("/"):
            path += "/"

        url = f"http://127.0.0.1:{proxy_port}{path}"

        print(f"🩺 Docker Compose health check: {url}")

        last_error = "Unknown error"

        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status < 500:
                        print(f"✅ Docker Compose health check passed: {url}")
                        break

            except urllib.error.HTTPError as e:
                if e.code < 500:
                    print(f"✅ Docker Compose proxy reachable: {url} returned HTTP {e.code}")
                    break

                last_error = f"HTTP {e.code}"
                print(f"⏳ Docker Compose proxy returned {e.code}, retrying... {attempt}/{retries}")
                time.sleep(delay)

            except Exception as e:
                last_error = str(e)
                print(f"⏳ Waiting for Docker Compose proxy... {attempt}/{retries}")
                time.sleep(delay)

        else:
            return {
                "success": False,
                "url": url,
                "error": last_error,
            }

    return {
        "success": True,
        "url": f"http://127.0.0.1:{proxy_port}",
        "error": None,
    }


def build_and_run_compose(project_dir, config):
    if not compose_available():
        raise RuntimeError("Docker Compose is not available.")

    if not compose_build(project_dir):
        raise RuntimeError("Docker Compose build failed.")

    if not compose_up(project_dir):
        raise RuntimeError("Docker Compose failed to start.")

    health = compose_health_check(config)

    if not health["success"]:
        logs = compose_logs(project_dir)

        raise RuntimeError(
            f"Docker Compose health check failed: {health['url']}\n"
            f"Error: {health['error']}\n\n"
            f"Compose logs:\n{logs}"
        )

    print("")
    print("✅ Docker Compose stack running")
    print(f"Live URL: http://127.0.0.1:{config['port']}")
    print("")
    print("Stop stack with:")
    print("cd generated-pingora-proxy && docker compose down")

    return True