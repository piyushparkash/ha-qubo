"""Redaction and request-shape checks for the qubopy patches."""
from __future__ import annotations

import json

import pytest
from qubopy.devices import QuboAirPurifier
from qubopy.exceptions import QuboAuthError

from custom_components.qubo import qubopy_patches as patches


def test_rate_limit_marker_survives_redaction():
    body = json.dumps({"code": "ERR_SM_050", "message": ["Login Limit reached"]})
    assert "ERR_SM_050" in patches._safe_error_detail(400, body)


def test_email_addresses_are_masked():
    body = json.dumps({"code": "ERR_SM_002", "message": "user bob@example.com missing"})
    detail = patches._safe_error_detail(400, body)
    assert "bob@example.com" not in detail
    assert "<email>" in detail


def test_jwt_tokens_are_masked():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghij"
    detail = patches._safe_error_detail(401, json.dumps({"message": f"bad {jwt}"}))
    assert jwt not in detail
    assert "<token>" in detail


def test_long_opaque_strings_are_masked():
    secret = "a" * 40
    detail = patches._safe_error_detail(400, json.dumps({"message": f"key {secret}"}))
    assert secret not in detail


@pytest.mark.parametrize(
    "body",
    [
        "<html>password=hunter2 bob@example.com</html>",
        "plain text bob@example.com",
        "[1, 2]",
        "",
    ],
)
def test_unstructured_bodies_are_dropped_entirely(body):
    assert patches._safe_error_detail(500, body) == "HTTP 500"


def test_message_is_length_capped():
    detail = patches._safe_error_detail(400, json.dumps({"message": "x " * 500}))
    assert len(detail) < 260


class _Response:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, response):
        self.response = response

    def post(self, url, **kwargs):
        return self.response


class _AuthSelf:
    base_url = "https://example.invalid"
    sp_id = "sp"
    app_id = "app"
    device_id = "DEV"

    def __init__(self, response):
        self._session = _Session(response)
        self._tokens = None

    async def _get_session(self):
        return self._session


async def test_failed_login_error_never_echoes_the_response_body():
    body = '{"code":"ERR_X","message":"hi bob@example.com","debug":"SECRET-DUMP"}'
    me = _AuthSelf(_Response(400, body))
    with pytest.raises(QuboAuthError) as err:
        await patches._login_with_email(
            me, "bob@example.com", "pw", device_attribute="DEV"
        )
    text = str(err.value)
    assert "bob@example.com" not in text
    assert "SECRET-DUMP" not in text
    assert "ERR_X" in text


async def test_successful_login_stores_tokens():
    body = json.dumps({"accessToken": "A", "refreshToken": "R", "uuid": "U"})
    me = _AuthSelf(_Response(200, body))
    tokens = await patches._login_with_email(
        me, "e@x.com", "pw", device_attribute="DEV"
    )
    assert (tokens.access_token, tokens.refresh_token, tokens.user_uuid) == (
        "A",
        "R",
        "U",
    )


def test_apply_patches_is_idempotent():
    patches.apply_patches()
    first = QuboAirPurifier.turn_on
    patches.apply_patches()
    assert QuboAirPurifier.turn_on is first
