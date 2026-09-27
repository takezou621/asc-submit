"""High-level App Store shipping operations built on the raw client.

Each function is idempotent where Apple allows it: existing resources are
updated rather than duplicated, so re-running a submit flow after a partial
failure is safe.
"""

from __future__ import annotations

import hashlib
import sys
import time

from .client import ApiError, Client

# App Store Connect API enum is "MAC_OS" (not "OS_X" — the latter is rejected
# with HTTP 409 "not a valid value" when creating a version).
MACOS_PLATFORM = "MAC_OS"
RELEASE_TYPES = {"AFTER_APPROVAL", "MANUAL", "SCHEDULED"}
# App Store Connect rejects unknown locales only at write time, so callers can
# pass any locale their App record has enabled (e.g. ja, en-US, zh-Hans, ko,
# es-ES, pt-BR). We normalize the few historical aliases Apple renamed.
LOCALE_ALIASES = {"ja-JP": "ja", "zh-CN": "zh-Hans", "es-ES": "es-ES", "ko-KR": "ko"}


def normalize_locale(locale: str) -> str:
    return LOCALE_ALIASES.get(locale, locale)


# Client-side mirrors of App Store Connect's field limits, enforced so a bad
# value fails here instead of at paste time. Apple's own docs disagree on the
# keywords field (the version-information reference says 100 bytes, the
# product-page guide says 100 characters); characters is the limit apps have
# actually been accepted against with multi-byte CJK keywords, so that is what
# we enforce — callers with non-ASCII keywords should still keep an eye on bytes.
KEYWORDS_MAX_CHARS = 100
SUBTITLE_MAX_CHARS = 30


def _validate_localized_text(field: str, value: str, max_chars: int) -> str:
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{field}: line breaks are not allowed (ASC renders the field as a single line)")
    if len(value) > max_chars:
        raise SystemExit(f"{field}: {len(value)} characters exceeds the {max_chars}-character limit")
    return value


# ---------------------------------------------------------------------------
# lookup helpers


def find_app(client: Client, app: str) -> dict:
    """Resolve an app by Apple ID or bundle ID and return the app resource."""
    if app.isdigit():
        return client.get(f"/v1/apps/{app}").get("data", {})
    found = client.get_all("/v1/apps", {"filter[bundleId]": app})
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(f"No app found with bundleId {app!r}")
    raise SystemExit(f"bundleId {app!r} matched {len(found)} apps; pass the Apple ID instead")


def list_versions(client: Client, app_id: str) -> list[dict]:
    return client.get_all(
        f"/v1/apps/{app_id}/appStoreVersions",
        {"fields[appStoreVersions]": "versionString,appStoreState,platform,releaseType"},
    )


def resolve_version(client: Client, app_id: str, version_string: str, platform: str | None = None) -> dict | None:
    """Return the appStoreVersion resource for a version string, or None."""
    for v in list_versions(client, app_id):
        if v["attributes"]["versionString"] == version_string and (
            platform is None or v["attributes"].get("platform") == platform
        ):
            return v
    return None


def require_version(client: Client, app_id: str, version_string: str) -> dict:
    version = resolve_version(client, app_id, version_string)
    if version is None:
        raise SystemExit(
            f"Version {version_string!r} does not exist on app {app_id}. "
            f"Create it first: asc-submit create-version {app_id} --version {version_string}"
        )
    return version


# ---------------------------------------------------------------------------
# version lifecycle


def create_version(
    client: Client,
    app_id: str,
    version_string: str,
    release_type: str = "AFTER_APPROVAL",
    platform: str = MACOS_PLATFORM,
) -> dict:
    """Create the version, or return the existing one (idempotent)."""
    if release_type not in RELEASE_TYPES:
        raise SystemExit(f"--release must be one of {sorted(RELEASE_TYPES)}")
    existing = resolve_version(client, app_id, version_string, platform)
    if existing:
        return existing
    return client.post(
        "/v1/appStoreVersions",
        {
            "data": {
                "type": "appStoreVersions",
                "attributes": {"versionString": version_string, "platform": platform, "releaseType": release_type},
                "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
            }
        },
    )["data"]


def list_builds(client: Client, app_id: str) -> list[dict]:
    return client.get_all(
        f"/v1/apps/{app_id}/builds",
        {"fields[builds]": "version,processingState"},
    )


def find_build(client: Client, app_id: str, build_string: str) -> dict | None:
    for b in list_builds(client, app_id):
        if b["attributes"].get("version") == build_string:
            return b
    return None


def wait_for_build(
    client: Client,
    app_id: str,
    build_string: str,
    want: str = "VALID",
    timeout: int = 1800,
    poll: int = 30,
    log=lambda msg: print(msg, flush=True),
) -> dict:
    """Poll until the build reaches ``want`` (e.g. VALID) or raise."""
    deadline = time.time() + timeout
    while True:
        build = find_build(client, app_id, build_string)
        if build:
            state = build["attributes"].get("processingState", "?")
            if state == want:
                return build
            log(f"build {build_string} state: {state}")
            if state in {"FAILED", "INVALID"}:
                raise SystemExit(
                    f"Build {build_string} finished in state {state}. Check the delivery "
                    "email from Apple for the ITMS error that caused it."
                )
        else:
            log(f"build {build_string} not listed yet")
        if time.time() > deadline:
            raise SystemExit(f"Timed out waiting for build {build_string} to reach {want}")
        time.sleep(poll)


def attach_build(client: Client, version_id: str, app_id: str, build_string: str) -> dict:
    build = find_build(client, app_id, build_string)
    if not build:
        raise SystemExit(
            f"Build {build_string} is not uploaded to App Store Connect for app {app_id}. "
            "Upload it first (xcodebuild -exportArchive destination=upload, altool or Transporter)."
        )
    # appStoreVersion's build relationship is **to-one** ("build", not "builds") —
    # PATCHing "builds" answers 404 "The relationship 'builds' does not exist"
    # (found live the first time this path ran; kilde's MAS workflow, run
    # 36282102696).
    client.patch(
        f"/v1/appStoreVersions/{version_id}/relationships/build",
        {"data": {"type": "builds", "id": build["id"]}},
    )
    return build


# ---------------------------------------------------------------------------
# version-localized metadata (What's New + description + keywords)


def _localizations(client: Client, version_id: str) -> dict[str, dict]:
    found = client.get_all(
        f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
        {"fields[appStoreVersionLocalizations]": "locale,whatsNew,description,keywords"},
    )
    return {loc["attributes"]["locale"]: loc for loc in found}


def set_localizations(
    client: Client,
    version_id: str,
    whats_new: dict[str, str] | None = None,
    descriptions: dict[str, str] | None = None,
    keywords: dict[str, str] | None = None,
) -> None:
    """Set What's New, description and/or keywords text per locale (PATCH or POST)."""
    whats_new = {normalize_locale(k): v for k, v in (whats_new or {}).items()}
    descriptions = {normalize_locale(k): v for k, v in (descriptions or {}).items()}
    keywords = {
        normalize_locale(k): _validate_localized_text(f"keywords[{k}]", v, KEYWORDS_MAX_CHARS)
        for k, v in (keywords or {}).items()
    }
    if not whats_new and not descriptions and not keywords:
        return
    existing = _localizations(client, version_id)

    for locale in sorted(set(whats_new) | set(descriptions) | set(keywords)):
        attrs = {}
        if locale in whats_new:
            attrs["whatsNew"] = whats_new[locale]
        if locale in descriptions:
            attrs["description"] = descriptions[locale]
        if locale in keywords:
            attrs["keywords"] = keywords[locale]
        if not attrs:
            continue
        if locale in existing:
            client.patch(
                f"/v1/appStoreVersionLocalizations/{existing[locale]['id']}",
                {"data": {"type": "appStoreVersionLocalizations", "id": existing[locale]['id'], "attributes": attrs}},
            )
        else:
            client.post(
                f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
                {
                    "data": {
                        "type": "appStoreVersionLocalizations",
                        "attributes": {"locale": locale, **attrs},
                        "relationships": {
                            "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
                        },
                    }
                },
            )


# ---------------------------------------------------------------------------
# app-level localized metadata (subtitle)


# The app record keeps one appInfo per pipeline state (READY_FOR_SALE,
# WAITING_FOR_REVIEW, PREPARE_FOR_SUBMISSION, …). Name and subtitle live on
# the appInfoLocalizations of the record whose state is still editable — the
# ones attached to live or in-review versions reject PATCHes. So unlike the
# version-localized fields above, subtitles require a version to exist in an
# editable state first.
EDITABLE_APP_INFO_STATES = {
    "PREPARE_FOR_SUBMISSION",
    "REJECTED",
    "DEVELOPER_REJECTED",
    "METADATA_REJECTED",
    "WAITING_FOR_EXPORT_COMPLIANCE",
}


def editable_app_info(client: Client, app_id: str) -> dict:
    """Return the appInfo whose localizations can still be edited, if any."""
    infos = client.get_all(f"/v1/apps/{app_id}/appInfos")
    for info in infos:
        if info["attributes"].get("state") in EDITABLE_APP_INFO_STATES:
            return info
    states = ", ".join(sorted({i["attributes"].get("state", "?") for i in infos})) or "none"
    raise SystemExit(
        f"App {app_id} has no appInfo in an editable state ({states}). "
        "Name/subtitle travel with a version: create the next version "
        "(asc-submit create-version) before setting a subtitle."
    )


def _app_info_localizations(client: Client, app_info_id: str) -> dict[str, dict]:
    found = client.get_all(
        f"/v1/appInfos/{app_info_id}/appInfoLocalizations",
        {"fields[appInfoLocalizations]": "locale,name,subtitle"},
    )
    return {loc["attributes"]["locale"]: loc for loc in found}


def set_subtitles(client: Client, app_id: str, subtitles: dict[str, str]) -> None:
    """Set the app subtitle per locale (PATCH or POST on the editable appInfo)."""
    subtitles = {
        normalize_locale(k): _validate_localized_text(f"subtitle[{k}]", v, SUBTITLE_MAX_CHARS)
        for k, v in (subtitles or {}).items()
    }
    if not subtitles:
        return
    info = editable_app_info(client, app_id)
    existing = _app_info_localizations(client, info["id"])

    for locale in sorted(subtitles):
        attrs = {"subtitle": subtitles[locale]}
        if locale in existing:
            client.patch(
                f"/v1/appInfoLocalizations/{existing[locale]['id']}",
                {"data": {"type": "appInfoLocalizations", "id": existing[locale]["id"], "attributes": attrs}},
            )
        else:
            client.post(
                f"/v1/appInfos/{info['id']}/appInfoLocalizations",
                {
                    "data": {
                        "type": "appInfoLocalizations",
                        "attributes": {"locale": locale, **attrs},
                        "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info["id"]}}},
                    }
                },
            )


# ---------------------------------------------------------------------------
# App Review notes


def set_review_notes(client: Client, version_id: str, notes: str) -> None:
    """Update the App Review Information notes shown to Apple's reviewers."""
    try:
        detail = client.get(f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail").get("data")
    except ApiError as err:
        if err.status == 404:
            detail = None
        else:
            raise
    if detail is None:
        client.post(
            "/v1/appStoreReviewDetails",
            {
                "data": {
                    "type": "appStoreReviewDetails",
                    "attributes": {"notes": notes},
                    "relationships": {
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
                    },
                }
            },
        )
        return
    client.patch(
        f"/v1/appStoreReviewDetails/{detail['id']}",
        {"data": {"type": "appStoreReviewDetails", "id": detail["id"], "attributes": {"notes": notes}}},
    )


# ---------------------------------------------------------------------------
# screenshots


def _localization_for(client: Client, version_id: str, locale: str) -> dict:
    loc = _localizations(client, version_id).get(locale)
    if loc is None:
        loc = client.post(
            f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
            {
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "attributes": {"locale": locale},
                    "relationships": {
                        "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}
                    },
                }
            },
        )["data"]
    return loc


def ensure_screenshot_set(client: Client, localization_id: str, display_type: str) -> dict:
    sets = client.get_all(
        f"/v1/appStoreVersionLocalizations/{localization_id}/appScreenshotSets",
        {"filter[screenshotDisplayType]": display_type},
    )
    if sets:
        return sets[0]
    return client.post(
        "/v1/appScreenshotSets",
        {
            "data": {
                "type": "appScreenshotSets",
                "attributes": {"screenshotDisplayType": display_type},
                "relationships": {
                    "appStoreVersionLocalization": {
                        "data": {"type": "appStoreVersionLocalizations", "id": localization_id}
                    }
                },
            }
        },
    )["data"]


def list_screenshots(client: Client, set_id: str) -> list[dict]:
    return client.get_all(f"/v1/appScreenshotSets/{set_id}/appScreenshots", {"fields[appScreenshots]": "fileName"})


def upload_screenshot(client: Client, set_id: str, path: str, log=lambda m: None) -> dict:
    """Reserve, upload and commit one screenshot file."""
    from pathlib import Path

    data = Path(path).read_bytes()
    file_name = Path(path).name
    checksum = hashlib.md5(data).hexdigest()  # Apple's sourceFileChecksum is MD5

    created = client.post(
        "/v1/appScreenshots",
        {
            "data": {
                "type": "appScreenshots",
                "attributes": {"fileName": file_name, "fileSize": len(data)},
                "relationships": {
                    "appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}
                },
            }
        },
    )
    shot = created["data"]
    shot_id = shot["id"]
    operations = created["data"]["attributes"].get("uploadOperations") or []

    for index, op in enumerate(operations):
        offset = op.get("offset") or 0
        length = op.get("length") or len(data)
        chunk = data[offset : offset + length]
        # Upload endpoints are pre-signed; no Authorization header, no JSON.
        client.raw_upload(op.get("method", "PATCH"), op["url"], chunk)
        log(f"  uploaded part {index + 1}/{len(operations)} of {file_name}")

    return client.patch(
        f"/v1/appScreenshots/{shot_id}",
        {
            "data": {
                "type": "appScreenshots",
                "id": shot_id,
                "attributes": {"uploaded": True, "sourceFileChecksum": checksum},
            }
        },
    )


def set_screenshots(
    client: Client,
    version_id: str,
    locale: str,
    files: list[str],
    display_type: str = "APP_DESKTOP",
    replace: bool = False,
    log=lambda m: print(m, flush=True),
) -> None:
    """Upload screenshots for one locale; optionally clear the set first.

    Upload order defines display order — files are sent one by one, in the
    order given.
    """
    loc = _localization_for(client, version_id, normalize_locale(locale))
    shot_set = ensure_screenshot_set(client, loc["id"], display_type)

    if replace:
        for existing in list_screenshots(client, shot_set["id"]):
            client.delete(f"/v1/appScreenshots/{existing['id']}")
            log(f"  removed existing {existing['attributes'].get('fileName', existing['id'])}")

    for path in files:
        log(f"uploading {path}")
        upload_screenshot(client, shot_set["id"], path, log)


# ---------------------------------------------------------------------------
# submission


def _open_review_submission(client: Client, version_id: str) -> dict | None:
    """Return the version's reviewSubmission resource, if one exists."""
    try:
        doc = client.get(f"/v1/appStoreVersions/{version_id}/reviewSubmissions")
    except ApiError as err:
        if err.status == 404:
            return None
        raise
    subs = doc.get("data") or []
    return subs[0] if subs else None


def submit_for_review(client: Client, version_id: str, platform: str = MACOS_PLATFORM) -> None:
    """Submit the version for App Review via the reviewSubmissions API.

    The legacy POST /v1/appStoreVersionSubmissions is deprecated: it answers
    403 "The resource 'appStoreVersionSubmissions' does not allow 'CREATE'.
    Allowed operation is: DELETE" (found live on kilde's 0.8.1 submission,
    run 36284583404). The reviewSubmissions flow is create → link the version
    (at creation, via appStoreVersionForReview) → PATCH submitted: true.
    """
    sub = _open_review_submission(client, version_id)
    if sub is None:
        try:
            sub = client.post(
                "/v1/reviewSubmissions",
                {
                    "data": {
                        "type": "reviewSubmissions",
                        "attributes": {"platform": platform},
                        "relationships": {
                            "appStoreVersionForReview": {
                                "data": {"type": "appStoreVersions", "id": version_id}
                            }
                        },
                    }
                },
            )["data"]
        except ApiError as err:
            if err.status in {409, 422}:
                # A submission may already exist (e.g. retried after a timeout).
                sub = _open_review_submission(client, version_id)
                if sub is None:
                    raise
            else:
                raise

    try:
        client.patch(
            f"/v1/reviewSubmissions/{sub['id']}",
            {
                "data": {
                    "type": "reviewSubmissions",
                    "id": sub["id"],
                    "attributes": {"submitted": True},
                }
            },
        )
    except ApiError as err:
        if err.status in {409, 422}:
            print("Version already has a pending submission; nothing to do.", file=sys.stderr)
            return
        raise


def cancel_submission(client: Client, version_id: str) -> None:
    sub = _open_review_submission(client, version_id)
    if sub is None:
        # Fall back to the legacy submission resource (only cancel — DELETE —
        # still works there for versions submitted before the deprecation).
        try:
            doc = client.get(f"/v1/appStoreVersions/{version_id}/appStoreVersionSubmission")
        except ApiError as err:
            if err.status == 404:
                raise SystemExit("This version has no pending submission to cancel")
            raise
        submission = doc.get("data")
        if not submission:
            raise SystemExit("This version has no pending submission to cancel")
        client.delete(f"/v1/appStoreVersionSubmissions/{submission['id']}")
        return
    client.delete(f"/v1/reviewSubmissions/{sub['id']}")
