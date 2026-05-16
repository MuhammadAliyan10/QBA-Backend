import asyncio
import sys
import os
import logging

# Path setup to find internal modules and generated protos
current_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.join(current_dir, "src")
# Add backend root to resolve 'api' module (protos)
backend_root = os.path.abspath(os.path.join(current_dir, "../../"))

sys.path.append(backend_root)
sys.path.append(src_dir)

from playwright.async_api import async_playwright
from activities.w3_solver import solve_w3_exercise
from core.user_facing_logger import UserFriendlyLogger
from activities.publish_activities import NervousSystem # Dummy for standalone

# Mock classes for standalone execution
class MockNervousSystem:
    async def publish(self, *args, **kwargs): pass
    async def publish_update(self, *args, **kwargs): pass

class MockLogger:
    async def info(self, type, message, **kwargs): 
        print(f"[{type}] {message}")
    async def error(self, type, message, **kwargs):
        print(f"[{type}] ERROR: {message}")

async def run_visual_demo():
    print("🚀 LAUNCHING QUANTA VISUAL DEMO MODE...")
    print("--- MISSION: SOLVE W3SCHOOLS HTML EXERCISE ---")
    
    async with async_playwright() as p:
        # Browser is launched with headless=False and slow_mo=800 (from navigation.py)
        # But we force it here for the standalone demo to be absolute.
        browser = await p.chromium.launch(headless=False, slow_mo=1000)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Initialize mocks
        user_logger = MockLogger()
        nervous_system = MockNervousSystem()
        
        try:
            result = await solve_w3_exercise(
                page, 
                "FYP2_DEMO_JOB", 
                user_logger, 
                nervous_system
            )
            print("\n--- MISSION ACCOMPLISHED ---")
            print(f"Answer: {result['answer']}")
            print("The evaluators should now see 'Correct!' on the screen.")
            
            # Keep browser open for 5 seconds for the video
            await asyncio.sleep(5)
            
        except Exception as e:
            print(f"Demo Failed: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(run_visual_demo())
