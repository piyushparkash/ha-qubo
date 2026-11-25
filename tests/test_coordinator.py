"""Login, session reuse and device-ID behaviour of the coordinator."""
from __future__ import annotations

import types

import pytest
from qubopy.devices import QuboAirPurifier
from qubopy.exceptions import QuboAuthError, QuboConnectionError, QuboError

from custom_components.qubo import coordinator as coord_mod
from custom_components.qubo.const import (
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
)
from tests.conftest import UpdateFailed

RATE_LIMIT_MSG = "Email login failed (HTTP 400: ERR_SM_050 - Login Limit reached)"


class FakeTokens:
    def __init__(self, access, refresh, user_uuid):
        self.access_token = access
        self.refresh_token = refresh
        self.user_uuid = user_uuid


class FakeAuth:
    """Records how it was built and used; behaviour is steered per test."""

    instances: list["FakeAuth"] = []
    file_device_id = "FILE-ID"
    login_error: Exception | None = None
    issued = ("ACCESS-1", "REFRESH-1", "USER-1")

    def __init__(self, device_id=None):
        self.device_id = device_id or self.file_device_id
        self.built_with_device_id = device_id
        self.tokens = None
        self.login_calls = 0
        self.set_tokens_calls = 0
        FakeAuth.instances.append(self)

    async def login_with_email(self, email, password, device_attribute=None):
        self.login_calls += 1
        if FakeAuth.login_error is not None:
            raise FakeAuth.login_error
        self.tokens = FakeTokens(*FakeAuth.issued)
        return self.tokens

    def set_tokens(self, access_token, refresh_token=None, user_uuid=""):
        self.set_tokens_calls += 1
        self.tokens = FakeTokens(access_token, refresh_token, user_uuid)

    async def close(self):
        pass


class FakePurifier(QuboAirPurifier):
    """A real purifier built from minimal info (unit_uuid=None => no MQTT)."""

    def __init__(self):
        info = types.SimpleNamespace(
            uuid="dev-1", device_id="dev-1", name="Purifier", unit_uuid=None
        )
        super().__init__(info, client=None)

    async def refresh(self):
        pass


class FakeClient:
    reject_next = 0  # raise QuboAuthError this many times before succeeding

    def __init__(self, auth):
        self.auth = auth

    async def get_all_devices(self):
        if FakeClient.reject_next > 0:
            FakeClient.reject_next -= 1
            raise QuboAuthError("session rejected")
        return [FakePurifier()]

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeAuth.instances = []
    FakeAuth.file_device_id = "FILE-ID"
    FakeAuth.login_error = None
    FakeAuth.issued = ("ACCESS-1", "REFRESH-1", "USER-1")
    FakeClient.reject_next = 0
    monkeypatch.setattr(coord_mod, "QuboAuth", FakeAuth)
    monkeypatch.setattr(coord_mod, "QuboClient", FakeClient)


def make(hass, entry):
    return coord_mod.QuboDataUpdateCoordinator(hass, entry)


EMAIL_ENTRY = {
    CONF_AUTH_METHOD: AUTH_METHOD_EMAIL,
    "email": "user@example.com",
    "password": "pw",
}


# --- device ID ------------------------------------------------------------


async def test_legacy_entry_keeps_file_device_id_and_saves_it(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    await make(hass, entry)._async_ensure_client()
    assert FakeAuth.instances[0].built_with_device_id is None  # old behaviour
    assert entry.data[CONF_DEVICE_ID] == "FILE-ID"


async def test_stored_device_id_is_used_instead_of_the_file(hass, make_entry):
    entry = make_entry({**EMAIL_ENTRY, CONF_DEVICE_ID: "STORED-ID"})
    FakeAuth.file_device_id = "DIFFERENT-AFTER-REBUILD"
    await make(hass, entry)._async_ensure_client()
    assert FakeAuth.instances[0].device_id == "STORED-ID"
    assert entry.data[CONF_DEVICE_ID] == "STORED-ID"


async def test_blank_stored_device_id_is_treated_as_missing(hass, make_entry):
    entry = make_entry({**EMAIL_ENTRY, CONF_DEVICE_ID: ""})
    await make(hass, entry)._async_ensure_client()
    assert entry.data[CONF_DEVICE_ID] == "FILE-ID"


async def test_device_id_survives_repeated_setups(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    await make(hass, entry)._async_ensure_client()
    FakeAuth.file_device_id = "NEW-RANDOM-AFTER-REBUILD"
    await make(hass, entry)._async_ensure_client()
    assert FakeAuth.instances[1].device_id == "FILE-ID"


# --- session reuse --------------------------------------------------------


async def test_first_email_setup_logs_in_once_and_saves_the_session(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    await make(hass, entry)._async_update_data()
    assert FakeAuth.instances[0].login_calls == 1
    assert entry.data[CONF_ACCESS_TOKEN] == "ACCESS-1"
    assert entry.data[CONF_REFRESH_TOKEN] == "REFRESH-1"
    assert entry.data[CONF_USER_UUID] == "USER-1"


async def test_restart_reuses_saved_session_without_logging_in(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    await make(hass, entry)._async_update_data()  # first run logs in
    await make(hass, entry)._async_update_data()  # "restart"
    second = FakeAuth.instances[1]
    assert second.login_calls == 0
    assert second.set_tokens_calls == 1


async def test_rejected_saved_session_falls_back_to_one_password_login(hass, make_entry):
    entry = make_entry(
        {
            **EMAIL_ENTRY,
            CONF_ACCESS_TOKEN: "OLD",
            CONF_REFRESH_TOKEN: "OLD-R",
            CONF_USER_UUID: "USER-1",
        }
    )
    FakeClient.reject_next = 1
    FakeAuth.issued = ("ACCESS-2", "REFRESH-2", "USER-1")
    await make(hass, entry)._async_update_data()
    auth = FakeAuth.instances[0]
    assert auth.set_tokens_calls == 1  # tried the saved session first
    assert auth.login_calls == 1  # then exactly one fresh login
    assert entry.data[CONF_REFRESH_TOKEN] == "REFRESH-2"  # and saved the new one


async def test_unchanged_session_does_not_rewrite_the_entry(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    coordinator = make(hass, entry)
    await coordinator._async_update_data()
    writes = []
    original = hass.config_entries.async_update_entry

    def spy(e, *, data):
        writes.append(data)
        original(e, data=data)

    hass.config_entries.async_update_entry = spy
    await coordinator._async_update_data()
    assert writes == []


async def test_phone_entry_never_does_a_password_login(hass, make_entry):
    entry = make_entry(
        {
            CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
            CONF_ACCESS_TOKEN: "A",
            CONF_REFRESH_TOKEN: "R",
            CONF_USER_UUID: "U",
        }
    )
    await make(hass, entry)._async_update_data()
    assert FakeAuth.instances[0].login_calls == 0


# --- login limit ----------------------------------------------------------


async def test_login_limit_backs_off_instead_of_retrying_every_poll(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    FakeAuth.login_error = QuboAuthError(RATE_LIMIT_MSG)
    coordinator = make(hass, entry)
    for _ in range(4):  # four polls in a row
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    # one real attempt, then it stops calling the login endpoint
    assert FakeAuth.instances[0].login_calls == 1


async def test_ordinary_login_failure_is_not_treated_as_a_rate_limit(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    FakeAuth.login_error = QuboAuthError("Email login failed (HTTP 400: ERR_SM_002)")
    coordinator = make(hass, entry)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator._login_blocked_until == 0.0


async def test_connection_error_raises_update_failed(hass, make_entry):
    entry = make_entry(EMAIL_ENTRY)
    coordinator = make(hass, entry)
    await coordinator._async_ensure_client()

    async def boom():
        raise QuboConnectionError("down")

    coordinator.client.get_all_devices = boom
    coordinator._logged_in = True
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


def test_qubo_auth_error_is_a_qubo_error():
    assert issubclass(QuboAuthError, QuboError)
