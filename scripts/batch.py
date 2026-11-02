"""How ``live_check.py`` runs a batch and turns its runs into an exit status.

A batch is minutes of paid model time, and a dropped connection used to end it:
``Connection reset by peer`` in one run killed the process and every result
already collected. A run lost to the network is not a verdict on the model, so
it is retried once and, if it fails again, recorded as *not run* -- never as
clean, and never as a defect the model is charged with.

Kept apart from ``live_check.py`` and free of side effects, for the reason
``answer_checks.py`` is: that script redirects ``FINANCIAL_PLANNER_HOME`` at
import time and so cannot be imported by tests. Nothing here imports the app or
an SDK; the caller says which exceptions count as transport errors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

# Exit statuses. 2 is what main() already returns for "cannot run": nothing, or
# not everything, was checked, so the batch cannot certify a pass or a failure.
EXIT_CLEAN = 0
EXIT_PROBLEMS = 1
EXIT_NOT_RUN = 2


@dataclass
class RunResult:
    """One run of one scenario, all turns, for the summary table."""

    unsourced: int = 0
    # Of those, the figures that matched only a tool's input (plan decision 4).
    tool_input: int = 0
    missing_tools: int = 0
    defects: int = 0
    empty_answers: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)
    # Why the run never finished, if it didn't. Set only by run_once_with_retry.
    not_run: str = ""

    @property
    def ok(self) -> bool:
        # A run that never finished checked nothing, so it is not clean.
        return not self.problems and not self.not_run


def is_transport_error(error: BaseException, transport: tuple[type[BaseException], ...]) -> bool:
    """True if ``error``, or anything in its cause/context chain, is a transport error.

    The chain is walked because a framework layer may re-raise a connection
    failure as its own exception type.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, transport):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def run_once_with_retry(
    run: Callable[[], RunResult],
    transport: tuple[type[BaseException], ...],
    log: Callable[[str], None] = print,
) -> RunResult:
    """Run, retrying once on a transport error; a second one makes it *not run*.

    ``run`` must start from a fresh home each call -- ``live_check.run_scenario``
    resets it first thing -- so the retry cannot read what the lost attempt wrote.
    Any other exception propagates: a bug in the harness or the app must crash
    the batch, not be filed away as an infrastructure failure.
    """
    for attempt in (1, 2):
        try:
            return run()
        except Exception as error:
            if not is_transport_error(error, transport):
                raise
            reason = f"{type(error).__name__}: {error}"
            if attempt == 1:
                log(f"  ~~ lost to the network ({reason}); retrying once on a fresh home")
            else:
                log(f"  !! lost to the network again ({reason}); this run is NOT RUN")
                return RunResult(not_run=reason)
    raise AssertionError("unreachable")


def exit_status(runs: Iterable[RunResult]) -> int:
    """2 if any run was not run, else 1 if any found problems, else 0.

    Not run outranks problems: a batch missing a run cannot vouch for the
    scenario it lost, so a wrapper must not read it as a complete verdict. The
    problems the other runs found are still printed and summed.
    """
    runs = list(runs)
    if any(r.not_run for r in runs):
        return EXIT_NOT_RUN
    return EXIT_CLEAN if all(r.ok for r in runs) else EXIT_PROBLEMS
