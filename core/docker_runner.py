import subprocess
import time
import urllib.error
import urllib.request


IMAGE_NAME = "generated-pingora-proxy"


def run_command(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def docker_available() -> bool:
    result = run_command(["docker", "ps"])
    return result.returncode == 0


def docker_build(project_dir, image_name=IMAGE_NAME):
    print("🐳 Building Docker image...")

    result = run_command(
        ["docker", "build", "-t", image_name, "."],
        cwd=project_dir,
    )

    if result.returncode != 0:
        print("❌ Docker build failed")
        print(result.stderr)
        return False

    print("✅ Docker image built")
    return True


def docker_run(config, image_name=IMAGE_NAME):
    proxy_port = config["port"]
    container_name = f"{image_name}-{proxy_port}"

    print(f"🧹 Removing old Docker container if it exists: {container_name}")

    run_command(
        ["docker", "rm", "-f", container_name],
    )

    print(f"🐳 Starting Docker container on port {proxy_port}...")

    result = run_command(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "-p",
            f"{proxy_port}:{proxy_port}",
            image_name,
        ]
    )

    if result.returncode != 0:
        print("❌ Docker run failed")
        print(result.stderr)
        return None

    container_id = result.stdout.strip()

    print(f"✅ Docker container started: {container_name}")
    return {
        "container_id": container_id,
        "container_name": container_name,
        "proxy_port": proxy_port,
    }


def docker_logs(container_name):
    result = run_command(["docker", "logs", container_name])

    if result.returncode != 0:
        return result.stderr

    return result.stdout


def docker_health_check(config, retries=45, delay=2):
    proxy_port = config["port"]
    routes = config["routes"]

    for route in routes:
        path = route["path"]

        if path != "/" and not path.endswith("/"):
            path += "/"

        url = f"http://127.0.0.1:{proxy_port}{path}"

        print(f"🩺 Docker health check: {url}")

        last_error = "Unknown error"

        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status < 500:
                        print(f"✅ Docker health check passed: {url}")
                        break

            except urllib.error.HTTPError as e:
                if e.code < 500:
                    print(f"✅ Docker proxy reachable: {url} returned HTTP {e.code}")
                    break

                last_error = f"HTTP {e.code}"
                print(f"⏳ Docker proxy returned {e.code}, retrying... {attempt}/{retries}")
                time.sleep(delay)

            except Exception as e:
                last_error = str(e)
                print(f"⏳ Waiting for Docker proxy... {attempt}/{retries}")
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


def build_and_run_docker(project_dir, config):
    if not docker_available():
        raise RuntimeError(
            "Docker is not available. Start Docker Desktop and enable WSL integration."
        )

    if not docker_build(project_dir):
        raise RuntimeError("Docker build failed.")

    container = docker_run(config)

    if container is None:
        raise RuntimeError("Docker container failed to start.")

    health = docker_health_check(config)

    if not health["success"]:
        logs = docker_logs(container["container_name"])
        raise RuntimeError(
            f"Docker health check failed: {health['url']}\n"
            f"Error: {health['error']}\n\n"
            f"Container logs:\n{logs}"
        )

    print("")
    print("✅ Docker sandbox running")
    print(f"Container: {container['container_name']}")
    print(f"Live URL:  http://127.0.0.1:{config['port']}")
    print("")
    print("Stop container with:")
    print(f"docker stop {container['container_name']}")

    return container