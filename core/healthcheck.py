import time
import urllib.error
import urllib.request


def wait_for_backend(port: int, retries=10, delay=1):
    url = f"http://127.0.0.1:{port}/"

    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 200:
                    print(f"✅ Backend ready: {url}")
                    return True
        except Exception:
            time.sleep(delay)

    print(f"❌ Backend not ready: {url}")
    return False


def health_check_route(proxy_port: int, path: str, retries=45, delay=2):
    check_path = path

    if check_path != "/" and not check_path.endswith("/"):
        check_path += "/"

    url = f"http://127.0.0.1:{proxy_port}{check_path}"

    print(f"🩺 Health check: {url}")

    last_error = "Unknown error"

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return {
                    "success": True,
                    "error": None,
                    "status": response.status,
                    "url": url,
                }

        except urllib.error.HTTPError as e:
            if e.code < 500:
                return {
                    "success": True,
                    "error": f"HTTP {e.code}",
                    "status": e.code,
                    "url": url,
                }

            last_error = f"HTTP {e.code}"
            print(f"⏳ HTTP {e.code}, retrying... attempt {attempt}/{retries}")
            time.sleep(delay)

        except Exception as e:
            last_error = str(e)
            print(f"⏳ Waiting for proxy... attempt {attempt}/{retries}")
            time.sleep(delay)

    return {
        "success": False,
        "error": last_error,
        "status": None,
        "url": url,
    }


def health_check_all_routes(config: dict):
    proxy_port = config["port"]

    for route in config["routes"]:
        result = health_check_route(proxy_port, route["path"])

        if not result["success"]:
            return result

    return {
        "success": True,
        "error": None,
        "status": 200,
        "url": None,
    }