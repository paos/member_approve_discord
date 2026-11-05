import argparse
import asyncio
import getpass
import json
import os
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import dotenv_values, load_dotenv

from mensa.discord_api import DiscordAPI, DiscordError

VIEW = 1 << 10
SEND = 1 << 11
HISTORY = 1 << 16
USE_COMMANDS = 1 << 31
MANAGE_ROLES = 1 << 28
MANAGE_CHANNELS = 1 << 4
INSTALL_PERMISSIONS = VIEW | SEND | HISTORY | USE_COMMANDS | MANAGE_ROLES | MANAGE_CHANNELS


def save_env(values):
    path = Path(".env")
    current = dict(dotenv_values(path)) if path.exists() else {}
    current.update(values)
    current.setdefault("ADMIN_TOKEN", secrets.token_urlsafe(48))
    current.setdefault("DATABASE_PATH", "data/membership.sqlite3")
    current.setdefault("APP_URL", "http://127.0.0.1:8766")
    current.setdefault("RECONCILE_SECONDS", "300")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in current.items():
            f.write(f"{key}={json.dumps(value or '')}\n")
    path.chmod(0o600)


def require_env(key):
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Set {key} in .env first.")
    return value


def install_url(application_id, guild_id):
    return "https://discord.com/oauth2/authorize?" + urlencode(
        {
            "client_id": application_id,
            "scope": "bot applications.commands",
            "permissions": str(INSTALL_PERMISSIONS),
            "guild_id": guild_id,
            "disable_guild_select": "true",
        }
    )


async def configure(args):
    token = getpass.getpass("Paste the new test app's bot token (hidden): ").strip()
    if not token:
        raise ValueError("Token cannot be empty.")
    api = DiscordAPI(token)
    try:
        application = await api.request("GET", "oauth2/applications/@me")
        expected = os.getenv("DISCORD_APPLICATION_ID")
        if expected and application["id"] != expected:
            raise ValueError(
                "That token belongs to a different app. Use the new Mensa Membership Test app."
            )
    finally:
        await api.close()
    values = {
        "DISCORD_BOT_TOKEN": token,
        "DISCORD_APPLICATION_ID": application["id"],
        "DISCORD_PUBLIC_KEY": application["verify_key"],
        "DISCORD_GUILD_ID": args.guild,
    }
    save_env(values)
    print("Credentials saved privately to .env. Install the app in the test server:")
    print(install_url(application["id"], args.guild))


async def bootstrap(args):
    api = DiscordAPI(require_env("DISCORD_BOT_TOKEN"))
    guild_id = require_env("DISCORD_GUILD_ID")
    application_id = require_env("DISCORD_APPLICATION_ID")
    state_path = Path("data/discord-setup.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get("guild_id", guild_id) != guild_id:
        raise ValueError("Saved setup belongs to a different server. Use a separate project copy.")

    def remember(key, value):
        state.update({"guild_id": guild_id, key: value})
        state_path.write_text(json.dumps(state, indent=2) + "\n")

    try:
        guild = await api.request("GET", f"guilds/{guild_id}")
        if guild["name"] != args.expected_server_name:
            raise ValueError("Server name does not match --expected-server-name; no changes made.")
        me = await api.request("GET", "users/@me")
        roles = await api.request("GET", f"guilds/{guild_id}/roles")
        existing = [r for r in roles if r["name"] == "Paid member"]
        if len(existing) > 1:
            raise ValueError("Multiple Paid member roles exist; resolve the ambiguity first.")
        if existing:
            role = existing[0]
            if int(role["permissions"]) != 0:
                raise ValueError(
                    "Existing Paid member role has global privileges; refusing to reuse it."
                )
        else:
            role = await api.request(
                "POST",
                f"guilds/{guild_id}/roles",
                json={
                    "name": "Paid member",
                    "permissions": "0",
                    "color": 3066993,
                    "mentionable": False,
                },
            )
        role_id = role["id"]
        remember("paid_role_id", role_id)
        save_env({"DISCORD_PAID_ROLE_ID": role_id})
        roles = await api.request("GET", f"guilds/{guild_id}/roles")
        bot_role = next(r for r in roles if r.get("tags", {}).get("bot_id") == me["id"])
        bot_rank = (bot_role["position"], -int(bot_role["id"]))
        paid_rank = (role["position"], -int(role["id"]))
        if bot_rank <= paid_rank:
            raise ValueError(
                "Move the app's role above Paid member in Server Settings → Roles, then rerun."
            )
        channels = await api.request("GET", f"guilds/{guild_id}/channels")
        channels_by_name = {}
        for name, restricted in [("membership-registration", False), ("paid-members", True)]:
            overwrite = [
                {
                    "id": guild_id,
                    "type": 0,
                    "deny": str(VIEW if restricted else 0),
                    "allow": "0" if restricted else str(VIEW | SEND | HISTORY | USE_COMMANDS),
                },
                {"id": me["id"], "type": 1, "allow": str(VIEW | SEND | HISTORY), "deny": "0"},
            ]
            if restricted:
                overwrite.append(
                    {
                        "id": role_id,
                        "type": 0,
                        "allow": str(VIEW | SEND | HISTORY | USE_COMMANDS),
                        "deny": "0",
                    }
                )
            matches = [c for c in channels if c["name"] == name]
            if len(matches) > 1:
                raise ValueError(f"Multiple channels named {name}; resolve the ambiguity first.")
            if matches:
                channel = matches[0]
                if state.get(name) != channel["id"]:
                    raise ValueError(
                        f"Channel {name} exists but was not created by this setup; refusing to alter it."
                    )
                await api.request(
                    "PATCH", f"channels/{channel['id']}", json={"permission_overwrites": overwrite}
                )
            else:
                channel = await api.request(
                    "POST",
                    f"guilds/{guild_id}/channels",
                    json={
                        "name": name,
                        "type": 0,
                        "permission_overwrites": overwrite,
                        "topic": "Fake membership data only. Private test server.",
                    },
                )
                remember(name, channel["id"])
            channels_by_name[name] = channel
        commands = [
            {"name": "membership", "description": "Link your Mensa member number", "type": 1},
            {"name": "membership-status", "description": "Check your membership access", "type": 1},
        ]
        await api.request(
            "PUT", f"applications/{application_id}/guilds/{guild_id}/commands", json=commands
        )
        registration = channels_by_name["membership-registration"]["id"]
        panel = {
            "content": "**Mensa membership — test server**\nClick below to link your member number privately. "
            "Use only fake data here: **10001** and **10002** are in the initial paid list; "
            "**10003** is not. Each number can be linked to one account. "
            "Use `/membership-status` to check access.\n"
            "This test trusts the submitted number; it does not verify identity.",
            "allowed_mentions": {"parse": []},
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 1,
                            "label": "Link Mensa membership",
                            "custom_id": "mensa:register",
                        }
                    ],
                }
            ],
        }
        if state.get("panel_message_id"):
            await api.request(
                "PATCH", f"channels/{registration}/messages/{state['panel_message_id']}", json=panel
            )
        else:
            message = await api.request("POST", f"channels/{registration}/messages", json=panel)
            remember("panel_message_id", message["id"])
        print(
            json.dumps(
                {
                    "server": guild["name"],
                    "paid_role_id": role_id,
                    "registration_url": f"https://discord.com/channels/{guild_id}/{registration}",
                },
                indent=2,
            )
        )
    finally:
        await api.close()


async def connect_endpoint(endpoint):
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The interactions endpoint must be an HTTPS URL without credentials.")
    api = DiscordAPI(require_env("DISCORD_BOT_TOKEN"))
    try:
        application = await api.request("GET", "applications/@me")
        if application["id"] != require_env("DISCORD_APPLICATION_ID"):
            raise ValueError("The bot token belongs to a different Discord application.")
        application = await api.request(
            "PATCH", "applications/@me", json={"interactions_endpoint_url": endpoint}
        )
        if application.get("interactions_endpoint_url") != endpoint:
            raise ValueError("Discord did not save the interactions endpoint.")
        print("Discord connected to " + endpoint)
    finally:
        await api.close()


def admin_request(method, path, **kwargs):
    url = os.getenv("APP_URL", "http://127.0.0.1:8766").rstrip("/") + path
    response = httpx.request(
        method,
        url,
        headers={"Authorization": "Bearer " + require_env("ADMIN_TOKEN")},
        timeout=120,
        **kwargs,
    )
    if response.is_error:
        raise ValueError(f"App returned HTTP {response.status_code}: {response.text}")
    return response.json()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Mensa Discord membership administration")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser(
        "configure", help="Save a bot token privately and discover app settings"
    )
    config.add_argument("--guild", required=True)
    setup = commands.add_parser(
        "bootstrap", help="Create test roles, channels, registration panel and commands"
    )
    setup.add_argument("--expected-server-name", default="Mensa Membership Test")
    paid = commands.add_parser(
        "import-paid", help="Preview a complete paid-member CSV; add --apply to replace"
    )
    paid.add_argument("file", type=Path)
    paid.add_argument("--apply", action="store_true")
    paid.add_argument("--allow-empty", action="store_true")
    commands.add_parser("status")
    commands.add_parser("sync")
    commands.add_parser("install-url")
    connect = commands.add_parser("connect", help="Connect Discord to a running HTTPS endpoint")
    connect.add_argument("endpoint")
    unlink = commands.add_parser("unlink")
    unlink.add_argument("user_id")
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "configure":
            asyncio.run(configure(args))
        elif args.command == "bootstrap":
            asyncio.run(bootstrap(args))
        elif args.command == "connect":
            asyncio.run(connect_endpoint(args.endpoint))
        elif args.command == "install-url":
            print(
                install_url(require_env("DISCORD_APPLICATION_ID"), require_env("DISCORD_GUILD_ID"))
            )
        elif args.command == "import-paid":
            doc = {"csv": args.file.read_text(encoding="utf-8-sig")}
            preview = admin_request("POST", "/admin/paid-members", json=doc)
            print(json.dumps(preview, indent=2))
            if args.apply:
                doc.update(confirmation=preview["confirmation"], allow_empty=args.allow_empty)
                print(json.dumps(admin_request("POST", "/admin/paid-members", json=doc), indent=2))
                print(json.dumps(admin_request("POST", "/admin/sync"), indent=2))
        elif args.command == "status":
            print(json.dumps(admin_request("GET", "/admin/status"), indent=2))
        elif args.command == "sync":
            print(json.dumps(admin_request("POST", "/admin/sync"), indent=2))
        elif args.command == "unlink":
            print(json.dumps(admin_request("POST", f"/admin/unlink/{args.user_id}"), indent=2))
            print(json.dumps(admin_request("POST", "/admin/sync"), indent=2))
        elif args.command == "backup":
            if args.destination.exists():
                raise ValueError("Backup destination already exists; choose a new filename.")
            source = Path(os.getenv("DATABASE_PATH", "data/membership.sqlite3")).resolve()
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
                fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                with sqlite3.connect(args.destination) as target:
                    src.backup(target)
            print(f"Backup saved to {args.destination}")
    except (ValueError, DiscordError, httpx.HTTPError, OSError) as exc:
        # No HTTP request URLs or authorization headers are printed.
        parser.exit(
            1, f"{exc if not isinstance(exc, httpx.HTTPError) else 'Network request failed'}\n"
        )


if __name__ == "__main__":
    main()
