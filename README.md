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
  "phasedRelease": true,
  "build": "8",
  "whatsNew": {
    "ja": "安定性と信頼性を改善しました。",
    "en-US": "Stability and reliability improvements."
  },
  "descriptions": {
    "ja": "アプリの説明です。",
    "en-US": "The app description."
  },
  "promotionalText": {
    "ja": "新機能の告知テキスト（170字まで）",
    "en-US": "Announce what's new (up to 170 chars)"
  },
  "keywords": {
    "ja": "録音,録画,文字起こし",
    "en-US": "recorder,transcription,meeting"
  },
  "subtitles": {
    "ja": "録って文字起こしするレコーダー",
    "en-US": "Record & transcribe meetings"
  },
  "supportUrls": {
    "ja": "https://kilde.app/ja/support",
    "en-US": "https://kilde.app/support"
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

The spec also accepts `releaseDate` (ISO 8601 with a UTC offset — implies
`releaseType: SCHEDULED`), `marketingUrls`, `privacyUrls` and `appNames`
(app-level, like subtitles), and `copyright`. Everything is optional; `run`
applies only what the spec sets.

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

### Linting the spec before it ships

`run` lints the spec before touching anything — and `validate` runs the same
check standalone, no API key, nothing leaves the machine:

```sh
asc-submit validate --spec release-0.7.0.json
```

It stops the line for the mistakes that otherwise ship silently: placeholder
copy (`TODO`, `Lorem ipsum`, `<app name>`), a mistyped key (`"whatsnew"` would
skip the release notes entirely), field-limit violations (keywords over 100
characters, subtitles over 30), and screenshots that no longer exist at the
given paths. Keyword quality audits (duplicates, spaces after commas, the
100-byte CJK limit) come back as warnings, as does `submit` without a `build`.
Keys starting with `_` are a comment convention and ignored.

## Waiting for the review outcome

Submissions used to end at `submit` — the release loop now closes with `wait`:

```sh
asc-submit wait 6812783176 --version 0.7.0
```

It polls the version's App Store state (every 5 minutes by default,
`--interval` to change; `--timeout` caps the watch at 5 days by default, `0`
waits forever) and exits the moment review resolves:

- **exit 0** — approved: `READY_FOR_SALE` (live on the store) or
  `PENDING_DEVELOPER_RELEASE` (approved, `releaseType MANUAL` — the release
  itself now waits for you in App Store Connect)
- **exit 1** — rejected / metadata rejected / withdrawn, with where to read
  the rejection notes, or the watch timed out (re-run to keep waiting)

A version still in `PREPARE_FOR_SUBMISSION` fails immediately with a hint to
submit it first, and `WAITING_FOR_EXPORT_COMPLIANCE` is called out — review
does not progress until the compliance question is answered.

`wait` records a run in the journal like `run` and `upload` do, so the live
state transitions are watchable from `asc-submit serve` or
`asc-submit logs <id> --follow` for the whole (possibly day-long) review.

For CI or a self-hosted watcher, one notification covers the outcome:

```sh
export ASC_WEBHOOK_URL=https://hooks.slack.com/services/…   # or pass --webhook
```

On resolution `wait` POSTs one Slack-style `{"text": …}` message; a failed
notification only logs a warning, it never masks the outcome.

## Watching a run in real time

`run`, `upload` and `wait` record every step as they execute — status, timing
and log text — under `.asc-submit/runs/<run-id>/` (`state.json` + `output.log`;
move it with `--runs-dir` or `$ASC_SUBMIT_RUNS_DIR`). The files are the
interface: there is no daemon and no database, and every viewer reads the same
data.

Browser dashboard, GitHub-Actions style (steps, durations, live logs):

```sh
asc-submit serve                 # http://127.0.0.1:8756 — add --open to launch it
```

The dashboard is read-only: workflows start from the CLI as always, the
browser only watches. It binds `127.0.0.1` and serves the same data as JSON,
so scripts work too:

| endpoint                   | returns                                |
| -------------------------- | -------------------------------------- |
| `GET /api/runs`            | run summaries, newest first            |
| `GET /api/runs/<id>`       | one run: status, timing, steps         |
| `GET /api/runs/<id>/log`   | log slice, `?from=N[&to=M]` char range |

The same journal is reachable without a browser:

```sh
asc-submit runs                  # list recorded runs
asc-submit runs <id>             # step-by-step detail
asc-submit logs <id> --follow    # tail a live run from the terminal
```

Run states mirror what happened: `success`, `failed` (the red box shows the
error, and the failing step carries it too), `cancelled` — Ctrl-C, or a submit
confirmation answered no (everything except the submission itself did ship).

Each `run` / `upload` / `wait` prints its run id at startup — that id is what you pass
to the commands above (and what `serve`'s URL looks like:
`http://127.0.0.1:8756/runs/<id>`).

### Release controls

Three knobs decide *how* the version goes out, all settable from the spec or
as commands:

- **Phased release** — the 7-day curve where the update reaches 1%, 2%, 5%…
  100% of automatic-update users:

  ```sh
  asc-submit phased-release 6812783176 --version 0.7.0        # show state
  asc-submit phased-release 6812783176 --version 0.7.0 --on   # enable
  ```

  Spec key: `"phasedRelease": true/false`. It only takes effect when the
  version releases automatically (`releaseType: AFTER_APPROVAL`, the default)
  — `validate` warns when the combination can't work.

- **Scheduled release** — a fixed date and time (implies
  `releaseType: SCHEDULED`):

  ```sh
  asc-submit schedule-release 6812783176 --version 0.7.0 --at 2026-10-01T09:00:00+09:00
  ```

  The date must be ISO 8601 **with a UTC offset** and in the future; spec key
  `"releaseDate"`.

- **Manual release** — `releaseType: MANUAL` leaves the release decision to
  you after approval; `asc-submit wait` then resolves to
  `PENDING_DEVELOPER_RELEASE` and reminds you the release is in your hands.

### Keywords and subtitles

- **Keywords** travel with a version (`appStoreVersionLocalizations`), like
  What's New and descriptions. The field is validated client-side against the
  100-character limit on a single line. Apple's own docs are inconsistent about
  the unit (the version-information reference says 100 *bytes*, the product-page
  guide says 100 *characters*); characters is the limit that multi-byte CJK
  keywords have actually been accepted against, so that is what this tool
  enforces — if your keywords are non-ASCII, keep an eye on the byte count too.
- **Subtitles, app names and privacy policy URLs are app-level, not per
  version** (`appInfoLocalizations`). The app keeps one appInfo per pipeline
  state, and only the one in an editable state (`PREPARE_FOR_SUBMISSION`,
  `REJECTED`, …) accepts writes — which is why `run` applies these *after*
  creating the version. Calling the `subtitle` / `app-name` / `privacy-url`
  subcommand while every version is live or in review fails with a hint to
  create the next version first. Subtitles and names have a 30-character
  limit, validated client-side.
- **Version-level metadata** (What's New, description, keywords, promotional
  text, support URL, marketing URL) travels with each version in
  `appStoreVersionLocalizations`; promotional text is capped at 170 characters
  and URLs must be http(s). `copyright` is set on the version itself.

## Individual commands

```sh
asc-submit versions 6812783176                       # list versions and states
asc-submit status 6812783176 --version 0.7.0        # state, build, submission
asc-submit create-version 6812783176 --version 0.7.0
asc-submit phased-release 6812783176 --version 0.7.0 [--on|--off]
asc-submit schedule-release 6812783176 --version 0.7.0 --at 2026-10-01T09:00:00+09:00
asc-submit whatsnew 6812783176 --version 0.7.0 --locale ja --file whatsnew-ja.txt
asc-submit description 6812783176 --version 0.7.0 --locale en-US --text "…"
asc-submit keywords 6812783176 --version 0.7.0 --locale ja --text "録音,録画"
asc-submit promotional-text 6812783176 --version 0.7.0 --locale ja --text "…"
asc-submit support-url 6812783176 --version 0.7.0 --locale ja --text "https://…"
asc-submit marketing-url 6812783176 --version 0.7.0 --locale ja --text "https://…"
asc-submit copyright 6812783176 --version 0.7.0 --text "© 2026 …"
asc-submit subtitle 6812783176 --locale ja --text "録って文字起こしするレコーダー"
asc-submit app-name 6812783176 --locale ja --text "Kilde"
asc-submit privacy-url 6812783176 --locale ja --text "https://…/privacy"
asc-submit review-notes 6812783176 --version 0.7.0 --file notes.md
asc-submit screenshots 6812783176 --version 0.7.0 --locale ja --replace shot-01.png shot-02.png
asc-submit attach-build 6812783176 --version 0.7.0 --build 8 --wait
asc-submit submit 6812783176 --version 0.7.0
asc-submit cancel-submission 6812783176 --version 0.7.0
asc-submit wait 6812783176 --version 0.7.0              # watch review to the outcome
asc-submit validate --spec release-0.7.0.json           # offline spec lint (no key)
asc-submit serve                                   # live browser view of runs
asc-submit runs / runs <id> / logs <id> --follow   # journal from the terminal
```

`<app>` accepts the Apple ID (a number) or the bundle ID.

## Notes for macOS apps

- Platform defaults to `MAC_OS` (`--platform IOS` / `TV_OS` / `VISION_OS`).
- Screenshot display type defaults to `APP_DESKTOP` (2880×1800 PNG/JPG).
- Upload order defines display order — list files in the order you want them shown.

## Using from GitHub Actions

The repo doubles as a composite action, so a workflow can pin the exact commit
(SHA) it runs — no marketplace, no version drift:

```yaml
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: takezou621/asc-submit@<commit-sha>   # validate needs no secrets
        with:
          args: validate --spec release-0.7.0.json

  release:
    needs: validate
    runs-on: [self-hosted, macOS]
    steps:
      - uses: actions/checkout@v4
      - uses: takezou621/asc-submit@<commit-sha>
        with:
          args: run 6812783176 --spec release-0.7.0.json --submit --yes
          key-path: ${{ secrets.ASC_KEY_PEM }}     # .p8 path or PEM content
          key-id: ${{ secrets.ASC_KEY_ID }}
          issuer: ${{ secrets.ASC_ISSUER }}
```

`args` is split on whitespace (no quoted arguments — use `--file` for long
text). The three key inputs map to the `ASC_KEY_PATH` / `ASC_KEY_ID` /
`ASC_ISSUER` environment variables, so the key never appears in the command
line or logs. The action installs asc-submit from the pinned checkout with
`python3 -m pip`; nothing is pulled from PyPI.

## Error hints

A `403 Forbidden` almost always means the API key was issued with the
Developer role. The role cannot be changed after creation; create an
App Manager key at *Users and Access → Integrations* and update
`ASC_KEY_PATH` / `ASC_KEY_ID` / `ASC_ISSUER`.

## Limitations (v1)

- Privacy labels ("nutrition" labels), pricing and availability are out of
  scope — those change rarely and are safer to manage in the web UI.
- Screenshot upload order follows invocation order; drag-to-reorder parity in the
  media manager is not implemented.
- App previews (videos) are not managed.
- `wait` notifications are one Slack-style webhook POST per outcome — no email,
  no richer formats (Discord/Teams adapters would wrap the same JSON).

## Development

```sh
python3 -m unittest discover -s tests -v
```

The test suite generates its own throwaway P-256 keys and never talks to
App Store Connect.

Documentation beyond this README, under [`docs/`](docs/):

- [docs/spec.md](docs/spec.md) — the complete release-spec reference: every
  key, its type and limits, what `validate` checks, and the execution order.
- [docs/journal.md](docs/journal.md) — the run journal format (`state.json`
  + `output.log`), the `serve` JSON API, and how to read runs from scripts.
- [docs/forensics.md](docs/forensics.md) — App Store Connect API findings
  from production runs: the urllib stalls, the reviewSubmissions migration,
  the to-one build relationship, the keywords chars-vs-bytes ambiguity, and
  the 403-vs-404 doctor probe.

## License

[MIT](LICENSE)
