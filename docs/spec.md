# Release spec reference

The spec is a single JSON object describing a whole release; `asc-submit run
<app> --spec release.json` executes it, `--dry-run` prints the plan, and
`asc-submit validate --spec release.json` lints it offline (no API key).
`examples/spec.example.json` is a complete, commented-by-example starting
point.

Keys starting with `_` are a comment convention and ignored.

## Version setup

| key                | type   | default          | notes |
| ------------------ | ------ | ---------------- | ----- |
| `version`          | string | — (required)     | e.g. `"0.7.0"` |
| `platform`         | string | `"MAC_OS"`       | one of `MAC_OS`, `IOS`, `TV_OS`, `VISION_OS` |
| `releaseType`      | string | `"AFTER_APPROVAL"` | `AFTER_APPROVAL` \| `MANUAL` \| `SCHEDULED` |
| `releaseDate`      | string | —                | ISO 8601 **with UTC offset**; implies `SCHEDULED`. Must be in the future. e.g. `"2026-10-01T09:00:00+09:00"` |
| `phasedRelease`    | bool   | —                | enables the 7-day phased curve; only effective with automatic release |
| `build`            | string | —                | build number **as a string** (`"8"`, not `8` — a number never matches ASC build numbers). `run` waits for it to reach `VALID` before attaching |
| `copyright`        | string | —                | version's copyright line, not localized |

## Version-localized metadata

Locale-keyed objects (`{"ja": "…", "en-US": "…"}`). Locales are whatever the
App record has enabled; a few historical aliases are normalized
(`ja-JP` → `ja`, `zh-CN` → `zh-Hans`, `ko-KR` → `ko`).

| key               | limit / format                |
| ----------------- | ----------------------------- |
| `whatsNew`        | —                             |
| `descriptions`    | line breaks rendered as-is    |
| `keywords`        | 100 chars, single line, comma-separated |
| `promotionalText` | 170 chars, single line        |
| `supportUrls`     | http(s) URL                   |
| `marketingUrls`   | http(s) URL                   |

## App-level localized metadata

Also locale-keyed, but written to `appInfoLocalizations` — these travel with
an **editable version** (see
[forensics](forensics.md#app-level-metadata-travels-with-an-editable-version)),
so `run` applies them after creating the version.

| key           | limit / format |
| ------------- | -------------- |
| `subtitles`   | 30 chars, single line |
| `appNames`    | 30 chars, single line |
| `privacyUrls` | http(s) URL    |

## App Review and media

| key                     | type   | default       | notes |
| ----------------------- | ------ | ------------- | ----- |
| `reviewNotes`           | string | —             | shown to Apple's reviewers |
| `screenshotDisplayType` | string | `"APP_DESKTOP"` | e.g. `IPHONE_65` for iOS |
| `screenshotsReplace`    | bool   | `false`       | delete the current set before uploading |
| `screenshots`           | object | —             | locale → list of file paths, **in display order** (upload order = display order) |
| `submit`                | bool   | `false`       | submit for review at the end (or pass `--submit` to `run`) |

## What validate checks

Errors stop the run; warnings are advice.

- **Errors**: placeholder copy (`TODO`, `FIXME`, `TBD`, `Lorem ipsum`,
  `<app name>`-style markers) in any text field; unknown keys (with a
  "did you mean" suggestion for case typos — a mistyped `whatsnew` would
  otherwise ship without release notes); wrong value types; invalid
  `platform` / `releaseType`; `releaseDate` that is not ISO 8601, lacks a
  UTC offset, or is in the past; limit violations (keywords 100, promotional
  text 170, subtitle/name 30); non-http(s) URLs; `build` given as a number;
  missing screenshot files.
- **Warnings**: keyword audit findings (duplicate terms, spaces around
  commas, empty terms, non-ASCII sets whose byte count exceeds 100);
  `submit` without a `build`; `phasedRelease` combined with `MANUAL` /
  `SCHEDULED` (where the curve never applies).

`run` performs the same lint before touching anything — a bad spec fails
before an API key is even needed, so specs can be validated on every PR.

## Execution order

`run` executes the spec in this order, recording each step in the
[run journal](journal.md):

1. create/resolve the version (idempotent)
2. schedule the release (`releaseDate`)
3. phased release on/off
4. wait for the build to reach `VALID`, attach it
5. version localizations (one request per locale, merged)
6. app-level metadata (subtitles, app names, privacy URLs)
7. copyright
8. App Review notes
9. screenshots per locale (replace first if asked)
10. submit for review (with confirmation unless `--yes`)

Every step is idempotent where Apple allows it, so re-running after a partial
failure is safe.
