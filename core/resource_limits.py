from pathlib import Path

DEFAULT_PROXY_MEMORY_LIMIT = "512m"
DEFAULT_BACKEND_MEMORY_LIMIT = "256m"
DEFAULT_PROXY_CPUS = "1.0"
DEFAULT_BACKEND_CPUS = "0.5"


def read_compose_file(project_dir) -> str:
    project_dir = Path(project_dir)
    compose_file = project_dir / "docker-compose.yml"

    if not compose_file.exists():
        raise FileNotFoundError(f"docker-compose.yml not found at {compose_file}")

    return compose_file.read_text()


def service_name_for_backend(port: int) -> str:
    return f"backend-{port}"


def get_backend_ports(config: dict) -> list[int]:
    ports = []

    for route in config["routes"]:
        upstream = route["upstream"]
        port = int(upstream.split(":")[1])
        ports.append(port)

    return sorted(set(ports))


def verify_text_contains(compose_text: str, value: str) -> bool:
    return value in compose_text


def verify_resource_limits(project_dir, config: dict) -> dict:
    """
    Agent 5C — Resource Limit Verification

    Verifies Docker Compose includes basic resource and restart safeguards.

    Expected:
    - proxy has memory + CPU limits
    - every backend has memory + CPU limits
    - every service has restart policy
    """

    compose_text = read_compose_file(project_dir)

    proxy_port = config["port"]
    backend_ports = get_backend_ports(config)

    tests = []

    proxy_service_checks = [
        {
            "name": "Proxy service exists",
            "passed": verify_text_contains(compose_text, "proxy:"),
            "expected": "proxy:",
        },
        {
            "name": "Proxy has memory limit",
            "passed": verify_text_contains(compose_text, f"mem_limit: {DEFAULT_PROXY_MEMORY_LIMIT}"),
            "expected": f"mem_limit: {DEFAULT_PROXY_MEMORY_LIMIT}",
        },
        {
            "name": "Proxy has CPU limit",
            "passed": verify_text_contains(compose_text, f'cpus: "{DEFAULT_PROXY_CPUS}"'),
            "expected": f'cpus: "{DEFAULT_PROXY_CPUS}"',
        },
        {
            "name": "Proxy has restart policy",
            "passed": verify_text_contains(compose_text, "restart: unless-stopped"),
            "expected": "restart: unless-stopped",
        },
        {
            "name": "Proxy exposes configured port",
            "passed": verify_text_contains(compose_text, f'"{proxy_port}:{proxy_port}"'),
            "expected": f'"{proxy_port}:{proxy_port}"',
        },
    ]

    tests.extend(proxy_service_checks)

    for port in backend_ports:
        service_name = service_name_for_backend(port)

        tests.extend(
            [
                {
                    "name": f"{service_name} service exists",
                    "passed": verify_text_contains(compose_text, f"{service_name}:"),
                    "expected": f"{service_name}:",
                },
                {
                    "name": f"{service_name} has memory limit",
                    "passed": verify_text_contains(compose_text, f"mem_limit: {DEFAULT_BACKEND_MEMORY_LIMIT}"),
                    "expected": f"mem_limit: {DEFAULT_BACKEND_MEMORY_LIMIT}",
                },
                {
                    "name": f"{service_name} has CPU limit",
                    "passed": verify_text_contains(compose_text, f'cpus: "{DEFAULT_BACKEND_CPUS}"'),
                    "expected": f'cpus: "{DEFAULT_BACKEND_CPUS}"',
                },
                {
                    "name": f"{service_name} has restart policy",
                    "passed": verify_text_contains(compose_text, "restart: unless-stopped"),
                    "expected": "restart: unless-stopped",
                },
            ]
        )

    passed_count = sum(1 for test in tests if test["passed"])
    failed = [test for test in tests if not test["passed"]]

    return {
        "passed": len(failed) == 0,
        "summary": f"{passed_count}/{len(tests)} resource limit checks passed",
        "details": {
            "total": len(tests),
            "passed": passed_count,
            "failed": len(failed),
            "tests": tests,
        },
        "error": None if not failed else "Some resource limits are missing",
    }