import re


def normalize_upstream(upstream: str) -> str:
    upstream = upstream.strip()
    upstream = upstream.replace("localhost", "127.0.0.1")
    upstream = upstream.replace("http://", "")
    upstream = upstream.replace("https://", "")
    return upstream.rstrip("/")


def get_upstream_port(upstream: str) -> int:
    return int(upstream.split(":")[1])


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object.")

    port = config.get("port")

    if not isinstance(port, int):
        raise ValueError("The server port must be a number, like 8080 or 9000.")

    if port < 1024 or port > 65535:
        raise ValueError("Use a port between 1024 and 65535.")

    routes = config.get("routes")

    if not isinstance(routes, list) or len(routes) == 0:
        raise ValueError(
            "I could not find where traffic should go. "
            "Example: create server on port 9000 and send traffic to backend 4000"
        )

    cleaned_routes = []
    seen_paths = set()

    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("Each route must be an object with path and upstream.")

        path = route.get("path")
        upstream = route.get("upstream")

        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Each route needs a path, like / or /api.")

        if not re.match(r"^/[a-zA-Z0-9/_-]*$", path):
            raise ValueError(
                f"Invalid route path '{path}'. "
                "Paths can only contain letters, numbers, /, _, and -."
            )

        if not isinstance(upstream, str):
            raise ValueError("Each route needs a backend, like localhost:3000.")

        path = path.rstrip("/") if path != "/" else "/"

        if path in seen_paths:
            raise ValueError(f"Duplicate route path '{path}' is not allowed.")

        seen_paths.add(path)

        upstream = normalize_upstream(upstream)

        if not re.match(r"^(127\.0\.0\.1|0\.0\.0\.0):[0-9]{2,5}$", upstream):
            raise ValueError(
                f"Unsafe backend '{upstream}'. "
                "For now, only local backends are allowed."
            )

        upstream_port = get_upstream_port(upstream)

        if upstream_port < 1024 or upstream_port > 65535:
            raise ValueError(
                f"Backend port {upstream_port} must be between 1024 and 65535."
            )

        cleaned_routes.append(
            {
                "path": path,
                "upstream": upstream,
            }
        )

    cleaned_routes.sort(key=lambda r: len(r["path"]), reverse=True)

    return {
        "port": port,
        "routes": cleaned_routes,
    }