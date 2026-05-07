import time
import urllib.error
import urllib.request


def route_url(proxy_port: int, path: str) -> str:
    check_path = path

    if check_path != "/" and not check_path.endswith("/"):
        check_path += "/"

    return f"http://127.0.0.1:{proxy_port}{check_path}"


def timed_request(url: str, timeout: int = 5) -> dict:
    start = time.perf_counter()

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
            elapsed_ms = int((time.perf_counter() - start) * 1000)

            return {
                "success": response.status < 500,
                "status": response.status,
                "latency_ms": elapsed_ms,
                "bytes": len(body),
                "error": None,
            }

    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return {
            "success": e.code < 500,
            "status": e.code,
            "latency_ms": elapsed_ms,
            "bytes": 0,
            "error": None,
        }

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        return {
            "success": False,
            "status": None,
            "latency_ms": elapsed_ms,
            "bytes": 0,
            "error": str(e),
        }


def check_route_performance(
    proxy_port: int,
    path: str,
    latency_threshold_ms: int = 500,
    samples: int = 3,
) -> dict:
    url = route_url(proxy_port, path)
    results = []

    for _ in range(samples):
        results.append(timed_request(url))

    successful = [result for result in results if result["success"]]
    failed = [result for result in results if not result["success"]]

    latencies = [result["latency_ms"] for result in successful]

    if latencies:
        avg_latency = int(sum(latencies) / len(latencies))
        max_latency = max(latencies)
        min_latency = min(latencies)
    else:
        avg_latency = None
        max_latency = None
        min_latency = None

    passed = (
        len(failed) == 0
        and avg_latency is not None
        and max_latency <= latency_threshold_ms
    )

    return {
        "route": path,
        "url": url,
        "passed": passed,
        "threshold_ms": latency_threshold_ms,
        "avg_latency_ms": avg_latency,
        "max_latency_ms": max_latency,
        "min_latency_ms": min_latency,
        "samples": results,
        "error": None if passed else "Route failed performance threshold",
    }


def summarize_performance(route_results: list[dict]) -> dict:
    passed_count = sum(1 for result in route_results if result["passed"])
    failed = [result for result in route_results if not result["passed"]]

    if route_results:
        avg_values = [
            result["avg_latency_ms"]
            for result in route_results
            if result["avg_latency_ms"] is not None
        ]

        max_values = [
            result["max_latency_ms"]
            for result in route_results
            if result["max_latency_ms"] is not None
        ]

        overall_avg = int(sum(avg_values) / len(avg_values)) if avg_values else None
        overall_max = max(max_values) if max_values else None
    else:
        overall_avg = None
        overall_max = None

    return {
        "passed": len(failed) == 0,
        "summary": f"{passed_count}/{len(route_results)} routes passed performance checks",
        "details": {
            "total_routes": len(route_results),
            "passed_routes": passed_count,
            "failed_routes": len(failed),
            "overall_avg_latency_ms": overall_avg,
            "overall_max_latency_ms": overall_max,
            "routes": route_results,
        },
        "error": None if not failed else "Some routes failed performance checks",
    }


def run_performance_checks(
    config: dict,
    latency_threshold_ms: int = 500,
    samples: int = 3,
) -> dict:
    """
    Agent 5B — Performance Checks

    Verifies:
    - every route responds
    - no route returns 5xx
    - every route stays under latency threshold
    """

    proxy_port = config["port"]
    route_results = []

    for route in config["routes"]:
        route_results.append(
            check_route_performance(
                proxy_port=proxy_port,
                path=route["path"],
                latency_threshold_ms=latency_threshold_ms,
                samples=samples,
            )
        )

    return summarize_performance(route_results)