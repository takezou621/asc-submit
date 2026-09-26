import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from asc_submit import auth


def _generate_p8() -> str:
    """Create a throwaway P-256 key so the real signing path is exercised."""
    out = subprocess.run(
        ["openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout"],
        capture_output=True,
        check=True,
    ).stdout
    fd, path = tempfile.mkstemp(suffix=".p8")
    Path(path).write_bytes(out)
    Path(path).chmod(0o600)
    import os

    os.close(fd)
    return path


class TokenTests(unittest.TestCase):
    def test_token_verifies_against_the_public_key(self):
        key_path = _generate_p8()
        self.addCleanup(Path(key_path).unlink)
        token = auth.make_token("KEY123", "00000000-1111-2222-3333-444444444444", key_path, now=1_700_000_000)

        header_b64, payload_b64, sig_b64 = token.split(".")

        # Header/payload must be exact ES256 JWT segments.
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header_b64 + "==")), {"alg": "ES256", "kid": "KEY123", "typ": "JWT"})
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        self.assertEqual(payload["iss"], "00000000-1111-2222-3333-444444444444")
        self.assertEqual(payload["aud"], "appstoreconnect-v1")
        self.assertEqual(payload["exp"] - payload["iat"], auth.TTL_SECONDS + 60)

        # Verify the signature with openssl, converting raw R||S back to DER.
        raw = base64.urlsafe_b64decode(sig_b64 + "==")
        self.assertEqual(len(raw), 64)
        r, s = raw[:32], raw[32:]

        def der_int(value: bytes) -> bytes:
            value = value.lstrip(b"\0") or b"\0"
            if value[0] & 0x80:
                value = b"\0" + value
            return b"\x02" + bytes([len(value)]) + value

        content = der_int(r) + der_int(s)
        der = b"\x30" + bytes([len(content)]) + content

        signing_input = f"{header_b64}.{payload_b64}".encode()
        with tempfile.NamedTemporaryFile(delete=False) as sig_file:
            sig_file.write(der)
            sig_path = sig_file.name
        with tempfile.NamedTemporaryFile(delete=False) as in_file:
            in_file.write(signing_input)
            in_path = in_file.name

        pub = subprocess.run(
            ["openssl", "pkey", "-in", key_path, "-pubout"], capture_output=True, check=True
        ).stdout
        with tempfile.NamedTemporaryFile(delete=False) as pub_file:
            pub_file.write(pub)
            pub_path = pub_file.name
        verify = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", pub_path, "-signature", sig_path, in_path],
            capture_output=True,
        )
        for f in (sig_path, in_path, pub_path):
            Path(f).unlink()
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_inline_pem_content_is_supported(self):
        key_path = _generate_p8()
        self.addCleanup(Path(key_path).unlink)
        pem = Path(key_path).read_text()
        token = auth.make_token("K", "issuer", pem)
        self.assertEqual(len(token.split(".")), 3)

    def test_missing_key_file_raises(self):
        with self.assertRaises(auth.AuthError):
            auth.make_token("K", "issuer", "/nonexistent/AuthKey.p8")

    def test_missing_id_or_issuer_raises(self):
        with self.assertRaises(auth.AuthError):
            auth.make_token("", "issuer", "/dev/null")


if __name__ == "__main__":
    unittest.main()
