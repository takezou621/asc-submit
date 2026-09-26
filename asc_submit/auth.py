"""ES256 JWT generation for the App Store Connect API.

Uses only the Python standard library plus the ``openssl`` binary that ships
with macOS and virtually every Linux distribution, so there is nothing to
pip install. If the optional ``cryptography`` package happens to be installed
we prefer it (fewer subprocesses), but nothing requires it.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

# JWTs live for 20 minutes; Apple ignores tokens issued for longer.
TTL_SECONDS = 20 * 60


class AuthError(RuntimeError):
    """Raised when the signing key cannot be used to produce a JWT."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_openssl(p8_path: str, signing_input: bytes) -> bytes:
    """Sign with ``openssl dgst -sha256 -sign`` and convert DER to raw R||S."""
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", p8_path],
        input=signing_input,
        capture_output=True,
        check=True,
    ).stdout

    # DER layout: SEQUENCE { r INTEGER, s INTEGER }. Each INTEGER carries a
    # one-byte tag and a length field, and may itself be padded with a leading
    # 0x00 (33 bytes) or shortened to 31 bytes. RFC 7518 ES256 wants exactly
    # 32 bytes of R followed by 32 bytes of S.
    def read_length(buf: bytes, idx: int) -> tuple[int, int]:
        length = buf[idx]
        if length & 0x80:
            nbytes = length & 0x7F
            length = int.from_bytes(buf[idx + 1 : idx + 1 + nbytes], "big")
            idx += nbytes
        return length, idx + 1

    _, pos = read_length(der, 1)  # skip SEQUENCE tag + length
    length, _ = read_length(der, pos + 1)
    r = der[pos + 2 : pos + 2 + length]
    pos = pos + 2 + length
    length, _ = read_length(der, pos + 1)
    s = der[pos + 2 : pos + 2 + length]
    return r[-32:].rjust(32, b"\0") + s[-32:].rjust(32, b"\0")


def _write_key_file(p8: str) -> str:
    """Allow inline PEM content instead of a path; return a real file path."""
    if p8.lstrip().startswith("-----BEGIN"):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".p8")
        with Path(path).open("wb") as fh:
            Path(path).chmod(0o600)
            fh.write(p8.encode("utf-8"))
        import os

        os.close(fd)
        return path
    return p8


def load_key(p8: str) -> tuple[str, bool]:
    """Resolve the .p8 value to (path, is_temporary).

    ``p8`` may be a filesystem path or the PEM content itself.
    """
    if p8.lstrip().startswith("-----BEGIN"):
        return _write_key_file(p8), True
    path = Path(p8).expanduser()
    if not path.is_file():
        raise AuthError(f"API key file not found: {path}")
    return str(path), False


def make_token(key_id: str, issuer_id: str, p8: str, now: int | None = None) -> str:
    """Build a signed Bearer token for api.appstoreconnect.apple.com."""
    if not key_id or not issuer_id:
        raise AuthError("Both key id (ASC_KEY_ID) and issuer id (ASC_ISSUER) are required")

    header = _b64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
    issued = now if now is not None else int(time.time())
    payload = _b64url(
        json.dumps(
            {"iss": issuer_id, "iat": issued - 60, "exp": issued + TTL_SECONDS, "aud": "appstoreconnect-v1"},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")

    try:
        import cryptography  # noqa: F401

        have_cryptography = True
    except ImportError:
        have_cryptography = False

    path, temporary = load_key(p8)
    try:
        if have_cryptography:
            sig = _sign_cryptography(path, signing_input)
        else:
            if shutil.which("openssl") is None:
                raise AuthError(
                    "Neither the 'cryptography' package nor the 'openssl' binary is available; "
                    "install one of them to sign App Store Connect API tokens"
                )
            sig = _sign_openssl(path, signing_input)
    finally:
        if temporary:
            Path(path).unlink(missing_ok=True)
    return f"{header}.{payload}.{_b64url(sig)}"


def _sign_cryptography(p8_path: str, signing_input: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = serialization.load_pem_private_key(Path(p8_path).read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise AuthError(f"{p8_path} does not contain an EC private key")
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))

    def read_length(buf: bytes, idx: int) -> tuple[int, int]:
        length = buf[idx]
        if length & 0x80:
            nbytes = length & 0x7F
            length = int.from_bytes(buf[idx + 1 : idx + 1 + nbytes], "big")
            idx += nbytes
        return length, idx + 1

    _, pos = read_length(der, 1)
    length, _ = read_length(der, pos + 1)
    r = der[pos + 2 : pos + 2 + length]
    pos = pos + 2 + length
    length, _ = read_length(der, pos + 1)
    s = der[pos + 2 : pos + 2 + length]
    return r[-32:].rjust(32, b"\0") + s[-32:].rjust(32, b"\0")
