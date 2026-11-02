"""Tests for ``scripts/batch.py``: a run lost to the network is *not run*.

A ``Connection reset by peer`` once killed a paid batch and every result it had
collected. These pin plan decision 9 offline: such an error in one run neither
ends the batch nor counts as clean. Loaded by path, like ``answer_checks``,
because ``scripts/`` is not a package and ``live_check.py`` must not be imported.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import anthropic
import httpx2
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "batch.py"
_spec = importlib.util.spec_from_file_location("batch", _PATH)
assert _spec is not None and _spec.loader is not None
batch = importlib.util.module_from_spec(_spec)
# Registered before exec so the dataclass can resolve its own module.
sys.modules["batch"] = batch
_spec.loader.exec_module(batch)

# What live_check.TRANSPORT_ERRORS names; that module cannot be imported here.
TRANSPORT = (anthropic.APIConnectionError, httpx2.TransportError, ConnectionError)


class _Scripted:
    """A run function that returns or raises each outcome in turn, counting calls."""

    def __init__(self, *outcomes: object) -> None:
        self.queue = list(outcomes)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        outcome = self.queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _batch(*runs) -> list:
    return [batch.run_once_with_retry(run, TRANSPORT, log=lambda _: None) for run in runs]


class TestATransportErrorIsNotRun:
    def test_one_lost_run_does_not_end_the_batch(self) -> None:
        lost = _Scripted(ConnectionResetError("reset"), ConnectionResetError("reset"))
        after = _Scripted(batch.RunResult())

        results = _batch(lost, after)

        assert after.calls == 1, "the run after the lost one must still happen"
        assert results[1].ok

    def test_a_lost_run_is_not_clean(self) -> None:
        [result] = _batch(_Scripted(ConnectionResetError("reset"), ConnectionResetError("reset")))

        assert result.not_run
        assert not result.ok
        # Not charged to the model: it is not a problem the run found.
        assert result.problems == []

    def test_a_lost_run_makes_the_batch_exit_2(self) -> None:
        results = _batch(
            _Scripted(batch.RunResult()),
            _Scripted(ConnectionResetError("reset"), ConnectionResetError("reset")),
        )

        assert batch.exit_status(results) == batch.EXIT_NOT_RUN == 2

    def test_not_run_outranks_problems(self) -> None:
        found = batch.RunResult(problems=["empty final answer"])
        lost = batch.RunResult(not_run="ConnectionResetError: reset")

        assert batch.exit_status([found, lost]) == 2

    def test_the_retry_is_used_when_it_succeeds(self) -> None:
        clean = batch.RunResult()
        run = _Scripted(ConnectionResetError("reset"), clean)

        [result] = _batch(run)

        assert run.calls == 2
        assert result is clean

    def test_it_retries_only_once(self) -> None:
        run = _Scripted(*[ConnectionResetError("reset")] * 3)

        _batch(run)

        assert run.calls == 2


class TestWhatCountsAsTransport:
    def test_the_sdk_connection_error(self) -> None:
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
        assert batch.is_transport_error(error, TRANSPORT)

    def test_a_stream_cut_mid_response(self) -> None:
        assert batch.is_transport_error(httpx2.RemoteProtocolError("closed"), TRANSPORT)

    def test_one_wrapped_by_a_framework_layer(self) -> None:
        try:
            try:
                raise ConnectionResetError("Connection reset by peer")
            except ConnectionResetError as inner:
                raise RuntimeError("graph step failed") from inner
        except RuntimeError as outer:
            assert batch.is_transport_error(outer, TRANSPORT)

    def test_a_bug_is_not_and_crashes_the_batch(self) -> None:
        run = _Scripted(KeyError("scenario"))

        with pytest.raises(KeyError):
            _batch(run)
        assert run.calls == 1, "a bug must not be retried as if it were the network"


class TestExitStatus:
    def test_all_clean_is_0(self) -> None:
        assert batch.exit_status([batch.RunResult(), batch.RunResult()]) == 0

    def test_a_problem_is_1(self) -> None:
        assert batch.exit_status([batch.RunResult(), batch.RunResult(problems=["x"])]) == 1
