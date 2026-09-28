"""Thin App Store Connect API client.

HTTP transport shells out to ``curl``. The stdlib ``urllib`` client negotiates
HTTP/1.1 and Apple's edge intermittently stalls some resource paths for such
requests (observed 2026-09-26: appStoreVersionLocalizations GETs time out via
urllib while the exact same URL returns 200 in ~0.6s via curl, both over
HTTP/1.1 and HTTP/2). curl ships with macOS and virtually every Linux
distribution, so the practical dependency footprint is unchanged — the only
other external dependency is ``openssl`` for JWT signing.

Full story in docs/forensics.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

BASE_URL = "https://api.appstoreconnect.apple.com"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    """An error response from the App Store Connect API."""

    def __init__(self, status: int, errors: list[dict], context: str = ""):
        self.status = status
        self.errors = errors
        detail = "; ".join(e.get("detail") or e.get("title") or e.get("code") or "?" for e in errors)
        super().__init__(f"HTTP {status}{(' ' + context) if context else ''}: {detail}")

    @property
    def forbidden(self) -> bool:
        return self.status == 403


class TransportError(RuntimeError):
    """curl itself failed (network, timeout, missing binary)."""


@dataclass
class Client:
    token: str
    base_url: str = BASE_URL
    retries: int = 3
    timeout: int = 60
    verbose: bool = False

    # -- request plumbing ---------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
        url: str | None = None,
        headers: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """Send one request and return the parsed JSON document.

        ``url`` overrides ``base_url + path`` (used for the binary upload
        endpoints, whose URLs are issued by Apple).
        """
        target = url or f"{self.base_url}{path}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        send_headers = {"Accept": "application/json"}
        if data is not None:
            send_headers["Content-Type"] = content_type
        send_headers.update(headers or {})

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if self.verbose:
                label = f" (retry {attempt})" if attempt else ""
                print(f"  > {method} {target}{label}", flush=True)
            status, payload = _curl(
                method,
                target,
                {"Authorization": f"Bearer {self.token}", **send_headers},
                data,
                timeout or self.timeout,
            )
            if status == 0:  # transport-level failure; retry
                last_error = TransportError(payload.decode(errors="replace").strip() or "curl failed")
                if attempt == self.retries:
                    raise last_error from None
                time.sleep(2**attempt)
                continue
            if status < 300:
                return json.loads(payload) if payload else {}

            try:
                errors = json.loads(payload).get("errors") or [{"title": "HTTP error"}]
            except ValueError:
                errors = [{"title": "HTTP error", "detail": payload.decode(errors="replace")[:400]}]
            last_error = ApiError(status, errors, context=method)
            if status not in RETRYABLE_STATUS or attempt == self.retries:
                raise last_error from None
            time.sleep(2**attempt)
        raise last_error  # pragma: no cover - loop always raises or returns

    def get(self, path: str, query: dict | None = None) -> dict:
        if query:
            path = f"{path}?{urlencode(query, doseq=True)}"
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body=body)

    def patch(self, path: str, body: dict) -> dict:
        return self.request("PATCH", path, body=body)

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def raw_upload(self, method: str, url: str, data: bytes, timeout: int = 120) -> None:
        """Send raw bytes to a pre-signed upload URL (no auth header)."""
        status, payload = _curl(method, url, {"Content-Type": "application/octet-stream"}, data, timeout)
        if status == 0:
            raise TransportError(payload.decode(errors="replace").strip() or "curl failed")
        if status >= 300:
            raise ApiError(
                status,
                [{"title": "upload failed", "detail": payload.decode(errors="replace")[:400]}],
                context=method,
            )

    # -- pagination ---------------------------------------------------------

    def get_all(self, path: str, query: dict | None = None) -> list[dict]:
        """Follow ``links.next`` and collect every ``data`` item."""
        items: list[dict] = []
        query = dict(query or {})
        query.setdefault("limit", 200)
        while True:
            doc = self.get(path, query)
            items.extend(doc.get("data", []))
            next_url = (doc.get("links") or {}).get("next")
            if not next_url:
                return items
            # Apple hands back absolute URLs; split them so requests keep
            # building from base_url.
            parts = urlsplit(next_url)
            path = parts.path
            query = dict(parse_qsl(parts.query))

    # -- convenience --------------------------------------------------------

    @staticmethod
    def forbidden_hint() -> str:
        return (
            "HTTP 403 from App Store Connect. The API key role must be App Manager "
            "or higher to create versions, edit metadata or submit for review. "
            "Keys issued with the Developer role are read-only for these resources; "
            "the role cannot be changed after the key is created — issue a new key at "
            "App Store Connect > Users and Access > Integrations."
        )


def _curl(
    method: str,
    url: str,
    headers: dict,
    data: bytes | None,
    timeout: int,
) -> tuple[int, bytes]:
    """Run one curl request and return (http status, response body).

    Status 0 means curl itself failed (transport error); the payload carries
    the stderr message.
    """
    if shutil.which("curl") is None:
        raise SystemExit("curl is required but was not found on PATH")
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method, "-w", "\n%{http_code}"]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 10)
    if proc.returncode != 0:
        return 0, proc.stderr
    body, _, status_line = proc.stdout.rpartition(b"\n")
    try:
        status = int(status_line.strip())
    except ValueError:
        return 0, proc.stderr or b"unexpected curl output"
    return status, body
