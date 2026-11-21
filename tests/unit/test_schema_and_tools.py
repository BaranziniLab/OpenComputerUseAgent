import json
import re
from pathlib import Path

import pytest

from opencomputeruseagent import schema, tools

ROOT = Path(__file__).resolve().parents[2]


def test_twelve_tools_with_the_designed_annotations():
    advertised = tools.advertised()
    assert [t["name"] for t in advertised] == list(tools.TABLE)
    assert len(advertised) == 12
    by_name = {t["name"]: t for t in advertised}
    for name in ("list_apps", "get_app_state", "screen_capture", "setup_status"):
        assert by_name[name]["annotations"]["readOnlyHint"] is True
        assert by_name[name]["annotations"]["destructiveHint"] is False
    for name in ("click", "perform_secondary_action", "scroll", "drag", "type_text", "press_key", "set_value"):
        assert by_name[name]["annotations"]["readOnlyHint"] is False
        assert by_name[name]["annotations"]["destructiveHint"] is True
    setup = by_name["open_permission_setup"]["annotations"]
    assert setup["readOnlyHint"] is False and setup["destructiveHint"] is False


def test_descriptions_say_fallback_and_never_name_copilot():
    for tool in tools.advertised():
        assert tool["description"].startswith("Fallback desktop control (Open Computer Use Agent). ")
        assert "copilot" not in json.dumps(tool).lower()


def test_instructions_route_but_do_not_claim_to_be_copilot():
    text = tools.INSTRUCTIONS
    assert text.startswith("Open Computer Use Agent is a fallback for desktop control. It is not Biorouter Copilot.")
    for state in ("unsupported_environment", "computer_use_unsupported_environment"):
        assert state in text
    assert "Never use both in one task" in text


def test_advertised_schemas_equal_the_pin_apart_from_text():
    advertised = {t["name"]: t for t in tools.advertised()}
    contract = tools.load_contract()
    assert sorted(contract) == sorted(tools.HELPER_TOOLS)
    for name, pinned in contract.items():
        assert tools.normalized(advertised[name]["inputSchema"]) == tools.normalized(pinned)


def test_schema_pin_comparison():
    contract = tools.load_contract()
    listed = [{"name": n, "description": "anything", "inputSchema": s} for n, s in contract.items()]
    assert tools.schema_mismatches(listed, contract) == []
    listed[0] = dict(listed[0], inputSchema={"type": "object", "properties": {}})
    assert tools.schema_mismatches(listed, contract) == [f"changed {listed[0]['name']}"]
    assert tools.schema_mismatches(listed[1:] + [{"name": "query", "inputSchema": {}}], contract)[:1]


def test_contract_matches_biorouters_old_fixture_byte_for_byte_in_content():
    data = json.loads((ROOT / "src/opencomputeruseagent/contract/tools.json").read_text())
    assert len(data["tools"]) == 10


@pytest.mark.parametrize("name, arguments, expected", [
    ("click", {"app": "TextEdit", "element_index": "3"}, []),
    ("click", {}, ["missing required argument: app"]),
    ("click", {"app": "x", "click_count": 0}, ["click_count must be at least 1"]),
    ("click", {"app": "x", "click_count": True}, ["click_count must be an integer"]),
    ("click", {"app": "x", "nope": 1}, ["unknown argument: nope"]),
    ("click", {"app": "x", "mouse_button": "fourth"}, ["mouse_button must be one of: left, right, middle"]),
    ("scroll", {"app": "x", "element_index": "1", "direction": "down", "pages": 0}, ["pages must be greater than 0"]),
    ("get_app_state", {"app": "x", "text_limit": "max"}, []),
    ("get_app_state", {"app": "x", "text_limit": "huge"}, ["text_limit has an unsupported value"]),
    ("screen_capture", {"window_title": ""}, ["window_title must not be empty"]),
    ("type_text", {"app": "x", "text": 5}, ["text must be a string"]),
    ("drag", {"app": "x", "from_x": float("nan"), "from_y": 1, "to_x": 2, "to_y": 3}, ["from_x must be a number"]),
])
def test_argument_validation(name, arguments, expected):
    assert schema.errors(arguments, tools.load_contract()[name]) == expected
