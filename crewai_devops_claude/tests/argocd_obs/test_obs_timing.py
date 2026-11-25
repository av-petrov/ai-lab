"""Run timing: LLM call pairing from CrewAI events, tool error marking, Markdown rendering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argocd_observability.report import render_timings
from argocd_observability.timing import RunTimer

from .conftest import argocd_backend, make_ctx


def test_llm_calls_timed_from_event_timestamps():
    t = RunTimer()
    t0 = datetime.now(timezone.utc)
    t.llm_started("a", t0, "openai/qwen-local")
    t.llm_started("b", t0 + timedelta(seconds=3), "openai/qwen-local")
    t.llm_finished("b", t0 + timedelta(seconds=4), True, {"prompt_tokens": 100, "completion_tokens": 7})
    t.llm_finished("a", t0 + timedelta(seconds=2.5), False)
    t.llm_finished("late", t0 + timedelta(seconds=9), True)  # handled before its start (thread pool)
    t.llm_finished("unknown", t0, True)  # completion whose start never arrives: not counted
    t.llm_started("late", t0 + timedelta(seconds=8), "m")
    s = t.summary()
    assert s.llm_calls == 3 and s.llm_seconds == 4.5
    first, second, late = s.steps  # sorted by start
    assert late.seconds == 1.0
    assert first.seconds == 2.5 and first.ok is False
    assert second.seconds == 1.0 and second.detail == "openai/qwen-local, tokens in=100 out=7"


def test_crewai_events_are_recorded():
    from crewai.events.event_bus import crewai_event_bus
    from crewai.events.event_types import LLMCallCompletedEvent, LLMCallStartedEvent
    from crewai.events.types.llm_events import LLMCallType

    t = RunTimer()
    with t.watch_llm_calls():
        crewai_event_bus.emit(None, LLMCallStartedEvent(call_id="c1", model="m", messages="hi"))
        crewai_event_bus.emit(None, LLMCallCompletedEvent(call_id="c1", model="m", response="ok",
                                                          call_type=LLMCallType.LLM_CALL))
    assert t.summary().llm_calls == 1
    crewai_event_bus.emit(None, LLMCallStartedEvent(call_id="c2", model="m", messages="hi"))
    crewai_event_bus.flush()
    assert "c2" not in t._llm_open  # handlers removed after the block


def test_tool_errors_marked_and_rendered(broken_cluster_routes):
    from argocd_observability.tools import build_tools

    ctx = make_ctx(argocd_backend(broken_cluster_routes)[0])
    tools = {tl.name: tl for tl in build_tools(ctx)}
    tools["argocd_get_application"].run(app_name="never")
    tools["argocd_get_application"].run(app_name="does-not-exist")
    with ctx.timer.phase("agent"):
        pass
    s = ctx.timer.summary()
    assert [x.ok for x in s.steps] == [True, False]
    md = "\n".join(render_timings(s))
    assert "argocd_get_application (error)" in md and "app_name=never" in md and "| agent |" in md
