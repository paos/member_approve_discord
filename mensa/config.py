import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    application_id: str
    public_key: str
    bot_token: str
    guild_id: str
    paid_role_id: str
    admin_token: str
    database_path: Path = Path("data/membership.sqlite3")
    reconcile_seconds: int = 300

    @classmethod
    def from_env(cls):
        load_dotenv()
        required = [
            "DISCORD_APPLICATION_ID",
            "DISCORD_PUBLIC_KEY",
            "DISCORD_BOT_TOKEN",
            "DISCORD_GUILD_ID",
            "DISCORD_PAID_ROLE_ID",
            "ADMIN_TOKEN",
        ]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        settings = cls(
            *(os.environ[key] for key in required),
            database_path=Path(os.getenv("DATABASE_PATH", "data/membership.sqlite3")),
            reconcile_seconds=int(os.getenv("RECONCILE_SECONDS", "300")),
        )
        if len(bytes.fromhex(settings.public_key)) != 32:
            raise ValueError("DISCORD_PUBLIC_KEY must be a 32-byte hexadecimal public key")
        if len(settings.admin_token) < 32:
            raise ValueError("ADMIN_TOKEN must be at least 32 characters")
        if settings.reconcile_seconds < 5:
            raise ValueError("RECONCILE_SECONDS must be at least 5")
        return settings
