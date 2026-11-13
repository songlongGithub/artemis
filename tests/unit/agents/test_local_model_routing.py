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

"""Personal-fork auxiliary models must keep their configured provider."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.memory.chunking import HistoryChunkManager, StepCapsuleLens


def context(provider):
    return SimpleNamespace(
        data_engine=None,
        llm_config=SimpleNamespace(operator=SimpleNamespace(provider=provider)),
    )


def build(which, ctx):
    if which == "flash":
        return VisualStepSummarizer(ctx, model_name="auxiliary")._llm
    lens = StepCapsuleLens("auxiliary", ctx=ctx, fallback_model_name="fallback")
    return lens._get_llm() if which == "capsule" else lens._get_fallback_llm()


@pytest.mark.parametrize("which", ["flash", "capsule", "fallback"])
@pytest.mark.parametrize("provider", ["google", "gemini", "openai", "anthropic"])
def test_explicit_auxiliary_model_keeps_provider(which, provider):
    marker = object()
    google = Mock(return_value=marker)
    with (
        patch("artemis.agents.flash.summarizer.get_google_llm", google),
        patch("artemis.services.llm.get_google_llm", google),
        patch("artemis.llm.router.ModelFactory.get_model", return_value=marker) as factory,
    ):
        assert build(which, context(provider)) is marker
    model = "fallback" if which == "fallback" else "auxiliary"
    if provider in {"google", "gemini"}:
        google.assert_called_once_with(model_name=model, temperature=0.0)
        factory.assert_not_called()
    else:
        google.assert_not_called()
        endpoint = factory.call_args.args[0]
        assert endpoint.provider.value == provider
        assert endpoint.model_name == model and endpoint.timeout_seconds == 120.0


@pytest.mark.parametrize(
    ("which", "effort"), [("flash", "max"), ("capsule", "max"), ("fallback", None)]
)
def test_auxiliary_model_inherits_effort_from_matching_route(which, effort):
    ctx = context("openai")
    ctx.llm_config.summarizer = SimpleNamespace(
        provider="openai",
        model="auxiliary",
        reasoning_effort="max",
        fallback=SimpleNamespace(provider="openai", model="fallback"),
    )
    with patch("artemis.llm.router.ModelFactory.get_model", return_value=object()) as factory:
        build(which, ctx)
    assert factory.call_args.args[0].reasoning_effort == effort


@pytest.mark.parametrize("which", ["flash", "capsule", "fallback"])
def test_compatible_endpoint_failure_never_switches_to_google(which):
    with (
        patch("artemis.llm.router.ModelFactory.get_model", side_effect=RuntimeError("unavailable")),
        patch("artemis.agents.flash.summarizer.get_google_llm") as flash_google,
        patch("artemis.services.llm.get_google_llm") as google,
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            build(which, context("openai"))
    flash_google.assert_not_called()
    google.assert_not_called()


@pytest.mark.parametrize("which", ["flash", "capsule", "fallback"])
def test_unknown_explicit_provider_is_not_hidden(which):
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build(which, context("not-a-provider"))


def test_capsule_without_context_uses_configured_default():
    with (
        patch(
            "artemis.config.llm.get_default_llm_config", return_value=context("openai").llm_config
        ),
        patch("artemis.llm.router.ModelFactory.get_model", return_value=object()) as factory,
    ):
        StepCapsuleLens("auxiliary")._get_llm()
    assert factory.call_args.args[0].provider.value == "openai"


def test_default_flash_summarizer_uses_agent_role():
    ctx = context("openai")
    with patch("artemis.agents.flash.summarizer.get_llm", return_value=object()) as get:
        VisualStepSummarizer(ctx)
    get.assert_called_once_with(ctx, name="summarizer")


def test_injected_capsule_models_are_preserved():
    primary, fallback = object(), object()
    lens = StepCapsuleLens(
        "primary", llm=primary, fallback_llm=fallback, fallback_model_name="other"
    )
    assert lens._get_llm() is primary and lens._get_fallback_llm() is fallback


def test_capsule_fallback_accepts_only_the_same_provider():
    manager = HistoryChunkManager(chunking_config=SimpleNamespace(model="primary"))
    ctx = context("openai")
    ctx.llm_config.summarizer = SimpleNamespace(
        fallback=SimpleNamespace(provider="openai", model="secondary")
    )
    assert manager._resolve_capsule_fallback_model(ctx) == "secondary"
    ctx.llm_config.summarizer.fallback.provider = "google"
    assert manager._resolve_capsule_fallback_model(ctx) is None
    ctx.llm_config.summarizer.fallback = SimpleNamespace(provider="openai", model="primary")
    assert manager._resolve_capsule_fallback_model(ctx) is None


@pytest.mark.parametrize("which", ["visual", "capsule"])
def test_flash_summary_effort_override_preserves_shared_pro_route(which):
    ctx = context("openai")
    ctx.llm_config.summarizer = SimpleNamespace(
        provider="openai", model="auxiliary", reasoning_effort="max"
    )
    with patch("artemis.llm.router.ModelFactory.get_model", return_value=object()) as factory:
        if which == "visual":
            VisualStepSummarizer(ctx, model_name="auxiliary", reasoning_effort="medium")
        else:
            StepCapsuleLens("auxiliary", ctx=ctx, reasoning_effort="medium")._get_llm()
        assert factory.call_args.args[0].reasoning_effort == "medium"
        # Pro creates the same service with no Flash override and still gets max.
        if which == "visual":
            VisualStepSummarizer(ctx, model_name="auxiliary")
        else:
            StepCapsuleLens("auxiliary", ctx=ctx)._get_llm()
        assert factory.call_args.args[0].reasoning_effort == "max"
    assert ctx.llm_config.summarizer.reasoning_effort == "max"


def test_flash_capsule_effort_does_not_override_gemini_fallback():
    ctx = context("openai")
    ctx.llm_config.summarizer = SimpleNamespace(
        provider="openai",
        model="auxiliary",
        reasoning_effort="max",
        fallback=SimpleNamespace(provider="openai", model="gemini-backup", reasoning_effort=None),
    )
    lens = StepCapsuleLens(
        "auxiliary", ctx=ctx, reasoning_effort="medium", fallback_model_name="gemini-backup"
    )
    with patch("artemis.llm.router.ModelFactory.get_model", return_value=object()) as factory:
        lens._get_fallback_llm()
    assert factory.call_args.args[0].reasoning_effort is None


def test_flash_and_pro_composition_roots_keep_independent_summary_effort():
    from artemis.agents.flash.runner import FlashRunner
    from artemis.config import load_agent_config
    from artemis.memory import ensure_step_memory

    cfg = load_agent_config()
    cfg.flash.step_summarizer.reasoning_effort = "medium"
    ctx = context("openai")
    ctx.driver = Mock()
    ctx.step_memory = None
    with (
        patch("artemis.agents.flash.runner.load_agent_config", return_value=cfg),
        patch("artemis.controllers.unified_controller.get_driver"),
        patch("artemis.agents.flash.runner.VisualStepSummarizer") as visual,
        patch("artemis.memory.HistoryChunkManager") as chunker,
    ):
        runner = FlashRunner(ctx, goal="Scroll once")
        runner.ctx.data_engine = SimpleNamespace(session_start_time=0)
        runner._build_ledger()
    assert visual.call_args.kwargs["reasoning_effort"] == "medium"
    assert chunker.call_args.kwargs["summarizer_reasoning_effort"] == "medium"

    ctx = context("openai")
    with (
        patch("artemis.config.load_agent_config", return_value=cfg),
        patch("artemis.agents.flash.summarizer.VisualStepSummarizer") as visual,
    ):
        ensure_step_memory(ctx)
    assert "reasoning_effort" not in visual.call_args.kwargs


def test_history_chunk_manager_forwards_flash_override():
    with patch("artemis.memory.chunking.StepCapsuleLens") as lens:
        HistoryChunkManager(ctx=context("openai"), summarizer_reasoning_effort="medium")
    assert lens.call_args.kwargs["reasoning_effort"] == "medium"
