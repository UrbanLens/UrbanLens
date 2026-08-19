"""A tiny step runner: named steps, timings, captured output, one JSON record.

Written because the existing deploy path answers "did the command exit 0",
which is not the same question as "is the site up and serving the new code".
A run here is a list of named steps; each records its own status, duration and
output tail, so a caller can be told *which* part failed without reading a
transcript - and an agent can act on the result without spending tokens
parsing logs.

Stdlib only, for the reasons in this package's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: How much of a step's output is kept in the JSON record. The full output
#: always goes to the run's log file; this is what a caller sees inline, sized
#: to carry a traceback's tail without carrying an entire test session.
_TAIL_CHARS = 4000


@dataclass
class StepResult:
    """One completed step."""

    name: str
    status: str
    seconds: float
    detail: str = ""
    output_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {"name": self.name, "status": self.status, "seconds": round(self.seconds, 2), "detail": self.detail, "output_tail": self.output_tail}


@dataclass
class Run:
    """A whole pipeline run, and the record it leaves behind.

    Attributes:
        run_id: Identifier the caller can poll on.
        kind: Which pipeline this is ("staging-deploy", "dev-create", ...).
        log_path: Full transcript. The JSON record carries only tails.
        steps: Completed steps, in order.
    """

    run_id: str
    kind: str
    log_path: Path
    steps: list[StepResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    status: str = "running"
    context: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        """Append one line to the transcript, timestamped."""
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")

    def run_step(self, name: str, command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 900, check: bool = True) -> StepResult:
        """Run one shell step, recording its outcome.

        Args:
            name: Step name, as reported to the caller.
            command: argv to execute.
            cwd: Working directory.
            env: Full environment for the child, or None to inherit.
            timeout: Seconds before the step is killed and marked failed.
            check: When False, a non-zero exit is recorded but does not fail
                the run - for steps whose failure is informational.

        Returns:
            The recorded result.
        """
        self.log(f"--- {name}: {' '.join(command)}")
        started = time.monotonic()
        try:
            completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
            output = (completed.stdout or "") + (completed.stderr or "")
            ok = completed.returncode == 0
            detail = "" if ok else f"exit {completed.returncode}"
        except subprocess.TimeoutExpired as exc:
            output = f"timed out after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"
            ok = False
            detail = f"timeout after {timeout}s"
        except OSError as exc:
            output = str(exc)
            ok = False
            detail = str(exc)

        seconds = time.monotonic() - started
        self.log(output.rstrip() or "(no output)")
        status = "ok" if ok else ("warn" if not check else "failed")
        result = StepResult(name=name, status=status, seconds=seconds, detail=detail, output_tail=output[-_TAIL_CHARS:])
        self.steps.append(result)
        return result

    def run_check(self, name: str, predicate: Callable[[], tuple[bool, str]], *, timeout: int = 300, interval: float = 3.0) -> StepResult:
        """Poll a predicate until it passes or the timeout expires.

        This is the piece the previous deploy path lacked: it waits for a state
        to become true rather than assuming it already is because a command
        exited 0.

        Args:
            name: Step name.
            predicate: Returns ``(passed, detail)``. Exceptions count as "not
                yet" - a connection refused during startup is expected, not a
                reason to abandon the wait.
            timeout: Seconds to keep trying.
            interval: Seconds between attempts.

        Returns:
            The recorded result.
        """
        self.log(f"--- {name}: waiting up to {timeout}s")
        started = time.monotonic()
        detail = "never satisfied"
        while time.monotonic() - started < timeout:
            try:
                passed, detail = predicate()
            except Exception as exc:
                passed, detail = False, f"{type(exc).__name__}: {exc}"
            if passed:
                seconds = time.monotonic() - started
                self.log(f"{name}: {detail}")
                result = StepResult(name=name, status="ok", seconds=seconds, detail=detail)
                self.steps.append(result)
                return result
            time.sleep(interval)

        seconds = time.monotonic() - started
        self.log(f"{name}: FAILED - {detail}")
        result = StepResult(name=name, status="failed", seconds=seconds, detail=detail)
        self.steps.append(result)
        return result

    def record(self, name: str, status: str, detail: str = "") -> StepResult:
        """Record a step that was decided without running a command."""
        result = StepResult(name=name, status=status, seconds=0.0, detail=detail)
        self.steps.append(result)
        self.log(f"--- {name}: {status} {detail}")
        return result

    @property
    def failed(self) -> bool:
        """Whether any step failed outright."""
        return any(step.status == "failed" for step in self.steps)

    def finish(self) -> dict[str, Any]:
        """Close the run and return its JSON-safe record."""
        self.finished_at = datetime.now(UTC).isoformat()
        self.status = "failed" if self.failed else "ok"
        record = {
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_path": str(self.log_path),
            "context": self.context,
            "steps": [step.as_dict() for step in self.steps],
        }
        self.log(f"=== {self.status.upper()} ===")
        self.log_path.with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record


def new_run(kind: str, run_dir: Path, run_id: str = "") -> Run:
    """Start a run, creating its log directory.

    Args:
        kind: Pipeline name.
        run_dir: Directory to write ``<run_id>.log`` and ``<run_id>.json`` into.
        run_id: Use this id instead of generating one. A caller that has
            already told somebody the id must be able to make the run use it -
            otherwise it hands out an id that will never exist.

    Returns:
        The started run.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or f"{kind}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    run = Run(run_id=run_id, kind=kind, log_path=run_dir / f"{run_id}.log")
    run.log(f"=== {kind} started ===")
    return run
