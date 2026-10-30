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
