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
import sys
from pathlib import Path

from . import auth, flows
from .client import ApiError, Client

ENV_KEY_PATH = "ASC_KEY_PATH"
ENV_KEY_ID = "ASC_KEY_ID"
ENV_ISSUER = "ASC_ISSUER"


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


def spec_plan(spec: dict) -> list[str]:
    """Human-readable list of operations a `run` would perform."""
    plan = [f"create/resolve version {spec['version']}"]
    if spec.get("build"):
        plan.append(f"attach build {spec['build']} (waiting for VALID state)")
    if spec.get("whatsNew"):
        plan.append(f"set What's New for: {', '.join(sorted(spec['whatsNew']))}")
    if spec.get("descriptions"):
        plan.append(f"set descriptions for: {', '.join(sorted(spec['descriptions']))}")
    if spec.get("keywords"):
        plan.append(f"set keywords for: {', '.join(sorted(spec['keywords']))}")
    if spec.get("subtitles"):
        plan.append(f"set subtitles for: {', '.join(sorted(spec['subtitles']))} (app-level)")
    if spec.get("reviewNotes"):
        plan.append("update App Review notes")
    for locale, files in sorted((spec.get("screenshots") or {}).items()):
        mode = "replace + upload" if spec.get("screenshotsReplace") else "upload"
        plan.append(f"{mode} {len(files)} screenshot(s) for {locale}")
    if spec.get("submit"):
        plan.append("SUBMIT for review")
    return plan


def run_spec(client: Client, app_id: str, spec: dict, submit_flag: bool, assume_yes: bool) -> None:
    version_string = spec["version"]
    version = flows.create_version(
        client, app_id, version_string, spec.get("releaseType", "AFTER_APPROVAL"), spec.get("platform", flows.MACOS_PLATFORM)
    )
    version_id = version["id"]

    build = spec.get("build")
    if build:
        state = flows.find_build(client, app_id, build)
        if not state or state["attributes"].get("processingState") != "VALID":
            print(f"waiting for build {build} to become VALID …")
            flows.wait_for_build(client, app_id, build)
        flows.attach_build(client, version_id, app_id, build)
        print(f"build {build} attached")

    flows.set_localizations(client, version_id, spec.get("whatsNew"), spec.get("descriptions"), spec.get("keywords"))
    if spec.get("whatsNew") or spec.get("descriptions") or spec.get("keywords"):
        print("localizations updated")

    # Subtitles are app-level and need the just-created version's appInfo in an
    # editable state — which create_version above just ensured — so this comes
    # after it, not before.
    if spec.get("subtitles"):
        flows.set_subtitles(client, app_id, spec["subtitles"])
        print("subtitles updated")

    if spec.get("reviewNotes"):
        flows.set_review_notes(client, version_id, spec["reviewNotes"])
        print("review notes updated")

    display_type = spec.get("screenshotDisplayType", "APP_DESKTOP")
    for locale, files in (spec.get("screenshots") or {}).items():
        flows.set_screenshots(
            client,
            version_id,
            locale,
            files,
            display_type=display_type,
            replace=bool(spec.get("screenshotsReplace")),
        )
        print(f"screenshots for {locale}: {len(files)} uploaded")

    if submit_flag or spec.get("submit"):
        if not assume_yes and sys.stdin.isatty():
            answer = input(f"Submit version {version_string} for App Review? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                print("aborted — everything except the submission is done")
                return
        flows.submit_for_review(client, version_id)
        print(f"version {version_string} submitted for review")


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
    submission = client.get(f"/v1/appStoreVersions/{version['id']}/appStoreVersionSubmission")
    if submission.get("data"):
        print("submitted  yes")


def cmd_create_version(client: Client, args) -> None:
    v = flows.create_version(client, args.app, args.version, args.release, args.platform)
    print(f"version {v['attributes']['versionString']} ready: {v['id']} ({v['attributes']['appStoreState']})")


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


def cmd_subtitle(client: Client, args) -> None:
    text = read_text(args)
    flows.set_subtitles(client, args.app, {args.locale: text})
    print(f"subtitle for {args.locale} updated")


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
    flows.submit_for_review(client, version["id"])
    print(f"version {args.version} submitted for review")


def cmd_cancel_submission(client: Client, args) -> None:
    version = flows.require_version(client, args.app, args.version)
    flows.cancel_submission(client, version["id"])
    print("submission cancelled")


def cmd_run(client: Client, args) -> None:
    spec = load_spec(args.spec)
    if args.dry_run:
        print(f"plan for app {args.app}:")
        for line in spec_plan(spec):
            print(f"  - {line}")
        return
    run_spec(client, args.app, spec, submit_flag=args.submit, assume_yes=args.yes)


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

    # 4. Submission uses POST /v1/appStoreVersionSubmissions, gated by the same
    #    role as metadata writes — there is no safer separate probe (a real
    #    POST would submit), so mirror the metadata verdict.
    write_ok = checks[1][1]
    checks.append(
        ("submit-capable (same role gate as metadata writes)", write_ok, "POST appStoreVersionSubmissions is probed live at submit time")
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

    if args.from_archive:
        archive_path = Path(args.from_archive).resolve()
        if not archive_path.is_dir():
            raise SystemExit(f"archive not found: {archive_path}")
    else:
        if not args.project and not args.workspace:
            raise SystemExit("--project or --workspace is required (or --from-archive)")
        if args.project and args.workspace:
            raise SystemExit("--project and --workspace are mutually exclusive")
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
        print("archiving …")
        xcode.run_xcodebuild(cmd, quiet=not args.verbose)

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
        print(f"archived bundle verified: {app.name} {short} ({bundle_build})")

    if args.archive_only:
        print(f"archive-only: {archive_path}")
        return

    options_plist = work_dir / "exportOptions.plist"
    xcode.write_export_options(options_plist, args.team_id)
    print("uploading to App Store Connect …")
    xcode.run_xcodebuild(
        xcode.build_export_command(archive_path, work_dir / "export", options_plist),
        quiet=not args.verbose,
    )
    print(
        "upload finished. Wait for the build to reach VALID in App Store Connect "
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
    p.add_argument("--platform", default=flows.MACOS_PLATFORM, help="OS_X (default), IOS, TV_OS")
    p.set_defaults(func=cmd_create_version)

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
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_keywords)

    p = add(
        "subtitle",
        "set the app subtitle for one locale (app-level: needs a version in an editable state)",
    )
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--locale", required=True)
    p.add_argument("--text")
    p.add_argument("--file")
    p.set_defaults(func=cmd_subtitle)

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
    p.add_argument("-v", "--verbose", action="store_true", help="print full xcodebuild output")
    p.set_defaults(func=cmd_upload)

    p = add("run", "run a whole shipping plan from a JSON spec")
    p.add_argument("app", help="Apple ID of the app, or its bundle ID")
    p.add_argument("--spec", required=True, help="path to the JSON spec (see examples/spec.example.json)")
    p.add_argument("--submit", action="store_true", help="submit for review at the end")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="print the plan without touching anything")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "upload":
            # xcodebuild path: no API key required.
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
