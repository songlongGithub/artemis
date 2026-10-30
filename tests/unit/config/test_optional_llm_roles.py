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

"""Ensure optional Pro judges honor explicit compatible-provider overrides."""

import pytest

from artemis.config.llm import LLMConfig, _expand_default_into_nodes


@pytest.mark.parametrize(
    "role",
    ["planner_validation", "validator_pixel_safety_net", "history_analyzer", "output_analyzer"],
)
def test_optional_role_explicit_override_inherits_provider(role):
    config = {
        "default": {
            "provider": "openai",
            "model": "proxy-primary",
            "fallback": {"provider": "openai", "model": "proxy-fallback"},
        },
        "nodes": {role: {"model": "proxy-judge"}},
    }
    resolved = LLMConfig.model_validate(_expand_default_into_nodes(config)).get_agent(role)
    assert resolved.provider == "openai"
    assert resolved.model == "proxy-judge"
    assert resolved.fallback.model == "proxy-fallback"
    assert resolved.fallback.provider == "openai"


@pytest.mark.parametrize(
    "role",
    ["planner_validation", "validator_pixel_safety_net", "history_analyzer", "output_analyzer"],
)
def test_unspecified_optional_role_keeps_factory_default(role):
    config = {
        "default": {
            "provider": "google",
            "model": "default-model",
            "fallback": {"provider": "google", "model": "fallback-model"},
        },
        "nodes": {},
    }
    resolved = LLMConfig.model_validate(_expand_default_into_nodes(config))
    assert getattr(resolved, role) is None
    assert resolved.get_agent(role).provider == "google"
