from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from ticketgate.config import Settings
from ticketgate.llm import FakeChatModel, build_llms
from ticketgate.models import TriageResult
from ticketgate.tools import check_patch

DEMO_TARGET = Path(__file__).parents[2] / "examples" / "demo-target"


@tool
def submit_investigation(summary: str) -> str:
    """Submit."""
    return summary


@tool
def search_code(query: str) -> str:
    """Search."""
    return query


def _triage(text: str) -> TriageResult:
    runnable = FakeChatModel().with_structured_output(TriageResult, method="json_schema")
    result = runnable.invoke([HumanMessage(content=text)])
    assert isinstance(result, TriageResult)
    return result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How do I change the default currency?", "question"),
        ("Bump the pinned linter version", "chore"),
        ("Add CSV export for invoices", "feature"),
        ("Invoice total is wrong when a discount is applied", "bug"),
    ],
)
def test_fake_triage_classifies_by_keyword(text: str, expected: str) -> None:
    assert _triage(text).type == expected


def test_fake_is_deterministic() -> None:
    assert _triage("Add CSV export") == _triage("Add CSV export")


def _investigator() -> Any:
    return FakeChatModel().bind_tools([search_code, submit_investigation])


def _ticket(text: str = "Invoice total is wrong when a discount is applied") -> list[BaseMessage]:
    return [SystemMessage(content="system"), HumanMessage(content=text)]


def test_fake_investigator_searches_first() -> None:
    response = _investigator().invoke(_ticket())
    assert response.tool_calls[0]["name"] == "search_code"
    assert response.tool_calls[0]["args"] == {"query": "discount"}


def _after_tool(messages: list[BaseMessage], name: str, content: str) -> list[BaseMessage]:
    call = {"name": name, "args": {}, "id": f"call-{len(messages)}", "type": "tool_call"}
    return [
        *messages,
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content=content, tool_call_id=call["id"]),
    ]


def test_fake_investigator_reads_the_first_search_hit() -> None:
    messages = _after_tool(_ticket(), "search_code", "src/invoicing/discounts.py:4: def apply")
    response = _investigator().invoke(messages)
    assert response.tool_calls[0]["name"] == "read_file"
    assert response.tool_calls[0]["args"] == {"path": "src/invoicing/discounts.py"}


def test_fake_investigator_submits_a_patch_that_applies_to_the_demo_target() -> None:
    messages = _after_tool(_ticket(), "search_code", "src/invoicing/discounts.py:4: def apply")
    messages = _after_tool(messages, "read_file", "1: code")
    response = _investigator().invoke(messages)
    call = response.tool_calls[0]
    assert call["name"] == "submit_investigation"
    assert call["args"]["plan"]
    assert check_patch(DEMO_TARGET, call["args"]["diff"]) is True


def test_fake_investigator_submits_a_plan_without_a_patch_when_it_has_none() -> None:
    messages = _ticket("Add CSV export for invoices")
    messages = _after_tool(messages, "search_code", "No matches.")
    messages = _after_tool(messages, "list_files", "src/invoicing/invoice.py")
    response = _investigator().invoke(messages)
    call = response.tool_calls[0]
    assert call["name"] == "submit_investigation"
    assert call["args"]["diff"] is None


def test_fake_without_tools_writes_plain_text() -> None:
    response = FakeChatModel().invoke(_ticket("How do I change the default currency?"))
    assert isinstance(response.content, str)
    assert response.content
    assert not response.tool_calls


def test_fake_mode_builds_without_aws() -> None:
    llms = build_llms(Settings(llm_mode="fake", _env_file=None))  # type: ignore[call-arg]
    assert isinstance(llms.principal, FakeChatModel)
    assert isinstance(llms.worker, FakeChatModel)


def _stubbed_client() -> tuple[Any, Stubber, list[dict[str, Any]]]:
    client = boto3.client(
        "bedrock-runtime",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    requests: list[dict[str, Any]] = []
    client.meta.events.register(
        "provide-client-params.bedrock-runtime.Converse",
        lambda params, **_: requests.append(params),
    )
    return client, Stubber(client), requests


def _converse_text(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


def _bedrock_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_mode="bedrock",
        bedrock_model_id="us.anthropic.claude-sonnet-5-5",
        bedrock_worker_model_id="us.anthropic.claude-haiku-5-5",
        aws_region="us-west-2",
    )


def test_bedrock_models_use_the_configured_ids() -> None:
    client, stubber, requests = _stubbed_client()
    llms = build_llms(_bedrock_settings(), client=client)
    stubber.add_response("converse", _converse_text("one"))
    stubber.add_response("converse", _converse_text("two"))
    with stubber:
        llms.principal.invoke("hi")
        llms.worker.invoke("hi")
    assert [r["modelId"] for r in requests] == [
        "us.anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-haiku-5-5",
    ]


def test_bedrock_triage_uses_json_schema_output_not_a_forced_tool() -> None:
    # The Claude 5.5 cards document limits on tool_choice, so triage does not rely on it.
    client, stubber, requests = _stubbed_client()
    llms = build_llms(_bedrock_settings(), client=client)
    payload = {"type": "bug", "severity": "high", "component": "Billing", "rationale": "r"}
    stubber.add_response("converse", _converse_text(json.dumps(payload)))
    with stubber:
        result = llms.worker.with_structured_output(TriageResult, method="json_schema").invoke("x")
    assert isinstance(result, TriageResult)
    assert result.component == "billing"
    request = requests[0]
    assert "toolConfig" not in request
    schema = json.loads(request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
    assert schema["additionalProperties"] is False


def test_bedrock_tool_loop_uses_auto_tool_choice_for_sonnet() -> None:
    # Sonnet 5.5 accepts only auto and none, so a forced choice would be a 400 in production.
    client, stubber, requests = _stubbed_client()
    llms = build_llms(_bedrock_settings(), client=client)
    stubber.add_response("converse", _converse_text("done"))
    with stubber:
        llms.principal.bind_tools([search_code]).invoke("x")
    tool_config = requests[0]["toolConfig"]
    assert tool_config["tools"][0]["toolSpec"]["name"] == "search_code"
    assert "any" not in tool_config.get("toolChoice", {})
    assert "tool" not in tool_config.get("toolChoice", {})


def test_bedrock_requests_do_not_send_temperature() -> None:
    client, stubber, requests = _stubbed_client()
    llms = build_llms(_bedrock_settings(), client=client)
    stubber.add_response("converse", _converse_text("ok"))
    with stubber:
        llms.principal.invoke("x")
    assert "temperature" not in requests[0].get("inferenceConfig", {})


def test_read_timeout_allows_for_first_time_schema_compilation() -> None:
    # AWS docs: compiling a new schema can take ~90s, so use a read timeout of at least 120s.
    llms = build_llms(_bedrock_settings())
    for llm in (llms.principal, llms.worker):
        assert isinstance(llm, ChatBedrockConverse)
        assert llm.client.meta.config.read_timeout >= 120


def test_default_model_ids_are_the_documented_inference_profiles() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.bedrock_model_id == "us.anthropic.claude-sonnet-5-5"
    assert settings.bedrock_worker_model_id == "us.anthropic.claude-haiku-5-5"
    assert settings.llm_mode == "fake"
