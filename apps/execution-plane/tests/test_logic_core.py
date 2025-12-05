import sys
import os
import unittest

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from algorithms.LevenshteinScorer import LevenshteinScorer, DOMElement
from core.DomPruner import DOMPruner

class TestLogicCore(unittest.TestCase):
    def test_levenshtein_scorer(self):
        scorer = LevenshteinScorer()

        elements = [
            DOMElement(tag_name="button", text="Login", attributes={"id": "login-btn"}),
            DOMElement(tag_name="div", text="Login Header", attributes={"class": "header"}),
            DOMElement(tag_name="a", text="Sign In", attributes={"href": "/login"}),
            DOMElement(tag_name="button", text="Submit", attributes={"aria-label": "login"}),
        ]

        # Test 1: Exact match on text
        best = scorer.find_best_candidate(elements, "Login")
        self.assertIsNotNone(best)
        self.assertEqual(best.element.text, "Login")
        print(f"Test 1 (Exact Match): Selected '{best.element.text}' with score {best.score}")

        # Test 2: Intent match (Sign In -> Login)
        best = scorer.find_best_candidate(elements, "login")
        # Should pick "Sign In" or "Login" or "Submit" depending on weights.
        # "Login" button:
        #   Base: 1.0 (text match)
        #   Tag: +0.2
        #   ID: +0.3 (login-btn contains login)
        #   Total: 1.5 -> 1.0
        # "Submit" button:
        #   Base: low
        #   Tag: +0.2
        #   Aria: +0.4 (matches intent)
        #   Total: ~0.6+

        # Let's see what it picks.
        print(f"Test 2 (Intent 'login'): Selected '{best.element.text}' with score {best.score} Reason: {best.match_reason}")

        # Test 3: ID match
        elements_id = [
            DOMElement(tag_name="button", text="Click Me", attributes={"id": "submit-order"}), # ID match +0.3
            DOMElement(tag_name="button", text="Click Me", attributes={"id": "random"}),      # No ID match
        ]
        best = scorer.find_best_candidate(elements_id, "submit")
        print(f"Test 3 (ID Match): Selected '{best.element.text}' with score {best.score} Reason: {best.match_reason}")
        self.assertTrue("ID match" in best.match_reason)
        self.assertEqual(best.element.attributes["id"], "submit-order")

    def test_dom_pruner(self):
        pruner = DOMPruner()
        html = """
        <html>
            <head>
                <script>console.log('junk');</script>
                <style>.junk { color: red; }</style>
            </head>
            <body>
                <!-- Comment -->
                <div id="main" class="container" style="background: blue;">
                    <h1>Welcome</h1>
                    <button id="login" onclick="alert('hi')">Login</button>
                    <a href="/about">About</a>
                </div>
            </body>
        </html>
        """

        cleaned, tokens = pruner.prune(html)
        print(f"Original HTML length: {len(html)}")
        print(f"Cleaned HTML length: {len(cleaned)}")
        print(f"Estimated Tokens: {tokens}")
        print("Cleaned HTML Preview:")
        print(cleaned)

        self.assertNotIn("<script>", cleaned)
        self.assertNotIn("console.log", cleaned)
        self.assertNotIn("<style>", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertIn('<button id="login">', cleaned) # Attributes stripped?
        # We kept 'id', 'name', etc. 'onclick' should be gone. 'style' should be gone.

if __name__ == "__main__":
    unittest.main()
