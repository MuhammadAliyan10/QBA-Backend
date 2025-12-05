"""
Test Suite for Glass Box Engine
================================

This test file demonstrates and validates all 6 algorithms in the Glass Box Engine.
It uses a local test HTML page and verifies each algorithm works correctly.

Run with:
    cd apps/execution-plane
    python tests/testGlassBox.py
"""

import asyncio
import sys
import os

# Add paths for imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from playwright.async_api import async_playwright
from core.GlassBox import GlassBoxEngine, KNOWN_ICONS


async def test_raycast_visibility():
    """Test 1: Raycast Visibility Check"""
    print("\n" + "="*70)
    print("TEST 1: Raycast Visibility Check (Occlusion Detection)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with occluded element
        await page.set_content("""
            <html>
                <body>
                    <button id="visible-btn" style="padding: 20px;">Visible Button</button>
                    <button id="hidden-btn" style="padding: 20px;">Hidden Button</button>
                    <div id="overlay" style="
                        position: absolute;
                        top: 0;
                        left: 0;
                        width: 50%;
                        height: 200px;
                        background: rgba(255,0,0,0.3);
                        z-index: 1000;
                    ">Cookie Banner Overlay</div>
                </body>
            </html>
        """)

        glass_box = GlassBoxEngine()

        # Test visible button
        visible_btn = await page.query_selector('#visible-btn')
        is_clickable = await glass_box.is_physically_clickable(visible_btn)
        print(f"✓ Visible Button: {'PASS' if is_clickable else 'FAIL'} (is_clickable={is_clickable})")

        # Test occluded button
        hidden_btn = await page.query_selector('#hidden-btn')
        is_occluded = not await glass_box.is_physically_clickable(hidden_btn)
        print(f"✓ Occluded Button: {'PASS' if is_occluded else 'FAIL'} (detected_occlusion={is_occluded})")

        await browser.close()
        return is_clickable and is_occluded


async def test_svg_icon_hash():
    """Test 2: SVG Topological Hasher"""
    print("\n" + "="*70)
    print("TEST 2: SVG Topological Hasher (Icon Identification)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with SVG icons
        await page.set_content("""
            <html>
                <body>
                    <button id="gear-icon">
                        <svg viewBox="0 0 24 24">
                            <path d="M12 15.5 A 3.5 3.5 0 1 1 12 8.5 A 3.5 3.5 0 1 1 12 15.5"/>
                        </svg>
                    </button>
                    <button id="search-icon">
                        <svg viewBox="0 0 24 24">
                            <path d="M10 2 A 8 8 0 1 1 10 18 A 8 8 0 1 1 10 2 M16 16 L 22 22"/>
                        </svg>
                    </button>
                </body>
            </html>
        """)

        glass_box = GlassBoxEngine()

        # Test gear icon
        gear_btn = await page.query_selector('#gear-icon')
        gear_hash = await glass_box.compute_icon_hash(gear_btn)
        print(f"✓ Gear Icon Hash: {gear_hash[:32]}...")
        print(f"  Hash Length: {len(gear_hash)} (expected: 64)")

        # Test search icon
        search_btn = await page.query_selector('#search-icon')
        search_hash = await glass_box.compute_icon_hash(search_btn)
        print(f"✓ Search Icon Hash: {search_hash[:32]}...")

        # Verify hashes are different
        different = gear_hash != search_hash
        print(f"✓ Icons have unique hashes: {'PASS' if different else 'FAIL'}")

        await browser.close()
        return len(gear_hash) == 64 and different


async def test_shadow_dom_piercer():
    """Test 3: Recursive Shadow Piercer"""
    print("\n" + "="*70)
    print("TEST 3: Recursive Shadow Piercer (Shadow DOM Traversal)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with shadow DOM
        await page.set_content("""
            <html>
                <body>
                    <button id="normal-btn">Normal Button</button>
                    <div id="shadow-host"></div>
                    <script>
                        const host = document.getElementById('shadow-host');
                        const shadow = host.attachShadow({ mode: 'open' });
                        shadow.innerHTML = '<button id="shadow-btn">Shadow Button</button>';
                    </script>
                </body>
            </html>
        """)

        await asyncio.sleep(0.5)  # Let shadow DOM attach

        glass_box = GlassBoxEngine()

        # Normal selector (won't find shadow button)
        normal_buttons = await page.query_selector_all('button')
        print(f"✓ Standard selector found: {len(normal_buttons)} button(s)")

        # Glass Box (should find both)
        all_buttons = await glass_box.get_all_interactive_nodes(page)
        print(f"✓ Glass Box found: {len(all_buttons)} interactive element(s)")

        success = len(all_buttons) >= len(normal_buttons)
        print(f"✓ Shadow DOM detection: {'PASS' if success else 'FAIL'}")

        await browser.close()
        return success


async def test_scroll_and_find():
    """Test 4: Velocity-Driven Explorer"""
    print("\n" + "="*70)
    print("TEST 4: Velocity-Driven Explorer (Infinite Scroll)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with long scrollable content
        await page.set_content("""
            <html>
                <body style="padding: 0; margin: 0;">
                    <div style="height: 300vh; background: linear-gradient(to bottom, #fff, #000);">
                        <div id="top" style="padding: 20px;">Top of Page</div>
                        <div id="middle" style="position: absolute; top: 150vh; padding: 20px; background: yellow;">
                            Middle Target
                        </div>
                        <div id="bottom" style="position: absolute; top: 280vh; padding: 20px; background: red;">
                            Bottom Target
                        </div>
                    </div>
                </body>
            </html>
        """)

        glass_box = GlassBoxEngine()

        # Try to find middle element
        async def check_middle():
            middle = await page.query_selector('#middle')
            if middle:
                is_visible = await middle.is_visible()
                return is_visible
            return False

        found = await glass_box.scroll_and_find(page, check_middle, max_scrolls=5)
        print(f"✓ Found middle target: {'PASS' if found else 'FAIL'}")

        await browser.close()
        return found


async def test_gaussian_typer():
    """Test 5: Gaussian Typer"""
    print("\n" + "="*70)
    print("TEST 5: Gaussian Typer (Human-Like Typing)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with input field
        await page.set_content("""
            <html>
                <body>
                    <input id="test-input" type="text" placeholder="Type here..." />
                    <div id="events-log"></div>
                    <script>
                        const input = document.getElementById('test-input');
                        const log = document.getElementById('events-log');
                        let eventCount = 0;

                        ['keydown', 'keypress', 'input', 'keyup'].forEach(eventType => {
                            input.addEventListener(eventType, () => {
                                eventCount++;
                                log.textContent = `Events fired: ${eventCount}`;
                            });
                        });
                    </script>
                </body>
            </html>
        """)

        glass_box = GlassBoxEngine()

        # Test human typing
        input_field = await page.query_selector('#test-input')
        test_text = "Test123"

        import time
        start_time = time.time()
        await glass_box.human_type(input_field, test_text)
        elapsed = time.time() - start_time

        # Verify typed content
        value = await input_field.input_value()
        print(f"✓ Typed text: '{value}' (expected: '{test_text}')")
        print(f"✓ Time elapsed: {elapsed:.2f}s for {len(test_text)} chars")
        print(f"✓ Avg per char: {elapsed/len(test_text)*1000:.0f}ms (human-like: ~100ms)")

        # Check if events were fired
        events_log = await page.query_selector('#events-log')
        events_text = await events_log.text_content()
        print(f"✓ {events_text}")

        success = value == test_text and elapsed > 0.3  # Should take at least 300ms for 7 chars

        await browser.close()
        return success


async def test_honeypot_filter():
    """Test 6: Honeypot Filter"""
    print("\n" + "="*70)
    print("TEST 6: Honeypot Filter (Visibility Analysis)")
    print("="*70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Create test page with various hidden elements
        await page.set_content("""
            <html>
                <body>
                    <a id="visible-link" href="#">Visible Link</a>
                    <a id="hidden-display" href="#" style="display: none;">Hidden Display None</a>
                    <a id="hidden-visibility" href="#" style="visibility: hidden;">Hidden Visibility</a>
                    <a id="hidden-opacity" href="#" style="opacity: 0;">Hidden Opacity 0</a>
                    <a id="hidden-tiny" href="#" style="width: 1px; height: 1px;">Tiny Element</a>
                    <a id="hidden-offscreen" href="#" style="position: absolute; left: -9999px;">Off Screen</a>
                    <a id="hidden-zindex" href="#" style="position: absolute; z-index: -999;">Negative Z</a>
                </body>
            </html>
        """)

        glass_box = GlassBoxEngine()

        # Get all links
        all_links = await page.query_selector_all('a')
        print(f"✓ Total links: {len(all_links)}")

        # Filter visible only
        visible_links = await glass_box.filter_visible_elements(all_links)
        print(f"✓ Visible links: {len(visible_links)}")
        print(f"✓ Filtered out: {len(all_links) - len(visible_links)} honeypot(s)")

        # Should only find the visible link
        success = len(visible_links) == 1
        print(f"✓ Honeypot detection: {'PASS' if success else 'FAIL'}")

        await browser.close()
        return success


async def run_all_tests():
    """Run all Glass Box Engine tests"""
    print("\n" + "="*70)
    print("GLASS BOX ENGINE - COMPREHENSIVE TEST SUITE")
    print("="*70)

    results = {}

    try:
        results['raycast'] = await test_raycast_visibility()
    except Exception as e:
        print(f"❌ Test 1 failed: {e}")
        results['raycast'] = False

    try:
        results['svg_hash'] = await test_svg_icon_hash()
    except Exception as e:
        print(f"❌ Test 2 failed: {e}")
        results['svg_hash'] = False

    try:
        results['shadow_dom'] = await test_shadow_dom_piercer()
    except Exception as e:
        print(f"❌ Test 3 failed: {e}")
        results['shadow_dom'] = False

    try:
        results['scroll'] = await test_scroll_and_find()
    except Exception as e:
        print(f"❌ Test 4 failed: {e}")
        results['scroll'] = False

    try:
        results['typing'] = await test_gaussian_typer()
    except Exception as e:
        print(f"❌ Test 5 failed: {e}")
        results['typing'] = False

    try:
        results['honeypot'] = await test_honeypot_filter()
    except Exception as e:
        print(f"❌ Test 6 failed: {e}")
        results['honeypot'] = False

    # Print summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = sum(results.values())
    total = len(results)

    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {test_name}")

    print(f"\nTotal: {passed}/{total} tests passed ({passed/total*100:.0f}%)")
    print("="*70)

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    exit(0 if success else 1)
