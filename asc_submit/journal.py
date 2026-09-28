"""On-disk journal of workflow runs — the data layer for live monitoring.

A run is a directory under the runs dir (``.asc-submit/runs`` by default;
override with ``--runs-dir`` or ``$ASC_SUBMIT_RUNS_DIR``):

    <run_id>/state.json   run metadata + per-step state, rewritten atomically
    <run_id>/output.log   append-only log; steps record character ranges

The files are the interface: ``asc-submit runs`` / ``asc-submit logs`` read
them from the terminal, ``asc-submit serve`` renders them in a browser, and
anything else (curl, jq, an editor) can read them directly. Runs are written
by the ``run`` and ``upload`` commands as they execute — there is no daemon
and no database to keep alive.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

RUNS_DIR_ENV = "ASC_SUBMIT_RUNS_DIR"
DEFAULT_RUNS_DIR = ".asc-submit/runs"
# Run ids sort chronologically as directory names and are safe in URLs.
RUN_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATE_FILE = "state.json"
LOG_FILE = "output.log"


def resolve_runs_dir(runs_dir: str | os.PathLike | None = None) -> Path:
    """Runs dir precedence: argument > $ASC_SUBMIT_RUNS_DIR > .asc-submit/runs."""
    return Path(runs_dir or os.environ.get(RUNS_DIR_ENV) or DEFAULT_RUNS_DIR)


def _now() -> str:
    # Microsecond precision: runs started in the same second must still sort.
    return datetime.now().astimezone().isoformat()


def _message(err: BaseException) -> str:
    return str(err) or err.__class__.__name__


class RunJournal:
    """Records one workflow execution: steps, statuses, timing and log text.

    Step log ranges are character offsets into the decoded ``output.log`` —
    written and read by this module only, so writers and readers always agree
    on the unit (the HTTP API serves the slices, browsers never count bytes).
    """

    def __init__(
        self,
        command: str,
        title: str,
        meta: dict | None = None,
        runs_dir: str | os.PathLike | None = None,
    ):
        self.runs_dir = resolve_runs_dir(runs_dir)
        stamp = datetime.now().astimezone()
        self.run_id = stamp.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.dir = self.runs_dir / self.run_id
        self.state: dict = {
            "id": self.run_id,
            "command": command,
            "title": title,
            "meta": meta or {},
            "status": STATUS_RUNNING,
            "error": None,
            "started_at": _now(),
            "finished_at": None,
            "steps": [],
        }
        self._log_chars = 0
        self._started = False
        self._finished = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        self._write_state()

    @contextlib.contextmanager
    def step(self, name: str):
        """Record one workflow step; a raised error fails the step and the run."""
        entry = {
            "name": name,
            "status": STATUS_RUNNING,
            "error": None,
            "started_at": _now(),
            "finished_at": None,
            "log_start": self._log_chars,
            "log_end": self._log_chars,
        }
        self.state["steps"].append(entry)
        self._write_state()
        try:
            yield entry
        except BaseException as err:
            entry["status"] = STATUS_FAILED
            entry["error"] = _message(err)
            entry["finished_at"] = _now()
            entry["log_end"] = self._log_chars
            self._write_state()
            self.finish(STATUS_FAILED, _message(err))
            raise
        else:
            entry["status"] = STATUS_SUCCESS
            entry["finished_at"] = _now()
            entry["log_end"] = self._log_chars
            self._write_state()

    def log(self, message: str) -> None:
        """Print a line to the terminal and append it to the run's log."""
        print(message, flush=True)
        if not self._started:
            return
        text = message if message.endswith("\n") else message + "\n"
        with open(self.dir / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(text)
        self._log_chars += len(text)

    def succeed(self) -> None:
        self.finish(STATUS_SUCCESS)

    def fail(self, error: str) -> None:
        self.finish(STATUS_FAILED, error)

    def cancel(self, error: str = "interrupted") -> None:
        self.finish(STATUS_CANCELLED, error)

    def finish(self, status: str, error: str | None = None) -> None:
        """Close the run; steps still marked running inherit the run's status."""
        if self._finished:
            return
        self._finished = True
        self.state["status"] = status
        self.state["error"] = error
        self.state["finished_at"] = _now()
        for entry in self.state["steps"]:
            if entry["status"] == STATUS_RUNNING:
                entry["status"] = status
                entry["finished_at"] = self.state["finished_at"]
                entry["log_end"] = self._log_chars
        self._write_state()

    def _write_state(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.dir / STATE_FILE)


class NullJournal:
    """Stand-in for code paths that print but do not record (spec dry-runs)."""

    run_id = None

    @staticmethod
    def step(name: str):
        return contextlib.nullcontext()

    @staticmethod
    def log(message: str) -> None:
        print(message, flush=True)

    @staticmethod
    def succeed() -> None:
        pass

    @staticmethod
    def fail(error: str) -> None:
        pass

    @staticmethod
    def cancel(error: str = "interrupted") -> None:
        pass


# ---------------------------------------------------------------------------
# readers (CLI subcommands + the serve view both go through these)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _with_duration(state: dict) -> dict:
    started = _parse_time(state.get("started_at"))
    ended = _parse_time(state.get("finished_at"))
    if started is None:
        state["duration_seconds"] = None
    else:
        delta = (ended or datetime.now().astimezone()) - started
        state["duration_seconds"] = max(0, round(delta.total_seconds()))
    return state


def list_runs(runs_dir: str | os.PathLike | None = None) -> list[dict]:
    """Run summaries, newest first; unreadable entries are skipped, not fatal."""
    root = resolve_runs_dir(runs_dir)
    runs = []
    if root.is_dir():
        for child in root.iterdir():
            if not (child.is_dir() and RUN_ID_PATTERN.match(child.name)):
                continue
            try:
                state = json.loads((child / STATE_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            state.pop("steps", None)
            runs.append(_with_duration(state))
    # Directory names tie within one second; the recorded start time decides.
    epoch = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
    runs.sort(key=lambda s: (_parse_time(s.get("started_at")) or epoch, s.get("id") or ""), reverse=True)
    return runs


def read_run(run_id: str, runs_dir: str | os.PathLike | None = None) -> dict | None:
    """Full state (steps included) for one run id, or None if unknown."""
    if not RUN_ID_PATTERN.match(run_id or ""):
        return None
    path = resolve_runs_dir(runs_dir) / run_id / STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _with_duration(state)


def read_log(
    run_id: str,
    runs_dir: str | os.PathLike | None = None,
    start: int = 0,
    end: int | None = None,
) -> tuple[str, int, int]:
    """Slice of the run's log: (text, next_offset, total_characters)."""
    if not RUN_ID_PATTERN.match(run_id or ""):
        return ("", 0, 0)
    path = resolve_runs_dir(runs_dir) / run_id / LOG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ("", 0, 0)
    start = max(0, start)
    end = len(text) if end is None else max(start, end)
    return (text[start:end], end, len(text))
