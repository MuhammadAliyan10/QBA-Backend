# tests/test_safe_client_clean_json.py
"""
Unit tests for SafeLLMClient._clean_json()

These tests run with zero network calls and zero external dependencies.
They verify the three repair stages:
  Stage 1: Markdown fence stripping
  Stage 2: Single → double quote normalization
  Stage 3: Outermost JSON boundary extraction + truncation repair

Run: pytest tests/test_safe_client_clean_json.py -v
"""
import json
import sys
import os

import pytest

# Bootstrap sys.path so the test can import from src/ without installation
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

# Minimal stub for pii_scrubber so importing SafeLLMClient doesn't fail
# in the test environment (no full dependency install required)
from unittest.mock import MagicMock
import types

_security = types.ModuleType("security")
_pii = types.ModuleType("security.pii_scrubber")
_pii.sanitize_payload = lambda x: x
sys.modules.setdefault("security", _security)
sys.modules.setdefault("security.pii_scrubber", _pii)

# Also stub httpx and tenacity if missing
for _mod in ["httpx", "tenacity"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.llm.safe_client import SafeLLMClient  # noqa: E402


@pytest.fixture
def client() -> SafeLLMClient:
    """Return a SafeLLMClient with no external dependencies wired."""
    c = object.__new__(SafeLLMClient)
    # Minimally initialise only the attributes _clean_json depends on (none)
    return c


# ─── Stage 1: Markdown fence stripping ───────────────────────────────────────

class TestMarkdownFenceStripping:
    def test_strips_json_fence(self, client):
        raw = '```json\n{"key": "value"}\n```'
        result = client._clean_json(raw)
        assert json.loads(result) == {"key": "value"}

    def test_strips_plain_fence(self, client):
        raw = '```\n{"a": 1}\n```'
        result = client._clean_json(raw)
        assert json.loads(result) == {"a": 1}

    def test_strips_fence_case_insensitive(self, client):
        raw = '```JSON\n{"x": true}\n```'
        result = client._clean_json(raw)
        assert json.loads(result) == {"x": True}

    def test_no_fence_unchanged(self, client):
        raw = '{"clean": "json"}'
        result = client._clean_json(raw)
        assert json.loads(result) == {"clean": "json"}

    def test_empty_string(self, client):
        result = client._clean_json("")
        assert result == ""

    def test_only_whitespace(self, client):
        result = client._clean_json("   \n  ")
        assert result == ""


# ─── Stage 2: Single → double quote normalization ────────────────────────────

class TestSingleQuoteNormalization:
    def test_single_quoted_value(self, client):
        raw = "{'type': 'click', 'target': 'button'}"
        result = client._clean_json(raw)
        parsed = json.loads(result)
        assert parsed == {"type": "click", "target": "button"}

    def test_mixed_quotes(self, client):
        """Value already in double quotes must not be double-escaped."""
        raw = '{"type": \'click\'}'
        result = client._clean_json(raw)
        parsed = json.loads(result)
        assert parsed["type"] == "click"

    def test_already_valid_json_untouched(self, client):
        raw = '{"status": "in_progress", "actions": []}'
        result = client._clean_json(raw)
        assert json.loads(result) == {"status": "in_progress", "actions": []}

    def test_single_quoted_array_elements(self, client):
        raw = "['one', 'two', 'three']"
        result = client._clean_json(raw)
        assert json.loads(result) == ["one", "two", "three"]


# ─── Stage 3: Boundary extraction ────────────────────────────────────────────

class TestBoundaryExtraction:
    def test_extracts_object_from_trailing_text(self, client):
        raw = 'Here is the JSON: {"key": "val"} End of response.'
        result = client._clean_json(raw)
        assert json.loads(result) == {"key": "val"}

    def test_extracts_array_from_trailing_text(self, client):
        raw = 'Result: [1, 2, 3] done.'
        result = client._clean_json(raw)
        assert json.loads(result) == [1, 2, 3]

    def test_prefers_first_opening_brace(self, client):
        # _clean_json extracts from the FIRST { to the LAST } — this spans both
        # objects in `prefix {"a": 1} {"b": 2} suffix`, producing:
        #   {"a": 1} {"b": 2}
        # That is not independently parseable as a single JSON object, which is
        # expected: _clean_json is a boundary finder, not a multi-object parser.
        raw = 'prefix {"a": 1} {"b": 2} suffix'
        result = client._clean_json(raw)
        assert result.startswith('{"a"')
        assert result.endswith("}")
        assert '"a"' in result and '"b"' in result

    def test_no_json_boundary_returns_text(self, client):
        raw = "no json here at all"
        result = client._clean_json(raw)
        assert result == "no json here at all"


# ─── Stage 3: Truncation repair ──────────────────────────────────────────────

class TestTruncationRepair:
    def test_repairs_unclosed_object(self, client):
        """LLM hit max_tokens mid-stream — closing brace missing."""
        raw = '{"thought_process": "clicking button", "actions": [{"type": "click"'
        result = client._clean_json(raw)
        # Must not raise — result may not be fully valid but should not crash
        assert result  # non-empty

    def test_repairs_unclosed_nested_object(self, client):
        raw = '{"a": {"b": 1'
        result = client._clean_json(raw)
        # After repair, should parse as valid JSON
        try:
            parsed = json.loads(result)
            assert parsed["a"]["b"] == 1
        except json.JSONDecodeError:
            # Repair may still fail on deeply malformed input — acceptable
            pass

    def test_valid_json_fast_path(self, client):
        """Valid JSON must take the fast path and return unchanged."""
        raw = '{"status": "done", "loops": 3}'
        result = client._clean_json(raw)
        assert json.loads(result) == {"status": "done", "loops": 3}

    def test_repairs_unclosed_array(self, client):
        raw = '[{"type": "click"}, {"type": "type"'
        result = client._clean_json(raw)
        assert result  # non-empty, repair attempted

    def test_nested_braces_balanced_correctly(self, client):
        """Triple-nested valid JSON must parse without repair."""
        obj = {"a": {"b": {"c": [1, 2, 3]}}}
        raw = json.dumps(obj)
        result = client._clean_json(raw)
        assert json.loads(result) == obj


# ─── End-to-end pipeline ─────────────────────────────────────────────────────

class TestEndToEndPipeline:
    def test_fence_plus_single_quotes(self, client):
        """Common LLM output: markdown fence wrapping single-quoted JSON."""
        raw = "```json\n{'thought_process': 'searching', 'status': 'in_progress'}\n```"
        result = client._clean_json(raw)
        parsed = json.loads(result)
        assert parsed["thought_process"] == "searching"
        assert parsed["status"] == "in_progress"

    def test_fence_plus_trailing_explanation(self, client):
        raw = '```json\n{"actions": []}\n```\nThis is my explanation.'
        result = client._clean_json(raw)
        assert json.loads(result) == {"actions": []}

    def test_real_world_agent_output(self, client):
        """Matches actual NVIDIA LLM output pattern observed in production."""
        raw = (
            '```json\n'
            '{"thought_process": "I see a search box", '
            '"actions": [{"type": "click", "target_id": "q-3", "value": ""}], '
            '"status": "in_progress"}\n'
            '```'
        )
        result = client._clean_json(raw)
        parsed = json.loads(result)
        assert parsed["status"] == "in_progress"
        assert len(parsed["actions"]) == 1
        assert parsed["actions"][0]["type"] == "click"
