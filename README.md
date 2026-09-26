# asc-submit

Ship an App Store version end to end from the command line: create the version,
set What's New / descriptions / App Review notes / screenshots, attach a build,
and submit for review — all through the official
[App Store Connect API](https://developer.apple.com/documentation/appstoreconnectapi),
with **zero Python dependencies** (standard library plus the `openssl` and
`curl` binaries that macOS and virtually every Linux distribution already
have).

Built for release automation where a web UI or a heavy framework (fastlane) is
more ceremony than value: reproducible submissions from CI, version-controlled
metadata, and no browser clicking on every release.

## Why

Apple's API supports the entire submission flow, but the required role is
App Manager or higher and the resource graph is nontrivial (version →
localizations → screenshot sets → screenshots → submission). This tool wraps
that graph in idempotent, scriptable commands:

- **Idempotent** — re-running after a partial failure updates instead of duplicating
- **Dry-run first** — `run --dry-run` prints the plan; `submit` asks for confirmation
- **Metadata as code** — What's New / descriptions / review notes live in your repo,
  not in App Store Connect
- **CI-friendly** — one `.p8` secret, no Ruby, no browser session

HTTP goes through `curl` on purpose: Apple's edge intermittently stalls
`urllib`-style HTTP/1.1 requests on some resource paths (reproduced with
identical URLs returning instantly under curl), and curl's HTTP/2 negotiation
avoids that class of problem entirely.

## Requirements

- Python 3.9+, `openssl` (JWT signing) and `curl` (HTTP) on PATH — all three
  ship with macOS and nearly every Linux distribution. Alternatively, the
  optional `cryptography` package replaces the `openssl` call.
- An **App Store Connect API key with the App Manager role (or Admin)**.
  Keys with the Developer role can read but **cannot** create versions, edit
  metadata or submit — the role is fixed at key creation time, so issue a new
  key at *Users and Access → Integrations* if needed.

```sh
export ASC_KEY_PATH=~/.appstoreconnect/AuthKey_XYZ.p8
export ASC_KEY_ID=XYZ
export ASC_ISSUER=00000000-0000-0000-0000-000000000000
```

Install from a checkout (nothing is pulled from PyPI):

```sh
pip install .
# or just run it in place:
python3 -m asc_submit --help
```

## Verifying your key

```sh
asc-submit doctor 6812783176
```

`doctor` reads the app's versions and probes write access against a
nonexistent resource id — no side effects. It exits 0 only when the key can
actually create versions, edit metadata and submit for review, so run it
right after issuing a new key.

## Archiving and uploading a build

The App Store Connect API has no build-upload endpoint, so `asc-submit` also
wraps `xcodebuild` for the first mile — archive the project and upload it with
the Apple ID session from your signed-in Xcode (no `.p8` needed here):

```sh
asc-submit upload --project MyApp.xcodeproj --scheme MyApp \
    --version 0.7.0 --build 9
```

- Version/build are passed as build settings (`MARKETING_VERSION` /
  `CURRENT_PROJECT_VERSION`), so any project whose Info.plist references
  those variables (the Xcode default) works.
- The archived bundle is verified against the requested version/build before
  upload (`--skip-version-check` to skip; `--archive-only` to stop after
  archiving; `--from-archive path.xcarchive` to re-upload an existing archive).
- Works for macOS (`--platform macOS`, default) and iOS.

Projects with bespoke archive steps (generated projects, private package
resolution, bundle-signing workarounds) keep their own archive script for that
part and use asc-submit for everything after the upload.

## Quick start: one spec, whole release

Write the release plan once (this file can live in your app's repository):

```json
{
  "version": "0.7.0",
  "releaseType": "AFTER_APPROVAL",
  "build": "8",
  "whatsNew": {
    "ja": "安定性と信頼性を改善しました。",
    "en-US": "Stability and reliability improvements."
  },
  "descriptions": {
    "ja": "アプリの説明です。",
    "en-US": "The app description."
  },
  "keywords": {
    "ja": "録音,録画,文字起こし",
    "en-US": "recorder,transcription,meeting"
  },
  "subtitles": {
    "ja": "録って文字起こしするレコーダー",
    "en-US": "Record & transcribe meetings"
  },
  "reviewNotes": "How to test this build for review: …",
  "screenshotsReplace": true,
  "screenshots": {
    "ja": ["shots/ja/01.png", "shots/ja/02.png"],
    "en-US": ["shots/en/01.png"]
  },
  "submit": true
}
```

Preview the plan, then execute:

```sh
asc-submit run 6812783176 --spec release-0.7.0.json --dry-run
asc-submit run 6812783176 --spec release-0.7.0.json --submit --yes
```

`run` will: create the version if missing → wait for build `8` to reach VALID →
attach it → update every localization (What's New, description, keywords) →
set subtitles → replace screenshots → submit for review.
Drop `--submit` to do everything but the submission (e.g. let a human press the
final button).

### Keywords and subtitles

- **Keywords** travel with a version (`appStoreVersionLocalizations`), like
  What's New and descriptions. The field is validated client-side against the
  100-character limit on a single line. Apple's own docs are inconsistent about
  the unit (the version-information reference says 100 *bytes*, the product-page
  guide says 100 *characters*); characters is the limit that multi-byte CJK
  keywords have actually been accepted against, so that is what this tool
  enforces — if your keywords are non-ASCII, keep an eye on the byte count too.
- **Subtitles are app-level, not per version** (`appInfoLocalizations`). The app
  keeps one appInfo per pipeline state, and only the one in an editable state
  (`PREPARE_FOR_SUBMISSION`, `REJECTED`, …) accepts writes — which is why `run`
  applies subtitles *after* creating the version. Calling the `subtitle`
  subcommand while every version is live or in review fails with a hint to
  create the next version first. 30-character limit, validated client-side.

## Individual commands

```sh
asc-submit versions 6812783176                       # list versions and states
asc-submit status 6812783176 --version 0.7.0        # state, build, submission
asc-submit create-version 6812783176 --version 0.7.0
asc-submit whatsnew 6812783176 --version 0.7.0 --locale ja --file whatsnew-ja.txt
asc-submit description 6812783176 --version 0.7.0 --locale en-US --text "…"
asc-submit keywords 6812783176 --version 0.7.0 --locale ja --text "録音,録画"
asc-submit subtitle 6812783176 --locale ja --text "録って文字起こしするレコーダー"
asc-submit review-notes 6812783176 --version 0.7.0 --file notes.md
asc-submit screenshots 6812783176 --version 0.7.0 --locale ja --replace shot-01.png shot-02.png
asc-submit attach-build 6812783176 --version 0.7.0 --build 8 --wait
asc-submit submit 6812783176 --version 0.7.0
asc-submit cancel-submission 6812783176 --version 0.7.0
```

`<app>` accepts the Apple ID (a number) or the bundle ID.

## Notes for macOS apps

- Platform defaults to `OS_X` (`--platform IOS` for iOS apps).
- Screenshot display type defaults to `APP_DESKTOP` (2880×1800 PNG/JPG).
- Upload order defines display order — list files in the order you want them shown.

## Error hints

A `403 Forbidden` almost always means the API key was issued with the
Developer role. The role cannot be changed after creation; create an
App Manager key at *Users and Access → Integrations* and update
`ASC_KEY_PATH` / `ASC_KEY_ID` / `ASC_ISSUER`.

## Limitations (v1)

- App-level metadata beyond the subtitle (privacy labels, pricing, availability)
  is out of scope — those change rarely and are safer to manage in the web UI.
- Screenshot upload order follows invocation order; drag-to-reorder parity in the
  media manager is not implemented.
- App previews (videos) are not managed.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The test suite generates its own throwaway P-256 keys and never talks to
App Store Connect.

## License

[MIT](LICENSE)
