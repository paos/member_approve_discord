"""Interactive development launcher. Run Start.command from Finder."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx


def wait_for_json(url, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Process exited with code {process.returncode}; see data/*.log.")
        try:
            response = httpx.get(url, timeout=1)
            if response.is_success:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}; see data/*.log.")


def main():
    children = []
    streams = []
    data = Path("data")
    data.mkdir(exist_ok=True, mode=0o700)
    ngrok = shutil.which("ngrok") or "/opt/homebrew/bin/ngrok"
    if not Path(ngrok).is_file():
        raise RuntimeError(
            "Install ngrok from https://ngrok.com/download before starting the HTTPS test endpoint."
        )
    try:
        # Refuse to share ports with another app or tunnel.
        import socket

        for port in (8766, 4040):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        subprocess.run([sys.executable, "-m", "mensa.cli", "bootstrap"], check=True)
        for name, command in [
            (
                "app",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "mensa.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8766",
                    "--no-access-log",
                ],
            ),
            ("tunnel", [ngrok, "http", "8766", "--log", "stdout", "--log-format", "json"]),
        ]:
            fd = os.open(data / f"{name}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            stream = os.fdopen(fd, "a")
            streams.append(stream)
            children.append(subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT))
        wait_for_json("http://127.0.0.1:8766/healthz", children[0])
        tunnels = wait_for_json("http://127.0.0.1:4040/api/tunnels", children[1])
        deadline = time.monotonic() + 30
        public_url = None
        while time.monotonic() < deadline:
            public_url = next(
                (
                    t["public_url"]
                    for t in tunnels.get("tunnels", [])
                    if t["public_url"].startswith("https://")
                ),
                None,
            )
            if public_url:
                break
            time.sleep(0.5)
            tunnels = wait_for_json("http://127.0.0.1:4040/api/tunnels", children[1])
        if not public_url:
            raise RuntimeError("ngrok did not provide an HTTPS URL. Check data/tunnel.log.")
        subprocess.run([sys.executable, "-m", "mensa.cli", "status"], check=True)
        # Only seed the synthetic snapshot on the first launch, never overwrite later imports.
        from dotenv import dotenv_values

        config = dotenv_values(".env")
        status = httpx.get(
            "http://127.0.0.1:8766/admin/status",
            headers={"Authorization": "Bearer " + config["ADMIN_TOKEN"]},
        ).json()
        if status["paid_imported_at"] is None:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mensa.cli",
                    "import-paid",
                    "examples/paid-members.csv",
                    "--apply",
                ],
                check=True,
            )
        endpoint = public_url + "/interactions"
        subprocess.run([sys.executable, "-m", "mensa.cli", "connect", endpoint], check=True)
        (data / "running.json").write_text(
            json.dumps({"interactions_endpoint": endpoint}, indent=2)
        )
        print("\nPython app and HTTPS tunnel are running.", flush=True)
        print("Discord Interactions Endpoint URL: " + endpoint, flush=True)
        print("Discord is connected. Open #membership-registration to test.", flush=True)
        print("Keep this window open while testing.", flush=True)
        print("Press Ctrl+C to stop both processes.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError("A process stopped. See data/app.log and data/tunnel.log.")
    finally:
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (RuntimeError, OSError, subprocess.CalledProcessError, httpx.HTTPError) as exc:
        sys.exit(str(exc))
