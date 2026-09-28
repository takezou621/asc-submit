# The run journal

`run`, `upload` and `wait` record every execution as files on disk — there is
no daemon and no database. The files are the interface: the CLI readers, the
`serve` dashboard and any external tool all read the same data.

## Layout

```
.asc-submit/runs/<run-id>/state.json    run metadata + step states (atomic rewrite)
.asc-submit/runs/<run-id>/output.log    append-only log text
```

Resolution order for the runs dir: `--runs-dir` argument →
`$ASC_SUBMIT_RUNS_DIR` → `.asc-submit/runs` (relative to the working
directory).

Run ids are `YYYYMMDD-HHMMSS-hex4` (e.g. `20260928-145233-a1b2`): they sort
chronologically as directory names and are safe in URLs.

## state.json

```json
{
  "id": "20260928-145233-a1b2",
  "command": "run",                    // run | upload | wait
  "title": "6812783176 → 0.7.0",
  "meta": { "app": "6812783176", "version": "0.7.0", "build": "8", … },
  "status": "success",                 // running | success | failed | cancelled
  "error": null,                       // failure/cancellation reason
  "started_at": "2026-09-28T14:52:33.044409+09:00",
  "finished_at": "2026-09-28T14:53:41.210001+09:00",
  "steps": [
    {
      "name": "attach build 8",
      "status": "success",
      "error": null,
      "started_at": "…",
      "finished_at": "…",
      "log_start": 132,                // character offsets into output.log
      "log_end": 214
    }
  ]
}
```

Notes for external readers:

- `state.json` is rewritten atomically (write to `.tmp`, `os.replace`) on
  every transition — poll the file's mtime or re-read it; it is never
  partially written.
- Step log ranges are **character** offsets into the decoded `output.log`,
  written and read by `asc_submit/journal.py` only, so writers and readers
  always agree on the unit.
- Steps still marked `running` when the run finishes inherit the run's final
  status.
- `cancelled` means neither success nor failure: Ctrl-C, or a submit
  confirmation answered "no" (everything except the submission itself did
  ship).

## Reading it

```sh
asc-submit runs                     # summaries, newest first
asc-submit runs <id>                # step-by-step detail
asc-submit logs <id> --follow       # tail -f a live run
asc-submit serve                    # read-only browser dashboard
```

`serve` exposes the same data as JSON at `http://127.0.0.1:8756`
(bind with `--host`/`--port`; it binds localhost only):

| endpoint                  | returns                                |
| ------------------------- | -------------------------------------- |
| `GET /api/runs`           | run summaries, newest first            |
| `GET /api/runs/<id>`      | one run: status, timing, steps         |
| `GET /api/runs/<id>/log`  | log slice, `?from=N[&to=M]` char range |

Anything else can read the files directly:

```sh
jq -r '.steps[] | "\(.status)\t\(.name)"' .asc-submit/runs/*/state.json
```
