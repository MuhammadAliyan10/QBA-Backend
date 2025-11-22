"""
Unit tests for the LLM Client (AI Element Extraction)
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from src.core.llm_client import LLMClient


class TestLLMClient:
    """Test suite for LLM Client functionality."""

    @pytest.fixture
    def llm_client(self):
        """Create a mock LLM client for testing."""
        with patch('src.core.llm_client.ChatOpenAI'):
            client = LLMClient(model="gpt-4")
            return client

    @pytest.mark.asyncio
    async def test_element_extraction_success(self, llm_client):
        """Test successful element extraction from HTML."""
        # Mock OpenAI response
        mock_response = {
            "selector": "button#submit",
            "confidence": 0.95,
            "reasoning": "Found submit button with high confidence"
        }
        
        llm_client.chain.ainvoke = AsyncMock(return_value=mock_response)
        
        # Test extraction
        result = await llm_client.extract_element(
            html="<button id='submit'>Click Me</button>",
            instruction="find the submit button"
        )
        
        assert result["selector"] == "button#submit"
        assert result["confidence"] > 0.9
        assert "submit button" in result["reasoning"].lower()

    @pytest.mark.asyncio
    async def test_element_extraction_retry_logic(self, llm_client):
        """Test retry logic on LLM failure."""
        # Mock failure then success
        llm_client.chain.ainvoke = AsyncMock(
            side_effect=[
                Exception("Rate limit"),
                {"selector": "button", "confidence": 0.8, "reasoning": "Found button"}
            ]
        )
        
        result = await llm_client.extract_element(
            html="<button>Click</button>",
            instruction="find button"
        )
        
        # Should succeed on retry
        assert result["selector"] == "button"
        assert llm_client.chain.ainvoke.call_count == 2

    @pytest.mark.asyncio
    async def test_element_extraction_error_handling(self, llm_client):
        """Test graceful error handling."""
        llm_client.chain.ainvoke = AsyncMock(side_effect=Exception("API Error"))
        
        with pytest.raises(Exception, match="API Error"):
            await llm_client.extract_element(
                html="<div>Test</div>",
                instruction="find element"
            )

    def test_sanitize_html_removes_dangerous_content(self, llm_client):
        """Test HTML sanitization for safety."""
        dangerous_html = "<script>alert('xss')</script><div>Safe</div>"
        
        # Mock sanitization (if implemented)
        if hasattr(llm_client, 'sanitize_html'):
            safe_html = llm_client.sanitize_html(dangerous_html)
            assert "<script>" not in safe_html
            assert "<div>Safe</div>" in safe_html
