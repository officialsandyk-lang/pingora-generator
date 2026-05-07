from pathlib import Path

PROJECT_DIR = Path("generated-pingora-proxy")


def get_backend_ports(config: dict) -> list[int]:
    ports = []

    for route in config["routes"]:
        upstream = route["upstream"]
        port = int(upstream.split(":")[1])
        ports.append(port)

    return sorted(set(ports))


def write_docker_files(config: dict):
    proxy_port = config["port"]
    backend_ports = get_backend_ports(config)

    dockerfile = """FROM rust:1-bookworm

RUN apt-get update && apt-get install -y \\
    python3 \\
    build-essential \\
    cmake \\
    pkg-config \\
    libssl-dev \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN cargo clean
RUN cargo build --release

RUN chmod +x run.sh

CMD ["./run.sh"]
"""

    backend_start_commands = []
    backend_wait_commands = []

    for port in backend_ports:
        backend_start_commands.append(
            f"""echo "🚀 Starting backend on port {port}..."
python3 -m http.server {port} --bind 127.0.0.1 > backend-{port}.log 2>&1 &
"""
        )

        backend_wait_commands.append(
            f"""echo "🩺 Waiting for backend {port}..."
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:{port}/ > /dev/null; then
        echo "✅ Backend {port} ready"
        break
    fi

    echo "⏳ Backend {port} not ready yet: $i/30"
    sleep 1

    if [ "$i" = "30" ]; then
        echo "❌ Backend {port} failed to start"
        echo "Backend {port} logs:"
        cat backend-{port}.log || true
        exit 1
    fi
done
"""
        )

    backend_start_block = "\n".join(backend_start_commands)
    backend_wait_block = "\n".join(backend_wait_commands)

    run_sh = f"""#!/usr/bin/env bash
set -e

echo "🚀 Starting backend services..."
{backend_start_block}

echo "⏳ Verifying backend services..."
{backend_wait_block}

echo "🚀 Starting Pingora proxy on port {proxy_port}..."
./target/release/generated-pingora-proxy
"""

    dockerignore = """.git
__pycache__
*.pyc
target
Cargo.lock
"""

    (PROJECT_DIR / "Dockerfile").write_text(dockerfile)
    (PROJECT_DIR / "run.sh").write_text(run_sh)
    (PROJECT_DIR / ".dockerignore").write_text(dockerignore)

    print("✅ Docker sandbox files generated")