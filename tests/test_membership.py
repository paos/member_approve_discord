import asyncio
import itertools
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from mensa.app import create_app
from mensa.config import Settings
from mensa.discord_api import DiscordAPI, DiscordError
from mensa.storage import Store, parse_paid_csv


class FakeDiscord:
    def __init__(self):
        self.roles = {}
        self.calls = []
        self.fail = False

    async def set_paid_role(self, guild_id, user_id, role_id, paid):
        self.calls.append((guild_id, user_id, role_id, paid))
        if self.fail:
            raise DiscordError(403)
        roles = self.roles.setdefault(user_id, {"unrelated-role"})
        if paid:
            roles.add(role_id)
        else:
            roles.discard(role_id)

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    key = SigningKey.generate()
    settings = Settings(
        "123",
        key.verify_key.encode().hex(),
        "test-bot-token",
        "456",
        "789",
        "a" * 48,
        tmp_path / "members.sqlite3",
    )
    discord = FakeDiscord()
    app = create_app(settings, discord=discord, start_worker=False)
    with TestClient(app) as client:
        yield client, app, discord, key, settings


ids = itertools.count(1000)


def interaction(user="111", number=None, kind=2, **overrides):
    data = {"name": "membership"}
    if number is not None:
        kind = 5
        data = {
            "custom_id": "mensa:link",
            "components": [
                {
                    "type": 1,
                    "components": [{"type": 4, "custom_id": "member_number", "value": number}],
                }
            ],
        }
    return {
        "id": str(next(ids)),
        "application_id": "123",
        "guild_id": "456",
        "type": kind,
        "member": {"user": {"id": user}},
        "data": data,
        **overrides,
    }


def signed(key, payload, timestamp=None):
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = key.sign(timestamp.encode() + body).signature.hex()
    return {
        "content": body,
        "headers": {
            "content-type": "application/json",
            "x-signature-timestamp": timestamp,
            "x-signature-ed25519": signature,
        },
    }


def submit(setup, payload):
    client, _, _, key, _ = setup
    return client.post("/interactions", **signed(key, payload))


def admin_headers(setup):
    return {"Authorization": "Bearer " + setup[4].admin_token}


def import_paid(setup, numbers):
    client = setup[0]
    body = {"csv": "member_number\n" + "\n".join(numbers) + "\n"}
    preview = client.post("/admin/paid-members", json=body, headers=admin_headers(setup))
    assert preview.status_code == 200, preview.text
    body["confirmation"] = preview.json()["confirmation"]
    result = client.post("/admin/paid-members", json=body, headers=admin_headers(setup))
    assert result.status_code == 200, result.text
    return result.json()


def sync(setup):
    return setup[0].post("/admin/sync", headers=admin_headers(setup)).json()


def test_discord_ping_and_private_form(setup):
    assert submit(setup, {"type": 1}).json() == {"type": 1}
    response = submit(setup, interaction()).json()
    assert response["type"] == 9
    assert response["data"]["components"][0]["components"][0]["custom_id"] == "member_number"


def test_button_opens_same_form(setup):
    response = submit(setup, interaction(kind=3, data={"custom_id": "mensa:register"})).json()
    assert response["type"] == 9


def test_paid_unpaid_and_renewal_access_preserves_other_roles(setup):
    import_paid(setup, ["10001"])
    for user, number in [("111", "10001"), ("222", "10002")]:
        response = submit(setup, interaction(user=user, number=number)).json()
        assert response["data"]["flags"] == 64
    assert sync(setup) == {"updated": 2, "failed": 0}
    discord = setup[2]
    assert discord.roles["111"] == {"789", "unrelated-role"}
    assert discord.roles["222"] == {"unrelated-role"}
    preview = import_paid(setup, ["10002"])
    assert (preview["grant_count"], preview["revoke_count"]) == (1, 1)
    sync(setup)
    assert discord.roles["111"] == {"unrelated-role"}
    assert discord.roles["222"] == {"789", "unrelated-role"}


def test_duplicate_member_claim_and_account_reassignment_rejected(setup):
    submit(setup, interaction(number="10001"))
    duplicate = submit(setup, interaction(user="222", number="10001")).json()
    assert "already linked" in duplicate["data"]["content"]
    assert setup[1].state.service.store.get_link("222") is None
    changed = submit(setup, interaction(number="10002")).json()
    assert "already linked" in changed["data"]["content"]
    assert setup[1].state.service.store.get_link("111")["member_number"] == "10001"


def test_repeated_submission_is_safe_and_keeps_leading_zeroes(setup):
    import_paid(setup, ["00123"])
    payload = interaction(number=" 00123 ")
    a = submit(setup, payload)
    b = submit(setup, payload)
    assert a.json() == b.json()
    sync(setup)
    assert setup[1].state.service.store.get_link("111")["member_number"] == "00123"
    assert "789" in setup[2].roles["111"]


def test_links_and_payment_list_survive_restart(setup):
    import_paid(setup, ["10001"])
    submit(setup, interaction(number="10001"))
    reopened = Store(setup[4].database_path)
    assert reopened.get_link("111")["paid"] == 1
    assert reopened.summary()["linked_accounts"] == 1
    assert reopened.targets()[0]["user_id"] == "111"


def test_unsigned_forged_and_expired_requests_are_rejected(setup):
    client, _, discord, key, _ = setup
    assert client.post("/interactions", json=interaction(number="10001")).status_code == 401
    assert (
        client.post("/interactions", **signed(SigningKey.generate(), {"type": 1})).status_code
        == 401
    )
    assert (
        client.post("/interactions", **signed(key, {"type": 1}, time.time() - 600)).status_code
        == 401
    )
    assert discord.calls == []


def test_wrong_server_and_application_cannot_link(setup):
    assert submit(setup, interaction(number="10001", guild_id="999")).json()["data"]["flags"] == 64
    assert submit(setup, interaction(number="10001", application_id="999")).status_code == 403
    assert setup[1].state.service.store.summary()["linked_accounts"] == 0


@pytest.mark.parametrize("number", ["", "abc", "12 34", "١٢٣", "1" * 21, "<@123>"])
def test_invalid_member_numbers_never_create_links(setup, number):
    response = submit(setup, interaction(number=number)).json()
    assert "digits" in response["data"]["content"]
    assert setup[1].state.service.store.summary()["linked_accounts"] == 0


def test_no_paid_snapshot_never_changes_roles(setup):
    submit(setup, interaction(number="10001"))
    assert sync(setup)["waiting_for_paid_list"]
    assert setup[2].calls == []


def test_failed_role_change_is_retained_and_retried(setup):
    import_paid(setup, ["10001"])
    submit(setup, interaction(number="10001"))
    setup[2].fail = True
    assert sync(setup)["failed"] == 1
    row = setup[1].state.service.store.get_link("111")
    assert row["pending"] == 1 and "403" in row["last_error"]
    setup[2].fail = False
    assert sync(setup)["updated"] == 1
    assert setup[1].state.service.store.get_link("111")["pending"] == 0


def test_admin_endpoints_require_token(setup):
    client = setup[0]
    assert client.get("/admin/status").status_code == 401
    assert (
        client.post("/admin/paid-members", json={"csv": "member_number\n10001"}).status_code == 401
    )
    assert client.post("/admin/sync").status_code == 401
    assert client.post("/admin/unlink/111").status_code == 401
    assert (
        client.get("/admin/status", headers=[(b"authorization", b"Bearer \xff")]).status_code == 401
    )


def test_preview_does_not_mutate_and_stale_confirmation_is_rejected(setup):
    client = setup[0]
    doc = {"csv": "member_number\n10001\n"}
    preview = client.post("/admin/paid-members", headers=admin_headers(setup), json=doc).json()
    assert not setup[1].state.service.store.initialized()
    import_paid(setup, ["10002"])
    response = client.post(
        "/admin/paid-members",
        headers=admin_headers(setup),
        json={**doc, "confirmation": preview["confirmation"]},
    )
    assert response.status_code == 409
    assert setup[1].state.service.store.summary()["paid_members"] == 1


@pytest.mark.parametrize(
    "csv",
    ["wrong_header\n10001", "member_number\nabc", "member_number\n1\n1", "member_number\n1,2"],
)
def test_invalid_imports_preserve_existing_paid_list(setup, csv):
    import_paid(setup, ["10001"])
    response = setup[0].post("/admin/paid-members", headers=admin_headers(setup), json={"csv": csv})
    assert response.status_code == 400
    assert setup[1].state.service.store.summary()["paid_members"] == 1


def test_empty_paid_list_requires_explicit_override(setup):
    import_paid(setup, ["10001"])
    client = setup[0]
    doc = {"csv": "member_number\n"}
    preview = client.post("/admin/paid-members", headers=admin_headers(setup), json=doc).json()
    doc["confirmation"] = preview["confirmation"]
    assert (
        client.post("/admin/paid-members", headers=admin_headers(setup), json=doc).status_code
        == 400
    )
    doc["allow_empty"] = True
    assert (
        client.post("/admin/paid-members", headers=admin_headers(setup), json=doc).status_code
        == 200
    )


def test_unlink_removes_paid_access_and_releases_number(setup):
    import_paid(setup, ["10001"])
    submit(setup, interaction(number="10001"))
    sync(setup)
    setup[0].post("/admin/unlink/111", headers=admin_headers(setup))
    sync(setup)
    assert setup[2].roles["111"] == {"unrelated-role"}
    submit(setup, interaction(user="222", number="10001"))
    sync(setup)
    assert "789" in setup[2].roles["222"]


def test_full_sync_repairs_role_drift(setup):
    import_paid(setup, ["10001"])
    submit(setup, interaction(number="10001"))
    sync(setup)
    setup[2].roles["111"].discard("789")
    sync(setup)
    assert "789" in setup[2].roles["111"]


def test_csv_bom_and_leading_zeroes():
    assert parse_paid_csv("\ufeffmember_number\r\n00123\r\n") == {"00123"}


def test_slow_discord_does_not_block_interaction_acknowledgement_and_stale_sync_retries(setup):
    async def scenario():
        _, app, discord, key, _ = setup
        service = app.state.service
        service.store.replace_paid({"10001"})
        service.store.link("111", "10001")
        entered, release = asyncio.Event(), asyncio.Event()
        original = discord.set_paid_role

        async def slow(*args):
            entered.set()
            await release.wait()
            await original(*args)

        discord.set_paid_role = slow
        job = asyncio.create_task(service.reconcile())
        await entered.wait()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            response = await asyncio.wait_for(
                c.post("/interactions", **signed(key, interaction(user="222"))), 1
            )
            assert response.json()["type"] == 9
        service.store.replace_paid({"10002"})
        release.set()
        await job
        assert service.store.get_link("111")["pending"] == 1
        discord.set_paid_role = original
        await service.reconcile()
        assert "789" not in discord.roles["111"]

    asyncio.run(scenario())


def test_discord_api_uses_only_target_role_and_handles_rate_limit():
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, json={"retry_after": 0})
            return httpx.Response(204)

        api = DiscordAPI("secret")
        await api.close()
        api.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://discord.com/api/v10/"
        )
        await api.set_paid_role("456", "111", "789", True)
        await api.set_paid_role("456", "111", "789", False)
        assert [r.method for r in requests] == ["PUT", "PUT", "DELETE"]
        assert all(r.url.path == "/api/v10/guilds/456/members/111/roles/789" for r in requests)
        await api.close()

    asyncio.run(scenario())
