"""Exercise a running HTTP server, its worker, and the CLI without Discord credentials."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import uvicorn
from nacl.signing import SigningKey

from mensa.app import create_app
from mensa.config import Settings
from mensa.discord_api import DiscordAPI


@contextmanager
def running_server(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                assert thread.is_alive(), "HTTP server stopped before startup"
                assert time.monotonic() < deadline, "HTTP server did not start"
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "HTTP server did not shut down"


def test_real_http_registration_worker_and_cli_import(tmp_path):
    role_state = set()
    key = SigningKey.generate()
    settings = Settings(
        "123",
        key.verify_key.encode().hex(),
        "fake-discord-token",
        "456",
        "789",
        "z" * 48,
        tmp_path / "members.sqlite3",
        reconcile_seconds=5,
    )
    api = DiscordAPI(settings.bot_token)

    def discord_rest(request):
        assert request.headers["authorization"] == "Bot fake-discord-token"
        assert request.url.path == "/api/v10/guilds/456/members/111/roles/789"
        if request.method == "PUT":
            role_state.add("789")
        elif request.method == "DELETE":
            role_state.discard("789")
        else:
            raise AssertionError(request.method)
        return httpx.Response(204)

    api.client = httpx.AsyncClient(
        base_url="https://discord.com/api/v10/",
        headers={"Authorization": "Bot fake-discord-token"},
        transport=httpx.MockTransport(discord_rest),
    )
    app = create_app(settings, discord=api)
    paid = tmp_path / "paid.csv"
    paid.write_text("member_number\n10001\n")
    with running_server(app) as url, httpx.Client(base_url=url, timeout=5) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        env = {**os.environ, "APP_URL": url, "ADMIN_TOKEN": settings.admin_token}
        command = [sys.executable, "-m", "mensa.cli", "import-paid", str(paid), "--apply"]
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=15, check=False
        )
        assert result.returncode == 0, result.stderr
        assert '"applied": true' in result.stdout
        payload = {
            "id": "999",
            "application_id": "123",
            "guild_id": "456",
            "type": 5,
            "member": {"user": {"id": "111"}},
            "data": {
                "custom_id": "mensa:link",
                "components": [
                    {
                        "type": 1,
                        "components": [{"type": 4, "custom_id": "member_number", "value": "10001"}],
                    }
                ],
            },
        }
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = key.sign(timestamp.encode() + body).signature.hex()
        response = client.post(
            "/interactions",
            content=body,
            headers={
                "content-type": "application/json",
                "x-signature-timestamp": timestamp,
                "x-signature-ed25519": signature,
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["flags"] == 64
        deadline = time.monotonic() + 5
        while "789" not in role_state:
            assert time.monotonic() < deadline, "Background worker did not grant paid access"
            time.sleep(0.01)
        paid.write_text("member_number\n10002\n")
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=15, check=False
        )
        assert result.returncode == 0, result.stderr
        assert '"revoke_count": 1' in result.stdout
        assert "789" not in role_state
