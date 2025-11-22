"""
Unit tests for SmartFinder (Fallback Strategy: Sniper -> Compressor -> LLM)
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from src.core.smart_finder import SmartFinder


class TestSmartFinder:
    """Test suite for SmartFinder with fallback strategy."""

    @pytest.fixture
    def smart_finder(self):
        """Create SmartFinder instance for testing."""
        return SmartFinder()

    @pytest.mark.asyncio
    async def test_fallback_strategy_sniper_success(self, smart_finder):
        """Test that Sniper (exact selector) is tried first."""
        mock_page = Mock()
        mock_page.query_selector = AsyncMock(return_value=Mock())  # Element found
        
        result = await smart_finder.find_element(
            page=mock_page,
            instruction="button#submit",
            use_exact_selector=True
        )
        
        assert result is not None
        assert smart_finder.last_strategy == "sniper"

    @pytest.mark.asyncio
    async def test_fallback_strategy_compressor_fallback(self, smart_finder):
        """Test fallback to Compressor when Sniper fails."""
        mock_page = Mock()
        mock_page.query_selector = AsyncMock(return_value=None)  # Sniper fails
        
        with patch.object(smart_finder, '_try_compressor', return_value=Mock()):
            result = await smart_finder.find_element(
                page=mock_page,
                instruction="find the submit button"
            )
            
            assert result is not None
            assert smart_finder.last_strategy == "compressor"

    @pytest.mark.asyncio
    async def test_fallback_strategy_llm_final_attempt(self, smart_finder):
        """Test fallback to LLM when both Sniper and Compressor fail."""
        mock_page = Mock()
        mock_page.query_selector = AsyncMock(return_value=None)
        
        with patch.object(smart_finder, '_try_compressor', return_value=None):
            with patch.object(smart_finder, '_try_llm', return_value=Mock()):
                result = await smart_finder.find_element(
                    page=mock_page,
                    instruction="find the submit button"
                )
                
                assert result is not None
                assert smart_finder.last_strategy == "llm"

    @pytest.mark.asyncio
    async def test_caching_avoids_redundant_llm_calls(self, smart_finder):
        """Test that successful LLM results are cached."""
        mock_page = Mock()
        mock_page.query_selector = AsyncMock(return_value=Mock())
        
        with patch.object(smart_finder, '_try_llm', return_value=Mock()) as mock_llm:
            # First call
            await smart_finder.find_element(mock_page, "find button")
            first_call_count = mock_llm.call_count
            
            # Second call with same instruction (should use cache)
            await smart_finder.find_element(mock_page, "find button")
            
            # LLM should not be called again
            assert mock_llm.call_count == first_call_count

    @pytest.mark.asyncio
    async def test_selector_validation(self, smart_finder):
        """Test that invalid selectors are rejected."""
        mock_page = Mock()
        
        with pytest.raises(ValueError, match="Invalid selector"):
            await smart_finder.find_element(
                page=mock_page,
                instruction="",  # Empty instruction
                use_exact_selector=True
            )
