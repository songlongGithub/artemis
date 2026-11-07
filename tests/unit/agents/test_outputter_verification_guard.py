"""The canonical final report cannot overrule settled machine verdicts."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from artemis.agents.outputter.outputter import outputter
from artemis.config import OutputConfig
from artemis.graph.state import State


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["failed", "inconclusive", "unchecked", "blocked"])
@pytest.mark.parametrize("structured", [False, True])
async def test_unmet_checks_skip_model_and_replace_false_success(tmp_path, kind, structured):
    tests = dict(passed=7, failed=0, inconclusive=0, unchecked=0)
    if kind != "blocked":
        tests[kind] = 1
    outcome = {"task_status": "blocked" if kind == "blocked" else "completed", "tests": tests}
    (tmp_path / "run_outcome.json").write_text(json.dumps(outcome))
    ledger = (
        json.dumps(
            {
                "kind": "assert",
                "item_text": "weather close",
                "status": kind,
                "evidence": "location timeout",
            }
        )
        + "\n"
    )
    (tmp_path / "check_ledger.jsonl").write_text(ledger)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "output.md").write_text("SUCCESS: everything passed")
    (notes / "task_plan.md").write_text("- [x] Check weather\n  - [assert] weather close\n")
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    schema = {"type": "object", "properties": {"achieved": {"type": "boolean"}}}
    with patch("artemis.agents.outputter.outputter.get_llm") as get_llm:
        result = await outputter(
            ctx,
            OutputConfig(structured_output=schema if structured else None),
            State(initial_goal="verify"),
        )
    get_llm.assert_not_called()
    assert "FAILED" in result if kind == "failed" else "NOT VERIFIED" in result
    assert "SUCCESS" not in result
    assert "location timeout" in result
    assert f"{kind}=1" in result if kind != "blocked" else "task_status=blocked" in result
    assert (notes / "output.md").read_text() == result
    assert (tmp_path / "check_ledger.jsonl").read_text() == ledger


@pytest.mark.asyncio
async def test_graph_outcome_still_guards_when_no_persisted_outcome(tmp_path):
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    state = State(
        initial_goal="verify", run_outcome={"task_status": "completed", "tests": {"unchecked": 1}}
    )
    with patch("artemis.agents.outputter.outputter.get_llm") as get_llm:
        result = await outputter(ctx, OutputConfig(), state)
    assert "unchecked=1" in result
    get_llm.assert_not_called()
