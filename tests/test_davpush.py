"""Tests for the DAV Push subscription key generation, request body and PROPFIND parsing."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from when2leave.davpush import (
    SubscriptionKeys,
    _build_register_body,
    _parse_propfind,
    new_callback_token,
)

_PROPFIND_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:P="https://bitfire.at/webdav-push">
  <D:response>
    <D:href>/remote.php/dav/calendars/alice/personal/</D:href>
    <D:propstat>
      <D:prop>
        <P:topic>abc123topic</P:topic>
        <P:transports>
          <P:transport>web-push</P:transport>
        </P:transports>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
"""


def test_subscription_keys_generate_produces_valid_base64url() -> None:
    keys = SubscriptionKeys.generate()
    # base64url alphabet only, no padding.
    decoded_pubkey = base64.urlsafe_b64decode(keys.public_key_b64 + "==")
    decoded_secret = base64.urlsafe_b64decode(keys.auth_secret_b64 + "==")
    # Uncompressed P-256 point: 0x04 prefix + 32-byte X + 32-byte Y = 65 bytes.
    assert len(decoded_pubkey) == 65
    assert decoded_pubkey[0] == 0x04
    assert len(decoded_secret) == 16


def test_subscription_keys_are_unique_per_call() -> None:
    a = SubscriptionKeys.generate()
    b = SubscriptionKeys.generate()
    assert a.public_key_b64 != b.public_key_b64
    assert a.auth_secret_b64 != b.auth_secret_b64


def test_build_register_body_contains_required_elements() -> None:
    keys = SubscriptionKeys.generate()
    expires = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    body = _build_register_body("https://svc.example.com/davpush/tok123", keys, expires)

    assert "<push-resource>https://svc.example.com/davpush/tok123</push-resource>" in body
    assert "<content-encoding>aes128gcm</content-encoding>" in body
    assert keys.public_key_b64 in body
    assert keys.auth_secret_b64 in body
    assert "<content-update>" in body
    assert "push-register" in body


def test_parse_propfind_extracts_topic_and_transports() -> None:
    caps = _parse_propfind(_PROPFIND_RESPONSE)
    assert caps.supported is True
    assert caps.topic == "abc123topic"
    assert caps.transports == ("web-push",)


def test_new_callback_token_is_unique_and_urlsafe() -> None:
    tokens = {new_callback_token() for _ in range(20)}
    assert len(tokens) == 20
    for token in tokens:
        assert token.isalnum()
