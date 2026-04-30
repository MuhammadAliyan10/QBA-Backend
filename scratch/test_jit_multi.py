# scratch/test_jit_multi.py
import asyncio
import logging
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "apps/execution-plane/src"))

from core.planning.sightedPipeline import SightedPipeline

logging.basicConfig(level=logging.INFO)

async def main():
    pipeline = SightedPipeline()
    # Test case: Navigate to a site, perform a search, and verify transition to results
    # We use a simple site like Wikipedia or a public search engine for reliability in tests
    url = "https://www.wikipedia.org/"
    objective = "Search for 'Artificial Intelligence' on Wikipedia and extract the first paragraph of the result page."
    
    print(f"\n--- STARTING JIT TEST ---")
    print(f"URL: {url}")
    print(f"Objective: {objective}\n")
    
    result = await pipeline.run(
        url=url,
        objective=objective,
        headless=True
    )
    
    print(f"\n--- TEST RESULT ---")
    print(f"Success: {result.success}")
    print(f"Status: {result.status}")
    print(f"Epochs Run: {result.epochs_run}")
    print(f"Extracted Data: {result.extracted_data}")
    if result.error:
        print(f"Error: {result.error}")

if __name__ == "__main__":
    asyncio.run(main())
