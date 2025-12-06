"""
Seed initial recipes into Qdrant vector database.

Run this script after starting Qdrant to populate it with default workflows.
"""

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from core.RecipeManager import RecipeManager

def seed_recipes():
    """Seed default recipes into Qdrant."""
    mgr = RecipeManager()

    print("🌱 Seeding recipes into Qdrant...")

    # Recipe 1: GitHub Explorer
    mgr.save_recipe(
        name="github_explorer",
        description="Navigate to GitHub and explore repositories, browse issues and pull requests",
        steps=[
            {"action": "GOTO", "params": {"url": "https://github.com/microsoft/playwright"}},
            {"action": "CLICK", "params": {"intent": "issues tab"}},
            {"action": "TYPE", "params": {"intent": "search all issues", "text": "bug"}}
        ]
    )
    print("✅ Seeded: github_explorer")

    # Recipe 2: Wikipedia Search
    mgr.save_recipe(
        name="wikipedia_search",
        description="Search Wikipedia for information about any topic",
        steps=[
            {"action": "GOTO", "params": {"url": "https://en.wikipedia.org/wiki/Main_Page"}},
            {"action": "TYPE", "params": {"intent": "search", "text": "{search_term}"}},
            {"action": "CLICK", "params": {"intent": "search button"}}
        ]
    )
    print("✅ Seeded: wikipedia_search")

    # Recipe 3: Amazon Product Search
    mgr.save_recipe(
        name="amazon_scraper",
        description="Search for products on Amazon e-commerce site",
        steps=[
            {"action": "GOTO", "params": {"url": "https://amazon.com"}},
            {"action": "TYPE", "params": {"intent": "search box", "text": "{item}"}},
            {"action": "CLICK", "params": {"intent": "search submit"}}
        ]
    )
    print("✅ Seeded: amazon_scraper")

    # Recipe 4: HTTPBin Test (with Protocol Capture)
    mgr.save_recipe(
        name="httpbin_protocol_test",
        description="Test HTTP requests and capture API session for protocol replay",
        steps=[
            {
                "action": "LOGIN_AND_SNIFF",
                "params": {
                    "url": "https://httpbin.org/anything",
                    "target_domain": "httpbin.org",
                    "iterations": 5
                }
            }
        ]
    )
    print("✅ Seeded: httpbin_protocol_test")

    # Recipe 5: God Mode Test (Full System Test)
    mgr.save_recipe(
        name="god_mode_test",
        description="Comprehensive test of all system capabilities including brain, security, and network probing",
        steps=[
            {"action": "GOTO", "params": {"url": "https://en.wikipedia.org/wiki/Main_Page"}},
            {"action": "TYPE", "params": {"intent": "search", "text": "Singularity"}},
            {"action": "CLICK", "params": {"intent": "search button"}},
            {
                "action": "LOGIN_AND_SNIFF",
                "params": {
                    "url": "https://httpbin.org/post",
                    "target_domain": "httpbin.org"
                }
            }
        ]
    )
    print("✅ Seeded: god_mode_test")

    # List all recipes
    print("\n📋 All recipes in database:")
    recipes = mgr.list_recipes()
    for r in recipes:
        print(f"   - {r['name']}: {r['description'][:60]}...")

    print(f"\n✅ Seeding complete! ({len(recipes)} recipes)")

if __name__ == "__main__":
    seed_recipes()
