"""Command line interface.

Authentication comes from the environment or global flags:

    export ASC_KEY_PATH=~/.appstoreconnect/AuthKey_XYZ.p8
    export ASC_KEY_ID=XYZ
    export ASC_ISSUER=00000000-0000-0000-0000-000000000000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import auth, flows
from .client import ApiError, Client
from .journal import (
    DEFAULT_RUNS_DIR,
    RUNS_DIR_ENV,
    NullJournal,
    RunJournal,
    list_runs,
    read_log,
    read_run,
    resolve_runs_dir,
)

ENV_KEY_PATH = "ASC_KEY_PATH"
ENV_KEY_ID = "ASC_KEY_ID"
ENV_ISSUER = "ASC_ISSUER"
ENV_WEBHOOK_URL = "ASC_WEBHOOK_URL"


# ---------------------------------------------------------------------------
# client construction


def build_client(args) -> Client:
    key_path = getattr(args, "key_path", None) or os.environ.get(ENV_KEY_PATH) or ""
    key_id = getattr(args, "key_id", None) or os.environ.get(ENV_KEY_ID) or ""
    issuer = getattr(args, "issuer", None) or os.environ.get(ENV_ISSUER) or ""
    if not key_path:
        raise SystemExit(
            f"No API key configured. Set {ENV_KEY_PATH} / {ENV_KEY_ID} / {ENV_ISSUER} "
            "or pass --key-path / --key-id / --issuer."
        )
    token = auth.make_token(key_id, issuer, key_path)
    return Client(token=token, verbose=getattr(args, "verbose", False))


def add_auth_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key-path", help=f".p8 path or PEM content (env: {ENV_KEY_PATH})")
    parser.add_argument("--key-id", help=f"API key id (env: {ENV_KEY_ID})")
    parser.add_argument("--issuer", help=f"issuer id (env: {ENV_ISSUER})")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")


# ---------------------------------------------------------------------------
# spec support for the `run` subcommand


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if not spec.get("version"):
        raise SystemExit(f"{path}: 'version' is required")
    for locale, files in (spec.get("screenshots") or {}).items():
        for f in files:
            if not Path(f).is_file():
                raise SystemExit(f"{path}: screenshot file not found: {f}")
    return spec


# ---------------------------------------------------------------------------
# spec linting (validate, and the same check inside `run`)


# Top-level keys a spec understands, with the JSON type each expects. Keys
# starting with "_" are a comment convention and ignored.
SPEC_KNOWN_KEYS = {
    "version": str,
    "platform": str,
    "releaseType": str,
    "releaseDate": str,
    "phasedRelease": bool,
    "build": str,
    "whatsNew": dict,
    "descriptions": dict,
    "keywords": dict,
    "promotionalText": dict,
    "supportUrls": dict,
    "marketingUrls": dict,
    "subtitles": dict,
    "appNames": dict,
    "privacyUrls": dict,
    "copyright": str,
    "reviewNotes": str,
    "screenshotDisplayType": str,
    "screenshotsReplace": bool,
    "screenshots": dict,
    "submit": bool,
}

# Copy that was never finished. `run` ignores unknown keys silently, so a
# mistyped "whatsnew" ships a release without its release notes — the lint
# turns that class of mistake into a stop-the-line error.
PLACEHOLDER_PATTERNS = [
    re.compile(r"\btodo\b", re.IGNORECASE),
    re.compile(r"\bfixme\b", re.IGNORECASE),
    re.compile(r"\btbd\b", re.IGNORECASE),
    re.compile(r"\btdb\b", re.IGNORECASE),
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"<[a-zA-Z][^<>\n]{0,39}>"),  # "<app name>"-style template markers
]

SPEC_PLATFORMS = {"MAC_OS", "IOS", "TV_OS", "VISION_OS"}


def _lint_text(field: str, text: str, errors: list[str]) -> None:
    for pattern in PLACEHOLDER_PATTERNS:
        match = pattern.search(text)
        if match:
            errors.append(f"{field}: placeholder text {match.group(0)!r} — finish the copy before shipping")
            return


def _lint_keywords(field: str, text: str, errors: list[str], warnings: list[str]) -> None:
    """Field-limit errors mirror what flows enforces at write time; the rest is advice."""
    if "\n" in text or "\r" in text:
        errors.append(f"{field}: line breaks are not allowed")
    if len(text) > flows.KEYWORDS_MAX_CHARS:
        errors.append(f"{field}: {len(text)} characters exceeds the {flows.KEYWORDS_MAX_CHARS}-character limit")
    terms = [t.strip() for t in text.split(",")]
    if "" in terms:
        warnings.append(f"{field}: empty keyword term (double or trailing comma)")
    counts: dict[str, int] = {}
    for term in terms:
        counts[term.casefold()] = counts.get(term.casefold(), 0) + 1
    for term, n in sorted(counts.items()):
        if term and n > 1:
            warnings.append(f"{field}: duplicate keyword {term!r} — duplicates waste the character budget")
    for raw, term in zip(text.split(","), terms):
        # Check the raw term: padding around a comma is a space Apple counts too.
        if term and re.search(r"\s", raw):
            warnings.append(f"{field}: keyword {term!r} contains a space — Apple counts spaces toward the limit")
    if len(text.encode("utf-8")) > flows.KEYWORDS_MAX_CHARS:
        warnings.append(
            f"{field}: {len(text.encode('utf-8'))} bytes — Apple's docs also cite a 100-byte limit; "
            "multi-byte keywords may be truncated"
        )


def validate_spec(spec: dict) -> tuple[list[str], list[str]]:
    """Offline spec lint. Returns (errors, warnings); errors stop a run."""
    errors: list[str] = []
    warnings: list[str] = []

    known_lower = {k.lower(): k for k in SPEC_KNOWN_KEYS}
    for key in spec:
        if key.startswith("_"):
            continue
        if key not in SPEC_KNOWN_KEYS:
            suggestion = (
                f" (did you mean {known_lower[key.lower()]!r}?)" if key.lower() in known_lower else ""
            )
            errors.append(f"unknown key {key!r}{suggestion}")
        elif key == "build":
            continue  # the dedicated check below has the better message
        elif not isinstance(spec[key], SPEC_KNOWN_KEYS[key]):
            errors.append(
                f"{key}: expected {SPEC_KNOWN_KEYS[key].__name__}, got {type(spec[key]).__name__}"
            )

    if not isinstance(spec.get("version"), str) or not spec.get("version"):
        errors.append('version: required, e.g. "0.7.0"')

    if spec.get("platform") is not None and spec["platform"] not in SPEC_PLATFORMS:
        errors.append(f"platform: {spec['platform']!r} is not one of {sorted(SPEC_PLATFORMS)}")
    if spec.get("releaseType") is not None and spec["releaseType"] not in flows.RELEASE_TYPES:
        errors.append(f"releaseType: {spec['releaseType']!r} is not one of {sorted(flows.RELEASE_TYPES)}")

    # Locale-keyed text fields: placeholders everywhere, per-field limits where
    # flows enforces them at write time.
    text_field_limits = {
        "keywords": None,  # audited separately
        "subtitles": flows.SUBTITLE_MAX_CHARS,
        "appNames": flows.APP_NAME_MAX_CHARS,
        "promotionalText": flows.PROMOTIONAL_TEXT_MAX_CHARS,
    }
    for key in ("whatsNew", "descriptions", *text_field_limits):
        mapping = spec.get(key)
        if not isinstance(mapping, dict):
            continue  # wrong-type error already recorded above
        for locale, text in mapping.items():
            field = f"{key}[{locale}]"
            if not isinstance(text, str):
                errors.append(f"{field}: expected text, got {type(text).__name__}")
                continue
            _lint_text(field, text, errors)
            if key == "keywords":
                _lint_keywords(field, text, errors, warnings)
            elif text_field_limits.get(key) is not None:
                if "\n" in text or "\r" in text:
                    errors.append(f"{field}: line breaks are not allowed")
                if len(text) > text_field_limits[key]:
                    errors.append(
                        f"{field}: {len(text)} characters exceeds the {text_field_limits[key]}-character limit"
                    )
    for key in ("supportUrls", "marketingUrls", "privacyUrls"):
        mapping = spec.get(key)
        if not isinstance(mapping, dict):
            continue
        for locale, url in mapping.items():
            if isinstance(url, str) and not url.startswith(("http://", "https://")):
                errors.append(f"{key}[{locale}]: must be an http(s) URL, got {url!r}")
    if isinstance(spec.get("reviewNotes"), str):
        _lint_text("reviewNotes", spec["reviewNotes"], errors)

    if isinstance(spec.get("releaseDate"), str):
        try:
            flows.parse_release_date(spec["releaseDate"])
        except SystemExit as err:
            errors.append(str(err))
    if spec.get("phasedRelease") and spec.get("releaseType") in {"MANUAL", "SCHEDULED"}:
        warnings.append(
            "phased release only takes effect when the version releases automatically "
            "(releaseType AFTER_APPROVAL, the default)"
        )

    build = spec.get("build")
    if build is not None and not isinstance(build, str):
        errors.append(
            f'build: expected a string like "8", got {type(build).__name__} — '
            "a number never matches ASC build numbers at attach time"
        )
    if spec.get("submit") and build is None:
        warnings.append("submit is set but build is not — App Review requires an attached build")

    if isinstance(spec.get("screenshots"), dict):
        for locale, files in spec["screenshots"].items():
            if not isinstance(files, list) or not files:
                errors.append(f"screenshots[{locale}]: expected a non-empty list of file paths")
                continue
            for f in files:
                if not isinstance(f, str):
                    errors.append(f"screenshots[{locale}]: file paths must be strings")
                elif not Path(f).is_file():
                    errors.append(f"screenshots[{locale}]: file not found: {f}")
    return errors, warnings


def spec_plan(spec: dict) -> list[str]:
    """Human-readable list of operations a `run` would perform."""
    plan = [f"create/resolve version {spec['version']}"]
    if spec.get("releaseDate"):
        plan.append(f"schedule release for {spec['releaseDate']}")
    if spec.get("phasedRelease") is not None:
        plan.append(f"phased release {'on' if spec['phasedRelease'] else 'off'}")
    if spec.get("build"):
        plan.append(f"attach build {spec['build']} (waiting for VALID state)")
    for label, key in (
        ("What's New", "whatsNew"),
        ("descriptions", "descriptions"),
        ("keywords", "keywords"),
        ("promotional text", "promotionalText"),
        ("support URLs", "supportUrls"),
        ("marketing URLs", "marketingUrls"),
    ):
        if spec.get(key):
            plan.append(f"set {label} for: {', '.join(sorted(spec[key]))}")
    if spec.get("copyright"):
        plan.append(f"set copyright: {spec['copyright']}")
    app_level = [
        f"{label} for: {', '.join(sorted(spec[key]))}"
        for label, key in (
            ("subtitles", "subtitles"),
            ("app names", "appNames"),
            ("privacy URLs", "privacyUrls"),
        )
        if spec.get(key)
    ]
    if app_level:
        plan.extend(f"set {line} (app-level)" for line in app_level)
    if spec.get("reviewNotes"):
        plan.append("update App Review notes")
    for locale, files in sorted((spec.get("screenshots") or {}).items()):
        mode = "replace + upload" if spec.get("screenshotsReplace") else "upload"
        plan.append(f"{mode} {len(files)} screenshot(s) for {locale}")
    if spec.get("submit"):
        plan.append("SUBMIT for review")
    return plan


def run_spec(
    client: Client,
    app_id: str,
    spec: dict,
    submit_flag: bool,
    assume_yes: bool,
    journal: RunJournal | NullJournal | None = None,
) -> str | None:
    """Execute the spec; returns "declined" when the submit confirmation got a no."""
    journal = journal or NullJournal()
    version_string = spec["version"]
    with journal.step(f"create/resolve version {version_string}"):
        version = flows.create_version(
            client, app_id, version_string, spec.get("releaseType", "AFTER_APPROVAL"), spec.get("platform", flows.MACOS_PLATFORM)
        )
        version_id = version["id"]
        journal.log(f"version {version_string} ready ({version_id}, {version['attributes'].get('appStoreState', '?')})")

    if spec.get("releaseDate"):
        with journal.step(f"schedule release for {spec['releaseDate']}"):
            flows.set_release_date(client, version_id, spec["releaseDate"])
            journal.log(f"release scheduled for {spec['releaseDate']}")

    if spec.get("phasedRelease") is not None:
        enabled = bool(spec["phasedRelease"])
        with journal.step(f"phased release {'on' if enabled else 'off'}"):
            (flows.enable_phased_release if enabled else flows.disable_phased_release)(client, version_id)
            journal.log("phased release updated")

    build = spec.get("build")
    if build:
        with journal.step(f"attach build {build}"):
            state = flows.find_build(client, app_id, build)
            if not state or state["attributes"].get("processingState") != "VALID":
                journal.log(f"waiting for build {build} to become VALID …")
                flows.wait_for_build(client, app_id, build, log=journal.log)
            flows.attach_build(client, version_id, app_id, build)
            journal.log(f"build {build} attached")

    version_locale_keys = ("whatsNew", "descriptions", "keywords", "promotionalText", "supportUrls", "marketingUrls")
    if any(spec.get(k) for k in version_locale_keys):
        locales = sorted(set().union(*(set(spec.get(k) or {}) for k in version_locale_keys)))
        with journal.step(f"update localizations ({', '.join(locales)})"):
            flows.set_localizations(
                client,
                version_id,
                whats_new=spec.get("whatsNew"),
                descriptions=spec.get("descriptions"),
                keywords=spec.get("keywords"),
                promotional_text=spec.get("promotionalText"),
                support_urls=spec.get("supportUrls"),
                marketing_urls=spec.get("marketingUrls"),
            )
            journal.log("localizations updated")

    # Subtitles, app names and privacy URLs are app-level and need the
    # just-created version's appInfo in an editable state — which
    # create_version above just ensured — so this comes after it, not before.
    app_level_keys = ("subtitles", "appNames", "privacyUrls")
    if any(spec.get(k) for k in app_level_keys):
        parts = [k for k in app_level_keys if spec.get(k)]
        with journal.step(f"set app-level metadata ({', '.join(parts)})"):
            flows.set_app_info_fields(
                client,
                app_id,
                subtitles=spec.get("subtitles"),
                names=spec.get("appNames"),
                privacy_urls=spec.get("privacyUrls"),
            )
            journal.log("app-level metadata updated")

    if spec.get("copyright"):
        with journal.step("set copyright"):
            flows.set_copyright(client, version_id, spec["copyright"])
            journal.log(f"copyright: {spec['copyright']}")

    if spec.get("reviewNotes"):
        with journal.step("update App Review notes"):
            flows.set_review_notes(client, version_id, spec["reviewNotes"])
            journal.log("review notes updated")

    display_type = spec.get("screenshotDisplayType", "APP_DESKTOP")
    for locale, files in (spec.get("screenshots") or {}).items():
        with journal.step(f"screenshots {locale} ({len(files)} file{'s' if len(files) != 1 else ''})"):
            flows.set_screenshots(
                client,
                version_id,
                locale,
                files,
                display_type=display_type,
                replace=bool(spec.get("screenshotsReplace")),
                log=journal.log,
            )
            journal.log(f"screenshots for {locale}: {len(files)} uploaded")

    if submit_flag or spec.get("submit"):
        with journal.step("submit for review") as entry:
            if not assume_yes and sys.stdin.isatty():
                answer = input(f"Submit version {version_string} for App Review? [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    journal.log("aborted — everything except the submission is done")
                    journal.cancel_step(entry)
                    return "declined"
            flows.submit_for_review(client, app_id, version_id)
            journal.log(f"version {version_string} submitted for review")
    return None


# ---------------------------------------------------------------------------
# subcommand handlers


def cmd_versions(client: Client, args) -> None:
    rows = flows.list_versions(client, args.app)
    if not rows:
        print("no App Store versions")
        return
    print(f"{'version':<12} {'state':<28} {'release':<16}")
    for v in rows:
        a = v["attributes"]
        print(f"{a['versionString']:<12} {a['appStoreState']:<28} {a.get('releaseType', ''):<16}")


def cmd_status(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    a = version["attributes"]
    print(f"version   {a['versionString']} ({version['id']})")
    print(f"state     {a['appStoreState']}")
    print(f"release   {a.get('releaseType', '')}")
    # The builds list is app-wide (there is no version→builds include); builds
    # are few in practice, so show them all and mark the one matching --build
    # expectations by number alone.
    for b in flows.list_builds(client, args.app):
        bi = b["attributes"]
        print(f"build     {bi.get('version')} ({bi.get('processingState')})")
    # Submission state: the legacy appStoreVersionSubmission endpoint answers
    # 404 when nothing is attached, and the modern reviewSubmissions flow is
    # what submit_for_review actually creates — check both.
    submitted = False
    try:
        submission = client.get(f"/v1/appStoreVersions/{version['id']}/appStoreVersionSubmission")
        if submission.get("data"):
            submitted = True
    except ApiError as err:
        if err.status != 404:
            raise
    if not submitted:
        sub = flows._open_review_submission(client, version["id"])
        if sub is not None:
            state = sub.get("attributes", {}).get("state", "?")
            print(f"submitted  yes (reviewSubmission: {state})")
            submitted = True
    if not submitted:
        print("submitted  no")


def cmd_create_version(client: Client, args) -> None:
    v = flows.create_version(client, args.app, args.version, args.release, args.platform)
    print(f"version {v['attributes']['versionString']} ready: {v['id']} ({v['attributes']['appStoreState']})")


def cmd_phased_release(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    if args.on:
        flows.enable_phased_release(client, version["id"])
        print(f"phased release on for {args.version}: the 7-day curve starts when the version releases")
        return
    if args.off:
        flows.disable_phased_release(client, version["id"])
        print(f"phased release off for {args.version}: everyone gets the update at once")
        return
    phased = flows.get_phased_release(client, version["id"])
    if phased is None:
        print(f"phased release: disabled (version {args.version} releases to everyone at once)")
        return
    a = phased.get("attributes", {})
    state = a.get("state", "?")
    if state == "ACTIVE":
        detail = f"day {a.get('currentDay', '?')}/{a.get('totalDays', '?')}"
    elif a.get("startDate"):
        detail = f"starts {a['startDate']}"
    else:
        detail = "starts when the version releases"
    print(f"phased release: enabled — {state} ({detail})")


def cmd_schedule_release(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_release_date(client, version["id"], args.at)
    print(f"version {args.version} scheduled for release at {args.at}")


def cmd_whatsnew(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    text = read_text(args)
    flows.set_localizations(client, version["id"], whats_new={args.locale: text})
    print(f"What's New for {args.locale} updated")


def cmd_description(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    text = read_text(args)
    flows.set_localizations(client, version["id"], descriptions={args.locale: text})
    print(f"description for {args.locale} updated")


def cmd_keywords(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    text = read_text(args)
    flows.set_localizations(client, version["id"], keywords={args.locale: text})
    print(f"keywords for {args.locale} updated")


def cmd_promotional_text(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_localizations(client, version["id"], promotional_text={args.locale: read_text(args)})
    print(f"promotional text for {args.locale} updated")


def cmd_support_url(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_localizations(client, version["id"], support_urls={args.locale: read_text(args)})
    print(f"support URL for {args.locale} updated")


def cmd_marketing_url(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_localizations(client, version["id"], marketing_urls={args.locale: read_text(args)})
    print(f"marketing URL for {args.locale} updated")


def cmd_subtitle(client: Client, args) -> None:
    text = read_text(args)
    flows.set_subtitles(client, args.app, {args.locale: text})
    print(f"subtitle for {args.locale} updated")


def cmd_app_name(client: Client, args) -> None:
    flows.set_app_info_fields(client, args.app, names={args.locale: read_text(args)})
    print(f"app name for {args.locale} updated")


def cmd_privacy_url(client: Client, args) -> None:
    flows.set_app_info_fields(client, args.app, privacy_urls={args.locale: read_text(args)})
    print(f"privacy policy URL for {args.locale} updated")


def cmd_copyright(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_copyright(client, version["id"], read_text(args))
    print("copyright updated")


def cmd_review_notes(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_review_notes(client, version["id"], read_text(args))
    print("App Review notes updated")


def cmd_screenshots(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.set_screenshots(
        client,
        version["id"],
        args.locale,
        args.files,
        display_type=args.display_type,
        replace=args.replace,
    )
    print(f"{len(args.files)} screenshot(s) uploaded for {args.locale}")


def cmd_attach_build(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    if args.wait:
        flows.wait_for_build(client, args.app, args.build)
    flows.attach_build(client, version["id"], args.app, args.build)
    print(f"build {args.build} attached to {args.version}")


def cmd_submit(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Submit {args.version} for App Review? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            raise SystemExit("aborted")
    app_id = flows.find_app(client, args.app)["id"]
    flows.submit_for_review(client, app_id, version["id"])
    print(f"version {args.version} submitted for review")


def cmd_cancel_submission(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.cancel_submission(client, version["id"])
    print("submission cancelled")


def _rejection_message(version: str, state: str) -> str:
    if state == "DEVELOPER_REJECTED":
        return f"version {version}: the submission was withdrawn by the developer (DEVELOPER_REJECTED)"
    if state == "METADATA_REJECTED":
        return f"version {version}: {state} — App Review found a metadata problem; see App Store Connect for what to fix"
    return (
        f"version {version}: {state} — see the rejection notes in App Store Connect "
        "(your app → the version → Review Information)"
    )


def notify_webhook(url: str, text: str, log=lambda m: print(m, flush=True)) -> bool:
    """POST one Slack-style {"text": …} message. Never raises: a failed
    notification must not mask the outcome it was reporting."""
    from .client import _curl

    try:
        status, payload = _curl(
            "POST",
            url,
            {"Content-Type": "application/json"},
            json.dumps({"text": text}).encode("utf-8"),
            15,
        )
    except (Exception, SystemExit) as err:
        log(f"warning: webhook notification failed: {err}")
        return False
    if status >= 300:
        log(f"warning: webhook answered HTTP {status}: {payload.decode(errors='replace')[:200]}")
        return False
    return True


def cmd_wait(client: Client, args) -> None:
    """Watch the version's review until it resolves; exit 0 on approval/release."""
    webhook = args.webhook or os.environ.get(ENV_WEBHOOK_URL) or None
    journal = RunJournal(
        command="wait",
        title=f"{args.app} → {args.version}: review watch",
        meta={"app": args.app, "version": args.version},
        runs_dir=getattr(args, "runs_dir", None),
    )
    journal.start()
    journal.log(
        f"run {journal.run_id} — follow live with 'asc-submit serve' "
        f"(http://127.0.0.1:8756) or 'asc-submit logs {journal.run_id} --follow'"
    )
    try:
        with journal.step(f"wait for review outcome ({args.version})"):
            version = flows.wait_for_review(
                client,
                args.app,
                args.version,
                timeout=args.timeout,
                poll=args.interval,
                log=journal.log,
            )
            state = version["attributes"].get("appStoreState", "?")
            if state in flows.REJECTED_STATES:
                raise SystemExit(_rejection_message(args.version, state))
        if state == "PENDING_DEVELOPER_RELEASE":
            message = (
                f"review approved — version {args.version} now waits for your manual "
                "release (releaseType MANUAL) in App Store Connect"
            )
        else:
            message = f"version {args.version} is READY_FOR_SALE — live on the App Store"
        journal.log(message)
        journal.succeed()
        if webhook:
            notify_webhook(webhook, f"asc-submit: {message}", log=journal.log)
    except KeyboardInterrupt:
        journal.cancel("interrupted (Ctrl-C)")
        raise
    except SystemExit as err:
        journal.fail(str(err) or type(err).__name__)
        if webhook:
            notify_webhook(
                webhook,
                f"asc-submit: review watch of {args.version} ended without approval: {err}",
                log=journal.log,
            )
        raise
    except BaseException as err:  # ApiError / anything else
        journal.fail(str(err) or type(err).__name__)
        raise


def cmd_validate(args) -> None:
    """Offline spec lint — no API key, nothing leaves the machine."""
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise SystemExit(f"{args.spec}: cannot read spec: {err}")
    if not isinstance(spec, dict):
        raise SystemExit(f"{args.spec}: the spec must be a JSON object")
    errors, warnings = validate_spec(spec)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if not errors and not warnings:
        print(f"{args.spec}: no issues found")
        return
    if errors:
        print(f"{args.spec}: {len(errors)} error(s), {len(warnings)} warning(s)", file=sys.stderr)
        sys.exit(1)
    print(f"{args.spec}: {len(warnings)} warning(s)")


def cmd_run(args) -> None:
    spec = load_spec(args.spec)
    # Lint before anything else — a bad spec fails here, without an API key,
    # so PR-time checks catch it before any secret is even configured.
    errors, warnings = validate_spec(spec)
    for w in warnings:
        print(f"warning: {w}")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        raise SystemExit(f"{args.spec}: {len(errors)} error(s) — the spec was not executed")
    if args.dry_run:
        # Plan printing touches nothing; no API key needed (see main()).
        print(f"plan for app {args.app}:")
        for line in spec_plan(spec):
            print(f"  - {line}")
        return
    client = build_client(args)
    journal = RunJournal(
        command="run",
        title=f"{args.app} → {spec['version']}",
        meta={
            "app": args.app,
            "version": spec["version"],
            "build": spec.get("build"),
            "spec": Path(args.spec).name,
            "platform": spec.get("platform", flows.MACOS_PLATFORM),
        },
        runs_dir=getattr(args, "runs_dir", None),
    )
    journal.start()
    journal.log(
        f"run {journal.run_id} — follow live with 'asc-submit serve' "
        f"(http://127.0.0.1:8756) or 'asc-submit logs {journal.run_id} --follow'"
    )
    try:
        outcome = run_spec(client, args.app, spec, submit_flag=args.submit, assume_yes=args.yes, journal=journal)
    except KeyboardInterrupt:
        journal.cancel("interrupted (Ctrl-C)")
        raise
    except BaseException as err:  # SystemExit / ApiError / anything else
        journal.fail(str(err) or type(err).__name__)
        raise
    if outcome == "declined":
        # everything shipped except the submission itself, by explicit choice
        journal.cancel("submission declined — everything except the submission is done")
    else:
        journal.succeed()


def cmd_doctor(client: Client, args) -> None:
    """Check this key can actually ship a version — no side effects."""
    checks: list[tuple[str, bool, str]] = []

    # 1. Read access (Developer keys have this).
    try:
        versions = flows.list_versions(client, args.app)
        checks.append(("read: app versions reachable", True, f"{len(versions)} version(s)"))
    except ApiError as err:
        checks.append(("read: app versions reachable", False, str(err)))

    # 2. Write probe against a nonexistent localization id. With write access
    #    Apple answers 404 (resource not found); without it, 403 comes first.
    probe_id = "00000000-0000-0000-0000-000000000000"
    try:
        client.patch(
            f"/v1/appStoreVersionLocalizations/{probe_id}",
            {"data": {"type": "appStoreVersionLocalizations", "id": probe_id, "attributes": {}}},
        )
        checks.append(("write: version metadata", True, "unexpectedly patched a nonexistent id"))
    except ApiError as err:
        if err.status == 403:
            checks.append(("write: version metadata", False, "HTTP 403 — key role cannot edit metadata"))
        elif err.status == 404:
            checks.append(("write: version metadata", True, "HTTP 404 on a probe id (resource absent, permission granted)"))
        else:
            checks.append(("write: version metadata", True, f"HTTP {err.status} (not a permission error)"))

    # 3. Write probe for app-level metadata (name/subtitle live on
    #    appInfoLocalizations, a separate resource from version localizations).
    probe_localization = "00000000-0000-0000-0000-000000000001"
    try:
        client.patch(
            f"/v1/appInfoLocalizations/{probe_localization}",
            {"data": {"type": "appInfoLocalizations", "id": probe_localization, "attributes": {}}},
        )
        checks.append(("write: app-level metadata (subtitle)", True, "unexpectedly patched a nonexistent id"))
    except ApiError as err:
        if err.status == 403:
            checks.append(("write: app-level metadata (subtitle)", False, "HTTP 403 — key role cannot edit app info"))
        elif err.status == 404:
            checks.append(("write: app-level metadata (subtitle)", True, "HTTP 404 on a probe id (resource absent, permission granted)"))
        else:
            checks.append(("write: app-level metadata (subtitle)", True, f"HTTP {err.status} (not a permission error)"))

    # 4. Submission uses the reviewSubmissions flow, gated by the same role as
    #    metadata writes — there is no safer separate probe (a real POST would
    #    submit), so mirror the metadata verdict.
    write_ok = checks[1][1]
    checks.append(
        ("submit-capable (same role gate as metadata writes)", write_ok, "reviewSubmissions is probed live at submit time")
    )

    print(f"asc-submit doctor for app {args.app}")
    all_ok = True
    for name, ok, detail in checks:
        all_ok = all_ok and ok
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {detail}")
    if all_ok:
        print("result: this key can create versions, edit metadata and submit for review")
    else:
        print("result: submissions NOT possible with this key — " + Client.forbidden_hint())
    sys.exit(0 if all_ok else 1)


def cmd_upload(args) -> None:
    """Archive the Xcode project and upload the build to ASC (no API key —
    authentication goes through the Apple ID session used by xcodebuild).

    Receives no client: unlike the ASC API commands, this does not require
    an API key, so main() skips build_client() for it.
    """
    from pathlib import Path

    from . import xcode

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    if not args.from_archive:
        if not args.project and not args.workspace:
            raise SystemExit("--project or --workspace is required (or --from-archive)")
        if args.project and args.workspace:
            raise SystemExit("--project and --workspace are mutually exclusive")

    journal = RunJournal(
        command="upload",
        title=f"upload {args.scheme} {args.version} ({args.build})",
        meta={
            "scheme": args.scheme,
            "version": args.version,
            "build": args.build,
            "platform": args.platform,
            "project": args.project or args.workspace,
        },
        runs_dir=getattr(args, "runs_dir", None),
    )
    journal.start()
    journal.log(
        f"run {journal.run_id} — follow live with 'asc-submit serve' "
        f"(http://127.0.0.1:8756) or 'asc-submit logs {journal.run_id} --follow'"
    )
    try:
        _upload_workflow(args, work_dir, journal)
    except KeyboardInterrupt:
        journal.cancel("interrupted (Ctrl-C)")
        raise
    except BaseException as err:
        journal.fail(str(err) or type(err).__name__)
        raise
    journal.succeed()


def _upload_workflow(args, work_dir: Path, journal: RunJournal | NullJournal) -> None:
    """The upload phases, recorded step by step on ``journal``."""
    from . import xcode

    if args.from_archive:
        with journal.step(f"locate archive ({args.from_archive})"):
            archive_path = Path(args.from_archive).resolve()
            if not archive_path.is_dir():
                raise SystemExit(f"archive not found: {archive_path}")
            journal.log(f"using existing archive: {archive_path}")
    else:
        with journal.step(f"archive {args.scheme} ({args.configuration}, {args.platform})"):
            archive_path = work_dir / f"{args.scheme}.xcarchive"
            cmd = xcode.build_archive_command(
                scheme=args.scheme,
                configuration=args.configuration,
                platform=args.platform,
                version=args.version,
                build=args.build,
                derived_data=work_dir / "derived",
                archive_path=archive_path,
                project=args.project,
                workspace=args.workspace,
                team_id=args.team_id,
            )
            journal.log(f"xcodebuild archive → {archive_path}")
            xcode.run_xcodebuild(cmd, quiet=not args.verbose, log=journal.log)

    with journal.step("verify archived bundle"):
        app = xcode.find_app_in_archive(archive_path)
        if not args.skip_version_check:
            short, bundle_build = xcode.read_bundle_versions(app)
            mismatches = []
            if short != args.version:
                mismatches.append(f"CFBundleShortVersionString={short} (expected {args.version})")
            if bundle_build != args.build:
                mismatches.append(f"CFBundleVersion={bundle_build} (expected {args.build})")
            if mismatches:
                raise SystemExit(
                    "archived bundle version mismatch: " + "; ".join(mismatches)
                    + ". The project probably hard-codes its Info.plist values instead of "
                    "using MARKETING_VERSION/CURRENT_PROJECT_VERSION build settings."
                )
            journal.log(f"archived bundle verified: {app.name} {short} ({bundle_build})")
        else:
            journal.log("version check skipped (--skip-version-check)")

    if args.archive_only:
        journal.log(f"archive-only: {archive_path}")
        return

    with journal.step("upload to App Store Connect"):
        options_plist = work_dir / "exportOptions.plist"
        xcode.write_export_options(options_plist, args.team_id)
        journal.log("exportArchive (destination=upload) started")
        xcode.run_xcodebuild(
            xcode.build_export_command(archive_path, work_dir / "export", options_plist),
            quiet=not args.verbose,
            log=journal.log,
        )
        journal.log("upload finished")
    journal.log(
        "Wait for the build to reach VALID in App Store Connect "
        "(asc-submit run will wait for you), then submit."
    )


def read_text(args) -> str:
    if args.text is not None and args.file is not None:
        raise SystemExit("--text and --file are mutually exclusive")
    if args.text is not None:
        return args.text
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    raise SystemExit("provide --text or --file")


# ---------------------------------------------------------------------------
# workflow history (runs / logs / serve) — journal readers, no API key


STEP_ICONS = {"success": "✓", "failed": "✕", "running": "●", "cancelled": "⊘"}


def _fmt_time(iso: str | None) -> str:
    """Journal timestamps carry microsecond sort precision; display without it."""
    if not iso:
        return ""
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def cmd_serve(args) -> None:
    """Serve the read-only browser dashboard over the recorded runs."""
    from . import server

    server.serve(
        runs_dir=getattr(args, "runs_dir", None),
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )


def cmd_runs(args) -> None:
    root = resolve_runs_dir(getattr(args, "runs_dir", None))
    if args.id:
        state = read_run(args.id, root)
        if state is None:
            raise SystemExit(f"no run '{args.id}' under {root}")
        print(f"{state['title']}  [{state['id']}]")
        print(f"command   {state['command']}")
        print(f"status    {state['status']}")
        print(f"started   {_fmt_time(state['started_at'])}")
        if state.get("finished_at"):
            print(f"finished  {_fmt_time(state['finished_at'])}")
        if state.get("error"):
            print(f"error     {state['error']}")
        if not state["steps"]:
            print("no steps recorded")
            return
        for step in state["steps"]:
            icon = STEP_ICONS.get(step["status"], "○")
            suffix = f" — {step['error']}" if step.get("error") else ""
            print(f"  [{icon}] {step['name']}{suffix}")
        print(f"logs: asc-submit logs {state['id']}")
        return
    runs = list_runs(root)
    if not runs:
        print(f"no workflow runs recorded under {root}")
        print("runs are recorded by 'asc-submit run', 'upload' and 'wait'")
        return
    print(f"{'run id':<22} {'status':<10} {'command':<8} title")
    for run in runs:
        print(f"{run['id']:<22} {run['status']:<10} {run['command']:<8} {run.get('title', '')}")
    print(f"\n{len(runs)} run(s) · detail: asc-submit runs <id> · logs: asc-submit logs <id>")


def cmd_logs(args) -> None:
    root = resolve_runs_dir(getattr(args, "runs_dir", None))
    state = read_run(args.id, root)
    if state is None:
        raise SystemExit(f"no run '{args.id}' under {root}")
    text, offset, _size = read_log(args.id, root)
    sys.stdout.write(text)
    sys.stdout.flush()
    if not args.follow:
        return
    import time

    # tail -f: stream new characters until the run finishes and the log is drained
    while True:
        finished = state.get("finished_at") is not None
        text, offset, size = read_log(args.id, root, start=offset)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
        if finished and offset >= size:
            return
        time.sleep(args.interval)
        state = read_run(args.id, root) or state


# ---------------------------------------------------------------------------
# parser assembly


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asc-submit",
        description="Ship an App Store version end to end via the App Store Connect API.",
    )
    parser.add_argument("--version", action="version", version=__import__("asc_submit").__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        add_auth_arguments(p)
        return p

    def add_app_and_version(p: argparse.ArgumentParser) -> None:
        p.add_argument("app", help="Apple ID of the app, or its bundle ID")
        p.add_argument("--version", required=True, help="version string, e.g. 0.7.0")

    p = add("versions", "list App Store versions and their states")
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.set_defaults(func=cmd_versions)

    p = add("status", "show one version's state, build and submission")
    add_app_and_version(p)
    p.set_defaults(func=cmd_status)

    p = add("create-version", "create the version if it does not exist yet")
    p.add_argument("app")
    p.add_argument("--version", required=True)
    p.add_argument("--release", default="AFTER_APPROVAL", help="AFTER_APPROVAL (default), MANUAL or SCHEDULED")
    p.add_argument("--platform", default=flows.MACOS_PLATFORM, help="MAC_OS (default), IOS, TV_OS, VISION_OS")
    p.set_defaults(func=cmd_create_version)

    p = add("phased-release", "show/enable/disable the 7-day phased release curve")
    add_app_and_version(p)
    p.add_argument("--on", action="store_true", help="enable phased release")
    p.add_argument("--off", action="store_true", help="disable phased release (release to everyone at once)")
    p.set_defaults(func=cmd_phased_release)

    p = add("schedule-release", "set releaseType SCHEDULED with a fixed release date and time")
    add_app_and_version(p)
    p.add_argument("--at", required=True, help="ISO 8601 with UTC offset, e.g. 2026-10-01T09:00:00+09:00")
    p.set_defaults(func=cmd_schedule_release)

    p = add("whatsnew", "set the What's New text for one locale")
    add_app_and_version(p)
    p.add_argument("--locale", required=True, help="e.g. ja, en-US, zh-Hans, ko, es-ES")
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_whatsnew)

    p = add("description", "set the description for one locale")
    add_app_and_version(p)
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_description)

    p = add("keywords", "set the keywords for one locale (comma-separated, single line)")
    add_app_and_version(p)
    p.add_argument("--locale", required=True, help="e.g. ja, en-US, zh-Hans, ko, es-ES")
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_keywords)

    p = add("promotional-text", "set the promotional text for one locale (above the description, 170 chars)")
    add_app_and_version(p)
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_promotional_text)

    p = add("support-url", "set the support URL for one locale (version-level)")
    add_app_and_version(p)
    p.add_argument("--locale", required=True)
    p.add_argument("--text", help="the URL")
    p.add_argument("--file")
    p.set_defaults(func=cmd_support_url)

    p = add("marketing-url", "set the marketing URL for one locale (version-level)")
    add_app_and_version(p)
    p.add_argument("--locale", required=True)
    p.add_argument("--text", help="the URL")
    p.add_argument("--file")
    p.set_defaults(func=cmd_marketing_url)

    p = add(
        "subtitle",
        "set the app subtitle for one locale (app-level: needs a version in an editable state)",
    )
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_subtitle)

    p = add(
        "app-name",
        "set the app name for one locale (app-level: needs a version in an editable state)",
    )
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_app_name)

    p = add(
        "privacy-url",
        "set the privacy policy URL for one locale (app-level: needs a version in an editable state)",
    )
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--locale", required=True)
    p.add_argument("--text", help="the URL")
    p.add_argument("--file")
    p.set_defaults(func=cmd_privacy_url)

    p = add("copyright", "set the version's copyright line (not localized)")
    add_app_and_version(p)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_copyright)

    p = add("review-notes", "replace the App Review Information notes")
    add_app_and_version(p)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_review_notes)

    p = add("screenshots", "upload screenshots for one locale (upload order = display order)")
    add_app_and_version(p)
    p.add_argument("--locale", required=True)
    p.add_argument("--display-type", default="APP_DESKTOP", help="macOS default: APP_DESKTOP")
    p.add_argument("--replace", action="store_true", help="delete the current set first")
    p.add_argument("files", nargs="+", help="png/jpg paths, in display order")
    p.set_defaults(func=cmd_screenshots)

    p = add("attach-build", "attach an uploaded build to the version")
    add_app_and_version(p)
    p.add_argument("--build", required=True, help="build number, e.g. 7")
    p.add_argument("--wait", action="store_true", help="wait for the build to reach VALID first")
    p.add_argument("--wait-timeout", type=int, default=1800)
    p.set_defaults(func=cmd_attach_build)

    p = add("submit", "submit the version for App Review")
    add_app_and_version(p)
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_submit)

    p = add("cancel-submission", "remove the version from review")
    add_app_and_version(p)
    p.set_defaults(func=cmd_cancel_submission)

    p = add(
        "wait",
        "watch the version's review until it resolves (approval, release or rejection)",
    )
    add_app_and_version(p)
    p.add_argument(
        "--timeout",
        type=int,
        default=432000,
        help="give up after this many seconds (default 432000 = 5 days; 0 = wait forever)",
    )
    p.add_argument("--interval", type=int, default=300, help="poll interval in seconds (default 300)")
    p.add_argument(
        "--webhook",
        help=f"POST the outcome to this URL as {{\"text\": …}} (env: {ENV_WEBHOOK_URL})",
    )
    p.add_argument("--runs-dir", help=f"where to record this run (env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR})")
    p.set_defaults(func=cmd_wait)

    p = add("doctor", "check whether this API key can ship a version (no side effects)")
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.set_defaults(func=cmd_doctor)

    # upload talks to xcodebuild, not the ASC API, so no auth arguments.
    p = sub.add_parser("upload", help="archive the Xcode project and upload the build (no API key)")
    p.add_argument("--project", help="path to .xcodeproj")
    p.add_argument("--workspace", help="path to .xcworkspace (mutually exclusive with --project)")
    p.add_argument("--scheme", required=True)
    p.add_argument("--version", required=True, help="marketing version, e.g. 0.7.0")
    p.add_argument("--build", required=True, help="build number, e.g. 9")
    p.add_argument("--configuration", default="Release")
    p.add_argument("--platform", default="macOS", choices=["macOS", "iOS"])
    p.add_argument("--team-id", help="development team for automatic signing")
    p.add_argument("--work-dir", default="build/asc-submit", help="scratch dir for derived data, archive and exportOptions")
    p.add_argument("--archive-only", action="store_true", help="stop after archiving (no upload)")
    p.add_argument("--from-archive", help="skip archiving; upload an existing .xcarchive")
    p.add_argument("--skip-version-check", action="store_true", help="do not compare the archived bundle version")
    p.add_argument("--runs-dir", help=f"where to record this run (env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR})")
    p.add_argument("-v", "--verbose", action="store_true", help="print full xcodebuild output")
    p.set_defaults(func=cmd_upload)

    # validate lints a spec file offline; like the journal readers it needs no
    # API key, so no auth arguments.
    p = sub.add_parser("validate", help="lint a release spec — placeholders, limits, key typos (no API key)")
    p.add_argument("--spec", required=True, help="path to the JSON spec")
    p.set_defaults(func=cmd_validate)

    p = add("run", "run a whole shipping plan from a JSON spec")
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--spec", required=True, help="path to the JSON spec (see examples/spec.example.json)")
    p.add_argument("--submit", action="store_true", help="submit for review at the end")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="print the plan without touching anything")
    p.add_argument("--runs-dir", help=f"where to record this run (env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR})")
    p.set_defaults(func=cmd_run)

    # serve / runs / logs read the run journal — no API key, no side effects.
    p = sub.add_parser("serve", help="live browser view of workflow runs (read-only)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8756, help="port (default 8756)")
    p.add_argument("--runs-dir", help=f"runs dir to watch (env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR})")
    p.add_argument("--open", action="store_true", help="open the dashboard in your default browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("runs", help="list recorded workflow runs, or show one run's steps")
    p.add_argument("id", nargs="?", help="run id (from the list, or the run's start banner)")
    p.add_argument("--runs-dir", help=f"env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR}")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("logs", help="print a run's recorded log (like tail -f with --follow)")
    p.add_argument("id", help="run id")
    p.add_argument("--follow", "-f", action="store_true", help="keep streaming until the run finishes")
    p.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1.0)")
    p.add_argument("--runs-dir", help=f"env: {RUNS_DIR_ENV}; default {DEFAULT_RUNS_DIR}")
    p.set_defaults(func=cmd_logs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"upload", "serve", "runs", "logs", "validate"}:
            # xcodebuild path and journal readers: no API key required.
            args.func(args)
            return 0
        if args.command == "run":
            # cmd_run lints the spec keylessly, then either prints the --dry-run
            # plan or builds the client itself — so a bad spec fails before any
            # secret is needed (CI can validate specs on every PR).
            args.func(args)
            return 0
        client = build_client(args)
        args.func(client, args)
    except ApiError as err:
        print(f"error: {err}", file=sys.stderr)
        if err.forbidden:
            print(Client.forbidden_hint(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
