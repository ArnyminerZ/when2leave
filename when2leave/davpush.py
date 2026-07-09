"""WebDAV Push (webdav-push) client: discovery, subscription registration and renewal.

Implements the client side of the draft WebDAV-Push specification
(https://github.com/bitfireAT/webdav-push/), as also implemented server-side by
Nextcloud's ``nc_ext_dav_push`` app (https://github.com/bitfireAT/nc_ext_dav_push):

1. **Discovery**: an ``OPTIONS`` request against a calendar collection; the server
   advertises support by including ``webdav-push`` in the ``DAV`` response header. A
   follow-up ``PROPFIND`` (Depth: 0) against the ``{https://bitfire.at/webdav-push}``
   namespace reads ``transports``, ``topic`` and ``supported-triggers``.
2. **Registration**: a ``POST`` of a ``push-register`` XML body to the collection,
   containing a Web Push subscription (``push-resource``, ``content-encoding``,
   ``subscription-public-key``, ``auth-secret``) and the triggers we care about
   (``content-update``). The server replies ``201``/``204`` with a ``Location`` header
   (the registration URL, used to renew/delete) and an ``Expires`` header.
3. **Renewal**: re-POST the same ``push-register`` body (same ``push-resource``) before
   ``Expires``; the spec requires the server to update the existing registration rather
   than create a duplicate.
4. **Delivery**: whenever the collection changes, the server POSTs an encrypted Web Push
   message (RFC 8291, AES-128-GCM) to our ``push-resource`` URL.

**On payload decryption**: full Web Push delivery requires an EC keypair and auth
secret from us (which we do generate and register, to keep the subscription spec-valid)
and, on the server side, encrypting the push body against them. Decrypting that body
would require re-implementing RFC 8291 (ECDH + HKDF + AES-128-GCM) client-side. We
deliberately don't: instead, the receiver treats *any* POST to a calendar's callback URL
as "this collection changed" and triggers a full CalDAV re-sync of it. We don't need the
payload's contents (a sync-token) because we always resolve the actual change via a
normal CalDAV fetch; this keeps the push path simple and easy to reason about, at the
cost of one extra CalDAV round-trip per push (negligible compared to the alternative of
a subtly-wrong hand-rolled decryption path). The polling fallback exists regardless, so
this is purely a latency optimization, not a correctness-critical path.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from when2leave.logging_config import get_logger

logger = get_logger(__name__)

PUSH_NS = "https://bitfire.at/webdav-push"
DAV_NS = "DAV:"

_PROPFIND_BODY = f"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{DAV_NS}" xmlns:P="{PUSH_NS}">
  <D:prop>
    <P:transports/>
    <P:topic/>
    <P:supported-triggers/>
  </D:prop>
</D:propfind>"""


@dataclass(frozen=True, slots=True)
class PushCapabilities:
    """What a calendar collection advertises for WebDAV Push support."""

    supported: bool
    topic: str | None = None
    transports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubscriptionKeys:
    """An EC P-256 keypair + auth secret for a Web Push subscription.

    We only ever use the public parts (we never decrypt push bodies -- see module
    docstring), but a spec-valid ``push-register`` request requires them.
    """

    public_key_b64: str
    auth_secret_b64: str

    @classmethod
    def generate(cls) -> SubscriptionKeys:
        """Generate a fresh EC P-256 keypair and random 16-byte auth secret."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_bytes = private_key.public_key().public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint,
        )
        auth_secret = secrets.token_bytes(16)
        return cls(
            public_key_b64=_b64url(public_bytes),
            auth_secret_b64=_b64url(auth_secret),
        )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """The outcome of a successful push-register call."""

    registration_url: str
    expires_at: datetime


class DavPushClient:
    """Performs WebDAV Push discovery and (de)registration against a Nextcloud server."""

    def __init__(self, username: str, password: str, timeout: float = 10.0) -> None:
        self._auth = (username, password)
        self._timeout = timeout

    async def discover(self, calendar_url: str) -> PushCapabilities:
        """Check whether ``calendar_url`` advertises WebDAV Push support."""
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            options_response = await client.options(calendar_url)
            dav_header = options_response.headers.get("DAV", "")
            if "webdav-push" not in dav_header:
                return PushCapabilities(supported=False)

            propfind_response = await client.request(
                "PROPFIND",
                calendar_url,
                content=_PROPFIND_BODY,
                headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
            )
            propfind_response.raise_for_status()
            return _parse_propfind(propfind_response.text)

    async def register(
        self,
        calendar_url: str,
        push_resource_url: str,
        keys: SubscriptionKeys,
        ttl: timedelta = timedelta(hours=24),
    ) -> RegistrationResult:
        """Register a subscription, or renew it if already registered for this resource."""
        expires = datetime.now(tz=UTC) + ttl
        body = _build_register_body(push_resource_url, keys, expires)

        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            response = await client.post(
                calendar_url,
                content=body,
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
            response.raise_for_status()

        location = response.headers.get("Location", calendar_url)
        registration_url = str(httpx.URL(calendar_url).join(location))
        expires_header = response.headers.get("Expires")
        expires_at = parsedate_to_datetime(expires_header) if expires_header else expires
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        logger.info(
            "davpush.registered",
            extra={"calendar_url": calendar_url, "expires_at": expires_at.isoformat()},
        )
        return RegistrationResult(registration_url=registration_url, expires_at=expires_at)

    async def unregister(self, registration_url: str) -> None:
        """Delete a previously created subscription."""
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as client:
            response = await client.delete(registration_url)
            if response.status_code not in (204, 404):
                response.raise_for_status()
        logger.info("davpush.unregistered", extra={"registration_url": registration_url})


def _build_register_body(push_resource_url: str, keys: SubscriptionKeys, expires: datetime) -> str:
    """Build the ``push-register`` XML request body per the webdav-push draft spec."""
    expires_rfc = format_datetime(expires, usegmt=True)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<push-register xmlns="{PUSH_NS}" xmlns:D="{DAV_NS}">
  <subscription>
    <web-push-subscription>
      <push-resource>{push_resource_url}</push-resource>
      <content-encoding>aes128gcm</content-encoding>
      <subscription-public-key type="p256dh">{keys.public_key_b64}</subscription-public-key>
      <auth-secret>{keys.auth_secret_b64}</auth-secret>
    </web-push-subscription>
  </subscription>
  <trigger>
    <content-update>
      <D:depth>1</D:depth>
    </content-update>
  </trigger>
  <expires>{expires_rfc}</expires>
</push-register>"""


def _parse_propfind(xml_text: str) -> PushCapabilities:
    root = ET.fromstring(xml_text)
    ns = {"D": DAV_NS, "P": PUSH_NS}
    topic_el = root.find(".//D:prop/P:topic", ns)
    transports_el = root.find(".//D:prop/P:transports", ns)
    transport_elements = (
        transports_el.findall("P:transport", ns) if transports_el is not None else []
    )
    transports = tuple((t.text or "").strip() for t in transport_elements)
    return PushCapabilities(
        supported=True,
        topic=(topic_el.text.strip() if topic_el is not None and topic_el.text else None),
        transports=transports,
    )


def new_callback_token() -> str:
    """Generate a unique, unguessable path segment for a calendar's callback URL."""
    return uuid.uuid4().hex
