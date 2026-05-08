import json
import asyncio
from unittest.mock import MagicMock

async def test_sanitizer():
    # 1. Mock a Playwright Response
    mock_response = MagicMock()
    mock_response.url = "https://www.facebook.com/api/graphql/"
    mock_response.status = 200
    mock_response.headers = {"content-type": "text/javascript; charset=utf-8"}
    
    # The Hijacking Payload
    hijacked_payload = 'for (;;);{"data": {"viewer": {"name": "Test User", "feed": {"posts": [{"text": "Hello World", "time": 123456789}]}}}}'
    
    # Mock text() and json() methods
    async def mock_text():
        return hijacked_payload
    
    async def mock_json():
        # This should fail in real life if the prefix is present
        raise Exception("JSON parse error")
        
    mock_response.text = mock_text
    mock_response.json = mock_json
    mock_response.request.resource_type = "xhr"
    mock_response.request.method = "POST"

    # 2. Import Sniffer
    import sys
    sys.path.insert(0, "src")
    from core.network_sniffer import NetworkSniffer
    
    sniffer = NetworkSniffer(target_domain="facebook.com")
    
    # 3. Simulate event
    print(f"Simulating interception of Meta GraphQL payload...")
    await sniffer._handle_response(mock_response)
    
    # 4. Verify results
    if sniffer.captured_responses:
        captured = sniffer.captured_responses[0]
        print(f"\n✅ SUCCESS: Payload Intercepted and Sanitized!")
        print(f"URL: {captured['url']}")
        print(f"Data: {json.dumps(captured['data'], indent=2)}")
    else:
        print("\n❌ FAILURE: Payload not captured.")

if __name__ == "__main__":
    asyncio.run(test_sanitizer())
