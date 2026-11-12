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

"""Optional closed-set selection for the first read-only Settings action."""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
import uuid

from dotenv import dotenv_values
import httpx
from langchain_core.messages import AIMessage

from artemis.config.paths import get_env_file
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.graph.state import State
from artemis.services.token_meter import record_llm_usage
from artemis.utils.visualization import parse_bounds
from third_party.mobile_use.controllers.platform_specific_commands_controller import (
    get_current_foreground_package,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_MODEL = "gemini-decision-1"
_ORIGIN = "https://generativelanguage.googleapis.com"
_ENDPOINT = f"{_ORIGIN}/v1beta/decisions"
_PACKAGE = "com.android.settings"
_TIMEOUT = 3.0
_ENABLE = "ARTEMIS_FLASH_DECISIONS"
_OTHER = "other"
_SCROLL = re.compile(r"\b(?:scroll|swipe)\b|滚动|滑动|滑一下|滑一次", re.IGNORECASE)
_NAVIGATE = re.compile(r"\b(?:open|navigate|go to)\b|打开|进入|前往", re.IGNORECASE)
_TITLE_IDS = {"android:id/title", f"{_PACKAGE}:id/title"}
_TEXT_VIEW = "android.widget.TextView"
_NAVIGATION = {
    "open_wifi": ("Wi-Fi", ("wi-fi", "wlan", "无线局域网"), "View nearby wireless networks"),
    "open_bluetooth": ("Bluetooth", ("bluetooth", "蓝牙"), "View nearby Bluetooth devices"),
}
_HOME_ANCHORS = {"airplane mode", "sim & network settings", "飞行模式", "sim 卡和移动网络"}
_EXACT_NAVIGATION = re.compile(
    r"(?:打开|进入|open)\s*(?P<label>wi-fi|wlan|bluetooth|无线局域网|蓝牙)"
    r"\s*(?:设置(?:页面)?|settings)?[。.!！]?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Action:
    tool: str
    target: str | int
    description: str
    bounds: tuple[int, ...] = ()
    resource_id: str = ""

    def message(self, usage: dict | None, *, source: str) -> AIMessage:
        field = "target" if self.tool == "click" else "direction"
        return AIMessage(
            content=self.description,
            tool_calls=[{"name": self.tool, "args": {field: self.target}, "id": uuid.uuid4().hex}],
            usage_metadata=usage,
            response_metadata={"decision_source": source},
        )


def _label(value: str) -> str:
    return value.strip().casefold().replace("‑", "-").replace("–", "-")


def _home_signature(elements: list[dict]) -> frozenset[tuple[str, tuple]]:
    """Use fixed Settings rows, not arbitrary screen text, to identify the page."""
    anchors = _HOME_ANCHORS | {alias for _, aliases, _ in _NAVIGATION.values() for alias in aliases}
    return frozenset(
        (_label(e["text"]), tuple(e["bounds"]))
        for e in elements
        if e.get("resource_id") in _TITLE_IDS
        and e.get("class") == _TEXT_VIEW
        and not e.get("is_ocr")
        and not e.get("is_hint")
        and e.get("bounds")
        and _label(e.get("text", "")) in anchors
    )


def _candidates(elements: list[dict], goal: str, tools: set[str]) -> dict[str, _Action]:
    if len({label for label, _ in _home_signature(elements)}) < 2:
        return {}
    if _SCROLL.search(goal) and "swipe" in tools:
        return {
            "finger_up": _Action("swipe", "up", "Swipe up once to reveal lower Settings entries."),
            "finger_down": _Action(
                "swipe", "down", "Swipe down once to reveal earlier Settings entries."
            ),
        }
    if not _NAVIGATE.search(goal) or "click" not in tools:
        return {}
    actions = {}
    for choice, (title, aliases, purpose) in _NAVIGATION.items():
        matches = [
            (i, e)
            for i, e in enumerate(elements, 1)
            if e.get("resource_id") in _TITLE_IDS
            and e.get("class") == _TEXT_VIEW
            and not e.get("is_ocr")
            and not e.get("is_hint")
            and len(e.get("bounds") or ()) == 4
            and _label(e.get("text", "")) in aliases
        ]
        if len(matches) == 1:
            i, e = matches[0]
            actions[choice] = _Action(
                "click",
                i,
                f"Open the {title} Settings page. {purpose}.",
                tuple(e["bounds"]),
                e["resource_id"],
            )
    return actions


def _exact_choice(goal: str, candidates: dict[str, _Action]) -> str | None:
    match = _EXACT_NAVIGATION.fullmatch(_label(goal))
    if match:
        label = match["label"]
        for choice, (_, aliases, _) in _NAVIGATION.items():
            if choice in candidates and label in aliases:
                return choice
    return None


def _still_on_same_page(
    raw_elements: list[dict],
    original: frozenset[tuple[str, tuple]],
    choice: str,
    action: _Action,
) -> bool:
    # Require the same Settings rows at the same bounds. Ignore unrelated screen
    # contents (e.g. connected SSID and the clock), and never return them to Google.
    current = [
        {
            "resource_id": e.get("resource-id"),
            "class": e.get("class"),
            "text": e.get("text", ""),
            "bounds": parse_bounds(e.get("bounds")),
        }
        for e in raw_elements
        if e.get("package") == _PACKAGE
        and e.get("enabled") in (True, "true")
        and e.get("checkable") in (False, "false")
    ]
    if not original or not original.issubset(_home_signature(current)):
        return False
    if action.tool != "click":
        return True
    aliases = _NAVIGATION[choice][1]
    matches = [
        e
        for e in current
        if e["resource_id"] == action.resource_id
        and e["class"] == _TEXT_VIEW
        and _label(e["text"]) in aliases
    ]
    return len(matches) == 1 and matches[0]["bounds"] == action.bounds


def _api_key() -> str | None:
    enabled = os.getenv(_ENABLE)
    if enabled is None:
        try:
            enabled = dotenv_values(get_env_file(), interpolate=False).get(_ENABLE)
        except (OSError, ValueError):
            return None
    if enabled != "1":
        return None

    # Reuse the configured Google key only with its native Google API origin.
    # Never send a gateway credential or a custom provider's key to Google.
    config_path = Path.home() / ".opencodex" / "config.json"
    try:
        provider = json.loads(config_path.read_text(encoding="utf-8"))["providers"]["google"]
        if provider["baseUrl"].rstrip("/") == _ORIGIN and provider["authMode"] == "key":
            key = provider["apiKey"]
            if isinstance(key, str) and key.strip():
                return key.strip()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    logger.info("Flash Decisions: configured Google API key unavailable; using Flash")
    return None


def _selected_choice(answer: dict, criteria: dict[str, str]) -> str | None:
    if answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice == _OTHER or choice not in criteria:
        return None
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or probabilities.keys() != criteria.keys():
        return None
    confidence = answer.get("confidence")
    values = [*probabilities.values(), confidence]
    if not all(type(value) in (int, float) and 0 <= value <= 1 for value in values):
        return None
    if abs(sum(probabilities.values()) - 1) > 0.01:
        return None
    others = max(value for name, value in probabilities.items() if name != choice)
    if min(confidence, probabilities[choice]) >= 0.90 and probabilities[choice] - others >= 0.25:
        return choice
    return None


async def try_initial_action(
    ctx: ArtemisContext,
    state: State,
    goal: str,
    tools: list,
    controller: UnifiedMobileController,
) -> AIMessage | None:
    """Return a validated Settings action or let the regular Flash model act."""
    if len(goal) > 1200 or not (_SCROLL.search(goal) or _NAVIGATE.search(goal)):
        return None
    key = _api_key()
    if not key:
        return None
    elements = getattr(state, "indexed_elements", None)
    if not isinstance(elements, list):
        return None
    candidates = _candidates(elements, goal, {tool.name for tool in tools})
    choice = _exact_choice(goal, candidates)
    if not choice and len(candidates) < 2:
        return None
    original = _home_signature(elements)

    started = time.perf_counter()
    outcome = "fallback:foreground"
    metadata = None
    try:
        async with asyncio.timeout(_TIMEOUT):
            if await asyncio.to_thread(get_current_foreground_package, ctx) != _PACKAGE:
                return None
            used_model = choice is None
            if used_model:
                criteria = {option: action.description for option, action in candidates.items()}
                criteria[_OTHER] = "No candidate is the explicit next action; defer to Flash."
                async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                    response = await client.post(
                        _ENDPOINT,
                        headers={"x-goog-api-key": key},
                        json={
                            "model": _MODEL,
                            "input": goal,
                            "questions": {
                                "action": {
                                    "type": "choice",
                                    "instructions": (
                                        "Choose the next action on the current Android Settings "
                                        "homepage. Swipe up reveals lower rows; swipe down reveals "
                                        "earlier rows. Select only if the task explicitly asks "
                                        "for that action now. If another action comes first, the "
                                        "intention is unclear, or it asks to change a setting, "
                                        "choose other. Negative restrictions prohibit actions."
                                    ),
                                    "criteria": criteria,
                                },
                            },
                        },
                    )
                    response.raise_for_status()
                data = response.json()
                usage = data.get("usage")
                fields = {
                    "input_tokens": "total_input_tokens",
                    "output_tokens": "total_output_tokens",
                    "total_tokens": "total_tokens",
                }
                if isinstance(usage, dict) and all(
                    type(usage.get(field)) is int and usage[field] >= 0 for field in fields.values()
                ):
                    metadata = {name: usage[field] for name, field in fields.items()}
                    record_llm_usage(
                        ctx.data_engine,
                        AIMessage(content="", usage_metadata=metadata),
                        source=f"google:decisions:{_MODEL}",
                        update_last_prompt=False,
                    )
                choice = _selected_choice(data["answers"]["action"], criteria)
                if choice is None:
                    outcome = "fallback:choice"
                    return None

            action = candidates[choice]
            outcome = "fallback:page_changed"
            fresh_elements = await controller.get_ui_elements()
            if (
                getattr(state, "indexed_elements", None) is not elements
                or not _still_on_same_page(fresh_elements, original, choice, action)
                or await asyncio.to_thread(get_current_foreground_package, ctx) != _PACKAGE
            ):
                return None
            outcome = f"selected:{choice}" if used_model else f"local:{choice}"
            return action.message(metadata, source="decisions" if used_model else "local")
    except Exception as exc:
        # This optional path must never prevent the ordinary Flash invocation.
        outcome = f"fallback:{type(exc).__name__}"
        if isinstance(exc, httpx.HTTPStatusError):
            outcome = f"fallback:HTTP{exc.response.status_code}"
        return None
    finally:
        elapsed = time.perf_counter() - started
        # Persist the full optional-path duration, including revalidation and
        # failed attempts. A timeout must not disappear from A/B measurements.
        engine = getattr(ctx, "data_engine", None)
        if engine is not None:
            try:
                engine.record_trace(
                    type="decision",
                    name="flash_initial_action",
                    payload={"outcome": outcome, "model": _MODEL, "token_usage": metadata},
                    duration=elapsed,
                    step_id=getattr(engine, "current_step_id", None),
                    status="success" if outcome.startswith(("selected:", "local:")) else "fallback",
                )
            except Exception:
                pass  # Optional telemetry must not prevent Flash fallback.
        logger.info(
            f"Flash Decisions: {outcome} model={_MODEL} "
            f"elapsed_ms={round(elapsed * 1000)} "
            f"input_tokens={(metadata or {}).get('input_tokens', 'unknown')} "
            f"output_tokens={(metadata or {}).get('output_tokens', 'unknown')}"
        )
