import urllib.error
import urllib.request


def request_url(url: str, method: str = "GET", data: bytes | None = None, headers: dict | None = None, timeout: int = 5):
    request = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers=headers or {},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "status": response.status,
                "body": response.read().decode("utf-8", errors="ignore"),
                "error": None,
            }

    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "body": e.read().decode("utf-8", errors="ignore"),
            "error": None,
        }

    except Exception as e:
        return {
            "status": None,
            "body": "",
            "error": str(e),
        }


def route_url(proxy_port: int, path: str) -> str:
    check_path = path

    if check_path != "/" and not check_path.endswith("/"):
        check_path += "/"

    return f"http://127.0.0.1:{proxy_port}{check_path}"


def expect_status(name: str, result: dict, expected_status: int) -> dict:
    passed = result["status"] == expected_status

    return {
        "name": name,
        "passed": passed,
        "expected": expected_status,
        "actual": result["status"],
        "error": result.get("error"),
    }


def run_valid_route_tests(config: dict, require_200: bool = True) -> list[dict]:
    proxy_port = config["port"]
    tests = []

    for route in config["routes"]:
        path = route["path"]
        url = route_url(proxy_port, path)

        result = request_url(url)

        if require_200:
            passed = result["status"] == 200
            expected = 200
        else:
            passed = result["status"] is not None and result["status"] < 500
            expected = "<500"

        tests.append(
            {
                "name": f"Valid route {path}",
                "passed": passed,
                "expected": expected,
                "actual": result["status"],
                "url": url,
                "error": result.get("error"),
            }
        )

    return tests


def run_blocked_path_tests(config: dict) -> list[dict]:
    proxy_port = config["port"]
    security = config.get("security", {})
    blocked_paths = security.get("blocked_paths", [])

    tests = []

    for path in blocked_paths:
        url = route_url(proxy_port, path)
        result = request_url(url)

        tests.append(
            expect_status(
                name=f"Blocked path {path}",
                result=result,
                expected_status=403,
            )
            | {"url": url}
        )

    return tests


def run_method_tests(config: dict) -> list[dict]:
    proxy_port = config["port"]
    first_route = config["routes"][0]["path"]
    url = route_url(proxy_port, first_route)

    tests = []

    result = request_url(url, method="TRACE")

    tests.append(
        expect_status(
            name="Unsupported method TRACE",
            result=result,
            expected_status=405,
        )
        | {"url": url}
    )

    return tests


def run_large_body_tests(config: dict) -> list[dict]:
    proxy_port = config["port"]
    first_route = config["routes"][0]["path"]
    url = route_url(proxy_port, first_route)

    security = config.get("security", {})
    max_body = int(security.get("max_request_body_bytes", 1_048_576))

    oversized_length = max_body + 1

    headers = {
        "Content-Length": str(oversized_length),
        "Content-Type": "application/octet-stream",
    }

    # Keep actual body small. The generated proxy rejects based on Content-Length.
    data = b"x"

    result = request_url(
        url,
        method="POST",
        data=data,
        headers=headers,
    )

    return [
        expect_status(
            name="Large request body rejected",
            result=result,
            expected_status=413,
        )
        | {"url": url}
    ]


def summarize_tests(tests: list[dict]) -> dict:
    passed_count = sum(1 for test in tests if test.get("passed"))
    failed = [test for test in tests if not test.get("passed")]

    return {
        "passed": len(failed) == 0,
        "summary": f"{passed_count}/{len(tests)} protection tests passed",
        "details": {
            "total": len(tests),
            "passed": passed_count,
            "failed": len(failed),
            "tests": tests,
        },
        "error": None if not failed else "Some protection tests failed",
    }


def run_protection_tests(config: dict, require_200: bool = True) -> dict:
    """
    Agent 5A — Security Protection Tests

    Verifies that generated Pingora security rules actually work.

    Tests:
    - valid routes still work
    - blocked paths return 403
    - unsupported HTTP methods return 405
    - oversized request body returns 413
    """

    tests = []

    tests.extend(run_valid_route_tests(config, require_200=require_200))
    tests.extend(run_blocked_path_tests(config))
    tests.extend(run_method_tests(config))
    tests.extend(run_large_body_tests(config))

    return summarize_tests(tests)