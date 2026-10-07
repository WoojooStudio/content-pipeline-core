"""Idempotency — checks job_id and output existence. Re-runs without --force are skipped."""
from __future__ import annotations

from pathlib import Path

from .ledger import read_all


class AlreadyDone(Exception):
    """Raised when re-running work that is already done."""
    pass


def check_idempotent(
    *,
    ledger_path: Path,
    event: str,
    job_id: str | None = None,
    output_path: Path | None = None,
    force: bool = False,
) -> None:
    """Raise AlreadyDone if the work is already done.

    force=True skips the check (for re-runs).
    At least one of job_id or output_path must be given.
    """
    if job_id is None and output_path is None:
        raise ValueError("At least one of job_id or output_path must be given.")

    if force:
        return

    if output_path is not None and output_path.exists():
        raise AlreadyDone(
            f"Output already exists: {output_path}\n"
            "Use the --force flag to re-run."
        )

    if job_id is not None:
        entries = read_all(ledger_path)
        for entry in entries:
            if entry.get("job_id") == job_id and entry.get("event") == event:
                raise AlreadyDone(
                    f"job_id={job_id!r} event={event!r} is already recorded in the ledger.\n"
                    "Use the --force flag to re-run."
                )
