# App Store Connect API forensics

Findings from running asc-submit against production submissions. Every item
below was observed live — the originating run ids are kilde App Store workflow
runs recorded in the run journal. These are the reasons the code reads the way
it does; if Apple changes behavior, this is where to compare notes.

The same stories live, in shorter form, as comments next to the code that
implements each workaround (`asc_submit/client.py`, `asc_submit/flows.py`).

## HTTP transport: why every request goes through curl

The stdlib `urllib` negotiates HTTP/1.1, and Apple's edge intermittently
**stalls some resource paths** for such requests. Observed 2026-09-26:
`appStoreVersionLocalizations` GETs time out via urllib while the exact same
URL returns 200 in ~0.6 s via curl — over both HTTP/1.1 and HTTP/2.

So `Client.request` shells out to `curl` (macOS and virtually every Linux
distribution ship it), which negotiates HTTP/2 and avoids that class of
stall entirely. The practical dependency footprint is unchanged; the only
other external binary is `openssl` for JWT signing.

Retry policy: HTTP 429/500/502/503/504 are retried with exponential backoff
(`2**attempt` seconds, 3 retries). 403/404/409/422 are never retried —
they carry meaning (see below).

## The submission API migration (legacy → reviewSubmissions)

The documented-for-years flow `POST /v1/appStoreVersionSubmissions` is
deprecated and now refuses creation outright. Live on kilde's 0.8.1
submission (run **36284583404**):

> HTTP 403 — "The resource 'appStoreVersionSubmissions' does not allow
> 'CREATE'. Allowed operation is: DELETE"

The modern `reviewSubmissions` flow has its own trap: the review submission
is created with the **app** relationship, not the version. Passing
`appStoreVersionForReview` in CREATE answers 409 — "can not be included in a
'CREATE' operation" (run **36285373437**). The working sequence, implemented
in `flows.submit_for_review`:

1. `POST /v1/reviewSubmissions` — relationships: `app`
2. `POST /v1/reviewSubmissionItems` — links `reviewSubmission` ↔ the version
3. `PATCH /v1/reviewSubmissions/{id}` — `attributes.submitted: true`

409/422 anywhere in that sequence means "already exists" (a retried run after
a partial failure) and is tolerated — the flow is idempotent end to end.

**Cancellation** still works on the legacy resource, but only with DELETE —
`DELETE /v1/appStoreVersionSubmissions/{id}` — for versions submitted before
the migration. Modern submissions cancel via
`DELETE /v1/reviewSubmissions/{id}`. `cancel-submission` tries the modern
resource first, falls back to legacy.

## The version's build relationship is to-one

`appStoreVersion.build` is a **singular** relationship. PATCHing
`.../relationships/builds` (plural) answers 404 — "The relationship 'builds'
does not exist" (found live the first time this path ran; run
**36282102696**). The correct request is
`PATCH /v1/appStoreVersions/{id}/relationships/build` with
`{"data": {"type": "builds", "id": …}}` — plural type, singular relationship
name.

## Platform enum is MAC_OS, not OS_X

Creating a version with `platform: "OS_X"` answers HTTP 409 "not a valid
value". The enum Apple enforces is `MAC_OS`, `IOS`, `TV_OS`, `VISION_OS`
(found live when a CI workflow ran create-version for the first time). All
asc-submit commands default to `MAC_OS` and validate the enum client-side.

## Keywords: characters, not bytes (probably)

Apple's own docs disagree with themselves: the version-information reference
says the keywords field is 100 *bytes*, the product-page guide says 100
*characters*. Multi-byte CJK keyword sets of exactly 100 characters have been
accepted in production, so asc-submit enforces **characters** client-side and
warns (via `validate`) when the byte count also exceeds 100. If Apple ever
starts enforcing bytes, that warning is the early signal.

## App-level metadata travels with an editable version

Name, subtitle and privacy policy URL live on `appInfoLocalizations` — but
the app keeps **one appInfo per pipeline state** (`READY_FOR_SALE`,
`WAITING_FOR_REVIEW`, `PREPARE_FOR_SUBMISSION`, …), and only the appInfo in
an editable state accepts writes:

    PREPARE_FOR_SUBMISSION, REJECTED, DEVELOPER_REJECTED,
    METADATA_REJECTED, WAITING_FOR_EXPORT_COMPLIANCE

That is why `run` applies subtitles/app names/privacy URLs **after** creating
the next version (which puts an appInfo into an editable state), and why the
`subtitle` / `app-name` / `privacy-url` subcommands fail with a hint when
every version is live or in review.

## 403 means the key role is wrong — and the doctor probe

Submissions and metadata writes need an API key with the **App Manager** or
Admin role. Developer-role keys can read but get 403 on every write, and the
role cannot be changed after key creation.

`asc-submit doctor` distinguishes "role too weak" from "permission granted"
without side effects by PATCHing a **nonexistent** resource id:

- **HTTP 403** — the role gate fires before existence is checked → the key
  cannot write.
- **HTTP 404** — Apple looked for the resource first → permission granted.

## Screenshot uploads

Screenshots reserve an id (`POST /v1/appScreenshots`), then upload the bytes
to **pre-signed URLs** returned as `uploadOperations` — no Authorization
header, `application/octet-stream` — and commit with `uploaded: true` plus an
MD5 `sourceFileChecksum` (MD5 is Apple's choice here, not ours). Upload order
defines display order; there is no API for reordering after the fact.

## Review outcome states

`appStoreVersion.appStoreState` is the source of truth for `wait`. Terminal
outcomes: `READY_FOR_SALE`, `PENDING_DEVELOPER_RELEASE` (approved, manual
release pending), `REJECTED`, `METADATA_REJECTED`, `DEVELOPER_REJECTED`.
In flight: `WAITING_FOR_REVIEW`, `IN_REVIEW`, `PROCESSING_FOR_APP_STORE`,
`WAITING_FOR_EXPORT_COMPLIANCE` (stuck until the compliance question is
answered in App Store Connect — `wait` calls this out).
