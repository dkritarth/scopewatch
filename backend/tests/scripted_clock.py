"""A monotonic clock a test drives by hand instead of by sleeping (issue #159).

``AgentLoop`` reads its injected ``time_fn`` at every deadline check and calls
its injected ``sleep_fn`` while polling for approval. Hand the loop a
``ScriptedClock`` and time only moves when the test says so, so a deadline test
asserts *which phase expired* rather than which phase the machine happened to
reach before its budget ran out.

Usage::

    clock = ScriptedClock()
    loop = AgentLoop(..., time_fn=clock, sleep_fn=clock.sleep)
    clock.advance(0.08)   # e.g. from inside the provider: the call overran
"""

from __future__ import annotations


class ScriptedClock:
    """A fake monotonic clock, advanced explicitly by the test.

    Callable so it can be passed straight as ``time_fn``; ``sleep`` can be
    passed as ``sleep_fn``, where sleeping both records itself and moves the
    scripted time forward (so the approval poll loop terminates without any
    real time passing).
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        #: How many times the loop read the clock. Lets a test assert that the
        #: loop really is reading *this* clock instead of the real one.
        self.reads = 0
        #: Every duration the loop asked to sleep for, in order.
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.reads += 1
        return self._now

    @property
    def current(self) -> float:
        """The scripted time, without counting a read."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, as an expensive call would."""
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        """Stand in for ``time.sleep``: recorded, and scripted time passes."""
        self.sleeps.append(seconds)
        self._now += seconds
