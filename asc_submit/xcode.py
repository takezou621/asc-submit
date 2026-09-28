"""Generic xcodebuild wrapper: archive an Xcode project and upload to ASC.

These commands intentionally shell out to ``xcodebuild`` — the App Store
Connect API has no build-upload endpoint, so shipping a build always goes
through Xcode's toolchain (which also handles signing, provisioning and the
Apple ID session). Keeping the wrapper here means external projects get the
same one-command shipping flow without a bespoke archive script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class XcodeError(RuntimeError):
    """xcodebuild failed (compile, signing, upload, …)."""


def build_archive_command(
    scheme: str,
    configuration: str,
    platform: str,
    version: str,
    build: str,
    derived_data: Path,
    archive_path: Path,
    project: str | None = None,
    workspace: str | None = None,
    team_id: str | None = None,
    code_sign_identity: str = "Apple Development",
) -> list[str]:
    """Assemble the ``xcodebuild archive`` argument list.

    ``--version`` / ``--build`` are passed as build settings
    (MARKETING_VERSION / CURRENT_PROJECT_VERSION), which works for any
    project whose Info.plist references those variables (the Xcode default).

    ``code_sign_identity`` is always passed even with automatic signing:
    projects that ship a manual ``CODE_SIGN_IDENTITY`` (e.g. a local dev
    certificate) otherwise fail with "conflicting provisioning settings".
    """
    cmd = ["xcodebuild", "archive"]
    if workspace:
        cmd += ["-workspace", workspace]
    else:
        cmd += ["-project", project or ""]
    cmd += [
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", f"generic/platform={platform}",
        "-derivedDataPath", str(derived_data),
        "-archivePath", str(archive_path),
        f"MARKETING_VERSION={version}",
        f"CURRENT_PROJECT_VERSION={build}",
        "CODE_SIGN_STYLE=Automatic",
        f"CODE_SIGN_IDENTITY={code_sign_identity}",
        "-allowProvisioningUpdates",
    ]
    if team_id:
        cmd += [f"DEVELOPMENT_TEAM={team_id}"]
    return cmd


def build_export_command(
    archive_path: Path,
    export_path: Path,
    options_plist: Path,
) -> list[str]:
    return [
        "xcodebuild",
        "-exportArchive",
        "-archivePath", str(archive_path),
        "-exportPath", str(export_path),
        "-exportOptionsPlist", str(options_plist),
        "-allowProvisioningUpdates",
    ]


EXPORT_OPTIONS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>app-store-connect</string>
    <key>destination</key>
    <string>upload</string>
    <key>signingStyle</key>
    <string>automatic</string>
{team_id_block}</dict>
</plist>
"""


def write_export_options(path: Path, team_id: str | None = None) -> None:
    """Write exportOptions.plist for an app-store-connect upload.

    Archiving is signed with Apple Development; exportArchive re-signs with
    the distribution certificate and provisioning profile, auto-created by
    -allowProvisioningUpdates.
    """
    team_block = ""
    if team_id:
        team_block = f"    <key>teamID</key>\n    <string>{team_id}</string>\n"
    path.write_text(EXPORT_OPTIONS_TEMPLATE.format(team_id_block=team_block), encoding="utf-8")


def find_app_in_archive(archive_path: Path) -> Path:
    """Locate the first .app bundle inside an .xcarchive."""
    products = archive_path / "Products" / "Applications"
    if not products.is_dir():
        raise XcodeError(f"archive has no Products/Applications: {archive_path}")
    apps = sorted(products.glob("*.app"))
    if not apps:
        raise XcodeError(f"no .app bundle in {products}")
    return apps[0]


def read_bundle_versions(app_path: Path) -> tuple[str, str]:
    """Read (CFBundleShortVersionString, CFBundleVersion) with plistlib.

    macOS bundles keep Info.plist under ``Contents/``; iOS bundles are flat
    (``App.app/Info.plist``), so try both. Not plutil, which resolves
    .app-internal paths through Foundation's bundle lookup and can fail
    spuriously.
    """
    import plistlib

    candidates = [app_path / "Contents" / "Info.plist", app_path / "Info.plist"]
    plist = next((c for c in candidates if c.is_file()), candidates[0])
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException) as err:
        raise XcodeError(f"cannot read Info.plist from {plist}: {err}") from None
    short = data.get("CFBundleShortVersionString")
    build = data.get("CFBundleVersion")
    if short is None or build is None:
        raise XcodeError(f"{plist} is missing CFBundleShortVersionString/CFBundleVersion")
    return str(short), str(build)


def run_xcodebuild(cmd: list[str], quiet: bool = False, log=print) -> None:
    """Run xcodebuild and hand its output to ``log``; raise on failure.

    ``log`` defaults to plain ``print`` so interactive use is unchanged; the
    upload workflow passes its journal so the tail lands in the run record.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise XcodeError(
            f"{' '.join(cmd[:2])} failed (exit {proc.returncode}):\n"
            f"{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
        )
    if not quiet:
        log(proc.stdout[-2000:])
