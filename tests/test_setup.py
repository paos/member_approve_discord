import argparse
import asyncio
import json

import pytest
from dotenv import dotenv_values

from mensa import cli


class DiscordSetupAPI:
    def __init__(self, token):
        self.roles = [
            {
                "id": "700",
                "name": "Bot",
                "position": 2,
                "tags": {"bot_id": "999"},
                "permissions": "0",
            }
        ]
        self.channels = []
        self.messages = {}
        self.commands = []
        self.server_name = "Mensa Membership Test"
        self.endpoint = None

    async def request(self, method, path, **kwargs):
        body = kwargs.get("json")
        if path == "applications/@me":
            if method == "PATCH":
                self.endpoint = body["interactions_endpoint_url"]
            return {"id": "123", "interactions_endpoint_url": self.endpoint}
        if path == "guilds/456":
            return {"name": self.server_name}
        if path == "users/@me":
            return {"id": "999"}
        if path == "guilds/456/roles":
            if method == "POST":
                self.roles.append({**body, "id": "789", "position": 1})
                return self.roles[-1]
            return self.roles
        if path == "guilds/456/channels":
            if method == "POST":
                channel = {**body, "id": str(1000 + len(self.channels))}
                self.channels.append(channel)
                return channel
            return self.channels
        if path == "applications/123/guilds/456/commands":
            self.commands = body
            return body
        if "/messages" in path:
            message_id = path.split("/")[-1] if method == "PATCH" else "1234"
            self.messages[message_id] = {**body, "id": message_id}
            return self.messages[message_id]
        if path.startswith("channels/") and method == "PATCH":
            channel = next(c for c in self.channels if c["id"] == path.split("/")[1])
            channel.update(body)
            return channel
        raise AssertionError((method, path, kwargs))

    async def close(self):
        pass


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    monkeypatch.setenv("DISCORD_GUILD_ID", "456")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123")
    api = DiscordSetupAPI("fake")
    monkeypatch.setattr(cli, "DiscordAPI", lambda token: api)
    return api, tmp_path


def test_bootstrap_creates_limited_paid_role_and_reuses_resources(setup_api):
    api, path = setup_api
    args = argparse.Namespace(expected_server_name="Mensa Membership Test")
    asyncio.run(cli.bootstrap(args))
    asyncio.run(cli.bootstrap(args))
    assert len(api.roles) == 2
    assert api.roles[1]["permissions"] == "0"
    assert len(api.channels) == 2
    private = next(c for c in api.channels if c["name"] == "paid-members")
    overwrites = {o["id"]: o for o in private["permission_overwrites"]}
    assert int(overwrites["456"]["deny"]) & cli.VIEW
    assert int(overwrites["789"]["allow"]) & cli.VIEW
    assert len(api.messages) == 1
    assert {c["name"] for c in api.commands} == {"membership", "membership-status"}
    assert dotenv_values(path / ".env")["DISCORD_PAID_ROLE_ID"] == "789"
    assert (path / ".env").stat().st_mode & 0o777 == 0o600
    assert json.loads((path / "data/discord-setup.json").read_text())["guild_id"] == "456"


def test_bootstrap_refuses_other_server_before_writing(setup_api):
    api, _ = setup_api
    api.server_name = "Real Mensa server"
    with pytest.raises(ValueError, match="Server name does not match"):
        asyncio.run(cli.bootstrap(argparse.Namespace(expected_server_name="Mensa Membership Test")))
    assert len(api.roles) == 1
    assert api.channels == []


def test_bootstrap_does_not_modify_unowned_existing_channel(setup_api):
    api, _ = setup_api
    api.channels.append(
        {"id": "111", "name": "membership-registration", "permission_overwrites": []}
    )
    with pytest.raises(ValueError, match="was not created by this setup"):
        asyncio.run(cli.bootstrap(argparse.Namespace(expected_server_name="Mensa Membership Test")))
    assert api.channels[0]["permission_overwrites"] == []


def test_bot_install_does_not_request_administrator():
    assert not cli.INSTALL_PERMISSIONS & (1 << 3)
    assert cli.INSTALL_PERMISSIONS & cli.MANAGE_ROLES


def test_connect_updates_discord_after_tunnel_changes(setup_api):
    api, _ = setup_api
    for endpoint in ("https://old.example/interactions", "https://new.example/interactions"):
        asyncio.run(cli.connect_endpoint(endpoint))
        assert api.endpoint == endpoint


def test_connect_refuses_insecure_endpoint(setup_api):
    api, _ = setup_api
    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(cli.connect_endpoint("http://example.com/interactions"))
    assert api.endpoint is None


def test_connect_refuses_different_application(setup_api, monkeypatch):
    api, _ = setup_api
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "different")
    with pytest.raises(ValueError, match="different Discord application"):
        asyncio.run(cli.connect_endpoint("https://example.com/interactions"))
    assert api.endpoint is None


def test_bootstrap_accepts_bot_above_paid_role_with_equal_numeric_positions(setup_api):
    api, _ = setup_api
    api.roles[0]["position"] = 1
    asyncio.run(cli.bootstrap(argparse.Namespace(expected_server_name="Mensa Membership Test")))
    assert {c["name"] for c in api.channels} == {"membership-registration", "paid-members"}


@pytest.mark.parametrize("position,role_id", [(0, "700"), (1, "800")])
def test_bootstrap_rejects_bot_below_paid_role(setup_api, position, role_id):
    api, _ = setup_api
    api.roles[0].update(position=position, id=role_id)
    with pytest.raises(ValueError, match="Move the app's role above"):
        asyncio.run(cli.bootstrap(argparse.Namespace(expected_server_name="Mensa Membership Test")))
    assert api.channels == []
