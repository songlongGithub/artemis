# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression checks for the optional Flash Decisions path."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from artemis.agents.flash import decision


@pytest.fixture
def screen():
    raw = [
        {
            "text": text,
            "resource-id": "android:id/title",
            "class": "android.widget.TextView",
            "package": "com.android.settings",
            "enabled": "true",
            "checkable": "false",
            "bounds": f"[240,{y}][450,{y + 60}]",
        }
        for text, y in (("Airplane Mode", 100), ("Wi‑Fi", 200), ("Bluetooth", 300))
    ]
    indexed = [
        {
            "resource_id": e["resource-id"],
            "text": e["text"],
            "class": e["class"],
            "bounds": decision.parse_bounds(e["bounds"]),
        }
        for e in raw
    ]
    indexed.insert(1, {"text": "private Wi-Fi SSID", "is_ocr": True})
    return raw, indexed


@pytest.fixture
def run_settings(screen):
    raw, indexed = screen
    ctx = SimpleNamespace(data_engine=None)
    state = SimpleNamespace(indexed_elements=indexed)
    controller = SimpleNamespace(get_ui_elements=AsyncMock(return_value=raw))
    return ctx, state, [SimpleNamespace(name=t) for t in ("click", "swipe")], controller


def install_response(monkeypatch, choice, status=200, confidence=0.98, foreground_after=None):
    monkeypatch.setattr(decision, "_api_key", lambda: "test-key")
    monkeypatch.setattr(
        decision,
        "get_current_foreground_package",
        Mock(side_effect=["com.android.settings", foreground_after or "com.android.settings"]),
    )

    def reply(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
        assert request.headers["x-goog-api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["model"] == "gemini-decision-1"
        assert "private Wi-Fi SSID" not in request.content.decode()
        criteria = body["questions"]["action"]["criteria"]
        assert "other" in criteria
        probabilities = {key: (1 - confidence) / (len(criteria) - 1) for key in criteria}
        probabilities[choice] = confidence
        return httpx.Response(
            status,
            json={
                "answers": {
                    "action": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": confidence,
                        "probabilities": probabilities,
                    }
                },
                "usage": {"total_input_tokens": 12, "total_output_tokens": 0, "total_tokens": 12},
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        decision.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(reply), **kwargs),
    )


@pytest.mark.asyncio
async def test_off_by_default_does_not_read_opencodex_or_contact_google(
    monkeypatch, tmp_path, run_settings
):
    monkeypatch.delenv("ARTEMIS_FLASH_DECISIONS", raising=False)
    monkeypatch.setattr(decision, "get_env_file", lambda: tmp_path / ".env")
    forbidden = Mock(side_effect=AssertionError("unexpected network or device access"))
    monkeypatch.setattr(decision.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(decision, "get_current_foreground_package", forbidden)
    ctx, state, tools, controller = run_settings

    assert await decision.try_initial_action(ctx, state, "Open Wi-Fi", tools, controller) is None
    forbidden.assert_not_called()
    controller.get_ui_elements.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal", "choice", "expected"),
    [
        ("Scroll the Settings list down once", "finger_up", ("swipe", {"direction": "up"})),
        (
            "Open the settings page listing nearby wireless networks",
            "open_wifi",
            ("click", {"target": 3}),
        ),
    ],
)
async def test_selected_candidate_uses_existing_tool_and_index(
    monkeypatch, run_settings, goal, choice, expected
):
    install_response(monkeypatch, choice)
    ctx, state, tools, controller = run_settings
    result = await decision.try_initial_action(ctx, state, goal, tools, controller)

    assert [(call["name"], call["args"]) for call in result.tool_calls] == [expected]
    assert result.usage_metadata == {"input_tokens": 12, "output_tokens": 0, "total_tokens": 12}
    controller.get_ui_elements.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "status", "confidence", "foreground_after", "move_wifi"),
    [
        ("open_wifi", 402, 0.98, None, False),
        ("open_wifi", 403, 0.98, None, False),
        ("open_wifi", 429, 0.98, None, False),
        ("open_wifi", 503, 0.98, None, False),
        ("open_wifi", 200, 0.55, None, False),
        ("other", 200, 0.98, None, False),
        ("open_wifi", 200, 0.98, "other.package", False),
        ("open_wifi", 200, 0.98, None, True),
    ],
)
async def test_rejects_unavailable_or_stale_navigation(
    monkeypatch, run_settings, choice, status, confidence, foreground_after, move_wifi
):
    install_response(monkeypatch, choice, status, confidence, foreground_after)
    ctx, state, tools, controller = run_settings
    if move_wifi:
        controller.get_ui_elements.return_value[1]["bounds"] = "[240,220][450,280]"
    assert (
        await decision.try_initial_action(
            ctx, state, "Open the settings page listing nearby wireless networks", tools, controller
        )
        is None
    )


@pytest.mark.asyncio
async def test_exact_read_only_navigation_does_not_make_an_api_request(monkeypatch, run_settings):
    monkeypatch.setattr(decision, "_api_key", lambda: "test-key")
    monkeypatch.setattr(
        decision, "get_current_foreground_package", lambda _: "com.android.settings"
    )
    network = Mock(side_effect=AssertionError("unexpected request"))
    monkeypatch.setattr(decision.httpx, "AsyncClient", network)
    ctx, state, tools, controller = run_settings

    result = await decision.try_initial_action(ctx, state, "Open Wi-Fi settings", tools, controller)
    assert result.tool_calls[0]["args"] == {"target": 3}
    assert result.usage_metadata is None
    assert result.response_metadata["decision_source"] == "local"
    network.assert_not_called()


@pytest.mark.asyncio
async def test_deadline_defers_without_executing_an_action(monkeypatch, run_settings):
    install_response(monkeypatch, "open_wifi")
    monkeypatch.setattr(decision, "_TIMEOUT", 0.01)
    ctx, state, tools, controller = run_settings

    async def slow_observation():
        await asyncio.sleep(1)
        raise AssertionError("should have been cancelled")

    controller.get_ui_elements.side_effect = slow_observation
    assert (
        await decision.try_initial_action(
            ctx, state, "Open the settings page listing nearby wireless networks", tools, controller
        )
        is None
    )


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"type": "text", "choice": "open_wifi"},
        {"type": "choice", "choice": "unknown"},
        {
            "type": "choice",
            "choice": "open_wifi",
            "confidence": True,
            "probabilities": {"open_wifi": 0.99, "other": 0.01},
        },
        {
            "type": "choice",
            "choice": "open_wifi",
            "confidence": 0.99,
            "probabilities": {"open_wifi": float("nan"), "other": 0.01},
        },
        {
            "type": "choice",
            "choice": "open_wifi",
            "confidence": 0.99,
            "probabilities": {"open_wifi": 0.99, "other": 0.99},
        },
        {
            "type": "choice",
            "choice": "open_wifi",
            "confidence": 0.99,
            "probabilities": {"open_wifi": 0.99},
        },
    ],
)
def test_invalid_probabilities_defer(answer):
    assert decision._selected_choice(answer, {"open_wifi": "Open Wi-Fi", "other": "Defer"}) is None


@pytest.mark.asyncio
async def test_structured_trace_records_selection_even_if_telemetry_fails(
    monkeypatch, run_settings
):
    install_response(monkeypatch, "open_wifi")
    ctx, state, tools, controller = run_settings
    ctx.data_engine = SimpleNamespace(
        current_step_id="step",
        record_trace=Mock(side_effect=RuntimeError("storage unavailable")),
    )
    result = await decision.try_initial_action(
        ctx, state, "Open the settings page listing nearby wireless networks", tools, controller
    )
    assert result.response_metadata["decision_source"] == "decisions"
    trace = ctx.data_engine.record_trace.call_args.kwargs
    assert trace["type"] == "decision"
    assert trace["payload"]["outcome"] == "selected:open_wifi"
    assert trace["duration"] >= 0
