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

"""The structured verdict request must never end on a model turn (Gemini
rejects such requests with 400 "Requests ending with a model turn are not
supported"), which is the natural state after the loop's tool-free reply."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from artemis.agents.checker.checker import CheckReport, _structured_report
from artemis.llm.structured import StructuredOutputError


def _llm(report: CheckReport):
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm, structured


@pytest.mark.asyncio
async def test_structured_report_appends_user_turn_after_model_reply():
    llm, structured = _llm(CheckReport(verdicts=[]))
    messages = [SystemMessage(content="s"), HumanMessage(content="h"), AIMessage(content="done")]

    await _structured_report(llm, messages)

    sent = structured.ainvoke.call_args.args[0]
    assert isinstance(sent[-1], HumanMessage)
    assert sent[-2] is messages[-1]
    assert len(messages) == 3  # caller's list untouched
    llm.with_structured_output.assert_called_once_with(
        CheckReport.model_json_schema(), include_raw=True
    )


@pytest.mark.asyncio
async def test_structured_report_keeps_user_ending_conversation_as_is():
    llm, structured = _llm(CheckReport(verdicts=[]))
    messages = [SystemMessage(content="s"), HumanMessage(content="final iteration")]

    await _structured_report(llm, messages)

    assert structured.ainvoke.call_args.args[0] is messages


def _payload(status="passed"):
    return {
        "verdicts": [
            {
                "item_text": "exact item",
                "kind": "verify",
                "status": status,
                "evidence": "device record: value=7",
            }
        ],
        "unmet_subgoals": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["passed", "failed", "inconclusive"])
async def test_fenced_json_recovers_without_changing_verdict_or_reasking(status):
    raw = AIMessage(content="```json\n" + json.dumps(_payload(status)) + "\n```")
    llm, _ = _llm({"raw": raw, "parsed": None, "parsing_error": ValueError("fenced JSON")})
    with patch("artemis.agents.checker.checker.acomplete_structured") as correction:
        report = await _structured_report(llm, [HumanMessage(content="audit")])
    assert report.model_dump() == CheckReport.model_validate(_payload(status)).model_dump()
    correction.assert_not_called()


@pytest.mark.asyncio
async def test_provider_parsed_report_is_used_directly():
    report = CheckReport.model_validate(_payload("failed"))
    llm, _ = _llm({"raw": AIMessage(content=""), "parsed": report, "parsing_error": None})
    assert await _structured_report(llm, [HumanMessage(content="audit")]) is report


@pytest.mark.asyncio
async def test_provider_parsed_dict_is_validated_locally():
    llm, _ = _llm({"raw": AIMessage(content=""), "parsed": _payload("failed")})
    report = await _structured_report(llm, [HumanMessage(content="audit")])
    assert report.verdicts[0].status == "failed"


@pytest.mark.asyncio
async def test_invalid_schema_gets_one_corrective_turn_then_remains_error():
    raw = AIMessage(content='{"verdicts":[{"status":"passed"}]}')
    llm, _ = _llm({"raw": raw, "parsed": None, "parsing_error": ValueError("schema")})
    # Exercise the real bounded parser path, with a second malformed model answer.
    with patch("artemis.services.llm.acomplete", new=AsyncMock(return_value=raw)) as complete:
        with pytest.raises(StructuredOutputError, match="schema validation failed"):
            await _structured_report(llm, [HumanMessage(content="audit")])
    assert complete.await_count == 1
    sent = complete.await_args.args[1]
    assert isinstance(sent[-1], HumanMessage)
    assert '"required"' in sent[-1].content


@pytest.mark.asyncio
async def test_transport_cancellation_is_not_converted_into_a_verdict():
    llm, structured = _llm(None)
    structured.ainvoke.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _structured_report(llm, [HumanMessage(content="audit")])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_corrective_turn_flattens_unresolved_tool_calls_and_keeps_payload(invalid):
    call = {"name": "CheckReport", "id": "rejected-call", "args": {"unexpected": 7}}
    if invalid:
        raw = AIMessage(
            content="",
            invalid_tool_calls=[{**call, "args": '{"unexpected":', "error": "invalid JSON"}],
        )
    else:
        raw = AIMessage(content="", tool_calls=[call])
    llm, _ = _llm({"raw": raw, "parsed": None, "parsing_error": ValueError("schema")})
    corrected = AIMessage(content=json.dumps(_payload("failed")))
    with patch("artemis.services.llm.acomplete", new=AsyncMock(return_value=corrected)) as complete:
        report = await _structured_report(llm, [HumanMessage(content="audit")])
    assert complete.await_count == 1
    sent = complete.await_args.args[1]
    assistant = sent[-2]
    assert isinstance(assistant, AIMessage)
    assert not assistant.tool_calls and not assistant.invalid_tool_calls
    assert "unexpected" in assistant.content
    assert report.verdicts[0].status == "failed"


@pytest.mark.asyncio
async def test_cancellation_during_correction_propagates():
    raw = AIMessage(content="not JSON")
    llm, _ = _llm({"raw": raw, "parsed": None, "parsing_error": ValueError("schema")})
    with patch(
        "artemis.services.llm.acomplete", new=AsyncMock(side_effect=asyncio.CancelledError())
    ) as complete:
        with pytest.raises(asyncio.CancelledError):
            await _structured_report(llm, [HumanMessage(content="audit")])
    assert complete.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_openai_sdk_retains_fenced_report_before_pydantic_parsing(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-checker-test",
                "object": "chat.completion",
                "created": 1,
                "model": "compatible-test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "```json\n" + json.dumps(_payload(status)) + "\n```",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(
            model="compatible-test-model",
            api_key="test-only",
            base_url="https://example.invalid/v1",
            http_async_client=client,
            max_retries=0,
        )
        report = await _structured_report(llm, [HumanMessage(content="audit exact item")])

    assert len(requests) == 1
    assert report.model_dump() == CheckReport.model_validate(_payload(status)).model_dump()
