"""Optional review is bounded, does not pause execution, and never fabricates approval."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from artemis.agents.planner.planner import ValidationResult, run_async_planner_validation
from artemis.config.agent import PlannerValidationConfig
from artemis.services import llm as service


@pytest.mark.asyncio
async def test_exhausted_advice_cannot_pause_global_task(tmp_path):
    pause_file = tmp_path / "pause"
    failing_call = AsyncMock(side_effect=ConnectionError("connection reset by peer"))

    async def invoke(*args, **kwargs):
        return await service._run_with_recovery(failing_call)

    with (
        patch.object(service, "PAUSE_FILE", pause_file),
        patch.object(service, "_handle_llm_pause_and_resume") as pause,
        patch.object(service, "retry_policy_for", return_value=SimpleNamespace(max_attempts=1)),
        patch.object(service, "acomplete_structured", side_effect=invoke),
    ):
        with pytest.raises(service.LLMExhaustedError):
            await service.acomplete_advisory_structured(
                None, [], schema=ValidationResult, timeout_seconds=0.1
            )
    pause.assert_not_called()
    assert not pause_file.exists()
    assert service._PAUSE_ALLOWED.get() is True
    assert failing_call.await_count == 1


@pytest.mark.asyncio
async def test_hung_advice_is_cancelled_and_policy_restored():
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            assert service._PAUSE_ALLOWED.get() is False
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch.object(service, "acomplete_structured", side_effect=hang):
        task = asyncio.create_task(
            service.acomplete_advisory_structured(
                None, [], schema=ValidationResult, timeout_seconds=0.02
            )
        )
        # The optional policy is task-local, not a global switch.
        await asyncio.sleep(0)
        assert service._PAUSE_ALLOWED.get() is True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 0.5)
    assert cancelled.is_set()
    assert service._PAUSE_ALLOWED.get() is True


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_turned_into_approval():
    with patch.object(service, "acomplete_structured", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.acomplete_advisory_structured(
                None, [], schema=ValidationResult, timeout_seconds=1
            )
    assert service._PAUSE_ALLOWED.get() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("deadline"), ValueError("invalid JSON")])
async def test_validator_failure_is_visible_as_unavailable(error):
    ctx = SimpleNamespace(data_engine=None, agent_config=None)
    with (
        patch("artemis.agents.planner.planner.get_llm"),
        patch("artemis.agents.planner.planner.acomplete_advisory_structured", side_effect=error),
    ):
        result = await run_async_planner_validation(ctx, "goal", "before", "after")
    assert result["status"] == "unavailable"
    assert type(error).__name__ in result["feedback"]


@pytest.mark.asyncio
async def test_unparseable_reply_uses_one_repair_then_unavailable():
    class InvalidModel:
        calls = 0

        async def astream(self, *args, **kwargs):
            self.calls += 1
            yield AIMessage(content="not valid JSON")

    model = InvalidModel()
    config = SimpleNamespace(
        pro=SimpleNamespace(planner_validation=PlannerValidationConfig(timeout_seconds=0.5))
    )
    ctx = SimpleNamespace(data_engine=None, agent_config=config)
    with patch("artemis.agents.planner.planner.get_llm", return_value=model):
        result = await run_async_planner_validation(ctx, "goal", "before", "after")
    assert model.calls == 2
    assert result["status"] == "unavailable"
    assert "StructuredOutputError" in result["feedback"]
