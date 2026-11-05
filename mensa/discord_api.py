import asyncio

import httpx


class DiscordError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Discord API returned HTTP {status}")


class DiscordAPI:
    def __init__(self, token: str):
        self.client = httpx.AsyncClient(
            base_url="https://discord.com/api/v10/",
            timeout=15,
            headers={"Authorization": f"Bot {token}", "User-Agent": "MensaMembership/0.1"},
        )

    async def request(self, method: str, path: str, **kwargs):
        for attempt in range(4):
            response = await self.client.request(method, path, **kwargs)
            if response.status_code == 429 and attempt < 3:
                await asyncio.sleep(min(float(response.json().get("retry_after", 1)), 60))
                continue
            if response.status_code >= 500 and attempt < 3:
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise DiscordError(response.status_code)
            return response.json() if response.content else None

    async def set_paid_role(self, guild_id: str, user_id: str, role_id: str, paid: bool):
        await self.request(
            "PUT" if paid else "DELETE", f"guilds/{guild_id}/members/{user_id}/roles/{role_id}"
        )

    async def close(self):
        await self.client.aclose()
