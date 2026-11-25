"""Wall-clock timing of a run: CLI phases, every tool call and every LLM call.

Filled by code (never by the LLM) into ``ArgoCDHealthReport.timings``:

* phases - ``timer.phase("preflight")`` blocks in the CLI;
* tools  - the ``_guarded`` tool wrapper records each call (also in ``--no-llm`` triage);
* llm    - CrewAI ``LLMCall*`` events, timed from the events' own timestamps (handlers run on a thread
           pool, so the time a handler runs is not the time the call happened).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .models import RunTimings, StepTiming


class RunTimer:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._wall0 = time.time()
        self._lock = threading.Lock()
        self.phases: list[StepTiming] = []
        self.steps: list[StepTiming] = []
        self._llm_open: dict[str, dict[str, Any]] = {}  # call_id -> start/end halves

    # ------------------------------------------------------------------ recording
    def offset(self, perf: float | None = None) -> float:
        return round((time.perf_counter() if perf is None else perf) - self._t0, 3)

    @contextmanager
    def phase(self, name: str, detail: str | None = None) -> Iterator[None]:
        start = time.perf_counter()
        ok = True
        try:
            yield
        except BaseException:
            ok = False
            raise
        finally:
            with self._lock:
                self.phases.append(StepTiming(step=name, kind="phase", started_at_s=self.offset(start),
                                              seconds=round(time.perf_counter() - start, 3), detail=detail, ok=ok))

    def tool(self, name: str, start_perf: float, detail: str | None, ok: bool) -> None:
        with self._lock:
            self.steps.append(StepTiming(step=name, kind="tool", started_at_s=self.offset(start_perf),
                                         seconds=round(time.perf_counter() - start_perf, 3), detail=detail, ok=ok))

    def mark(self, name: str, detail: str | None = None, ok: bool = True) -> None:
        """A zero-duration event in the step list (e.g. a guardrail verdict)."""
        with self._lock:
            self.steps.append(StepTiming(step=name, kind="event", started_at_s=self.offset(), seconds=0.0,
                                         detail=detail, ok=ok))

    def llm_started(self, call_id: str, ts: datetime, model: str | None) -> None:
        with self._lock:
            self._llm_open.setdefault(call_id, {}).update(start=ts.timestamp(), model=model)
            self._llm_close(call_id)

    def llm_finished(self, call_id: str, ts: datetime, ok: bool, usage: dict[str, Any] | None = None) -> None:
        # CrewAI runs sync handlers on a thread pool: "completed" can be handled before "started", so
        # both sides are stored by call_id and the step is recorded once both have arrived.
        with self._lock:
            self._llm_open.setdefault(call_id, {}).update(end=ts.timestamp(), ok=ok, usage=usage)
            self._llm_close(call_id)

    def _llm_close(self, call_id: str) -> None:  # caller holds the lock
        c = self._llm_open[call_id]
        if "start" not in c or "end" not in c:
            return
        del self._llm_open[call_id]
        parts = [c["model"]] if c.get("model") else []
        usage = c.get("usage")
        if usage:
            tin = usage.get("prompt_tokens", usage.get("input_tokens"))
            tout = usage.get("completion_tokens", usage.get("output_tokens"))
            if tin is not None or tout is not None:
                parts.append(f"tokens in={tin} out={tout}")
        self.steps.append(StepTiming(step="llm_call", kind="llm", started_at_s=round(c["start"] - self._wall0, 3),
                                     seconds=round(c["end"] - c["start"], 3),
                                     detail=", ".join(str(p) for p in parts) or None, ok=c["ok"]))

    # ------------------------------------------------------------------ CrewAI events
    @contextmanager
    def watch_llm_calls(self) -> Iterator[None]:
        """Record LLM calls emitted on the CrewAI event bus while the block runs."""
        try:
            from crewai.events.event_bus import crewai_event_bus
            from crewai.events.event_types import LLMCallCompletedEvent, LLMCallFailedEvent, LLMCallStartedEvent
        except ImportError:  # pragma: no cover - CrewAI without the event bus: phases/tools still work
            yield
            return

        def on_start(_source: Any, ev: Any) -> None:
            self.llm_started(ev.call_id, ev.timestamp, ev.model)

        def on_done(_source: Any, ev: Any) -> None:
            self.llm_finished(ev.call_id, ev.timestamp, True, getattr(ev, "usage", None))

        def on_fail(_source: Any, ev: Any) -> None:
            self.llm_finished(ev.call_id, ev.timestamp, False)

        handlers = ((LLMCallStartedEvent, on_start), (LLMCallCompletedEvent, on_done), (LLMCallFailedEvent, on_fail))
        for et, h in handlers:
            crewai_event_bus.register_handler(et, h)
        try:
            yield
        finally:
            crewai_event_bus.flush(timeout=10)
            for et, h in handlers:
                crewai_event_bus.off(et, h)

    # ------------------------------------------------------------------ result
    def summary(self) -> RunTimings:
        with self._lock:
            steps = sorted(self.steps, key=lambda s: s.started_at_s)
            tools = [s for s in steps if s.kind == "tool"]
            llms = [s for s in steps if s.kind == "llm"]
            return RunTimings(
                total_seconds=self.offset(),
                phases=list(self.phases),
                steps=steps,
                tool_calls=len(tools), tool_seconds=round(sum(s.seconds for s in tools), 3),
                llm_calls=len(llms), llm_seconds=round(sum(s.seconds for s in llms), 3),
            )
