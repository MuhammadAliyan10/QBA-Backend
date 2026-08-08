# tests/conftest.py
"""
Shared pytest configuration for the execution-plane test suite.

No fixtures are defined here yet — this file exists to mark the directory
as a pytest root and to ensure sys.path is correct before any test module runs.
"""
import sys
import os

# Ensure src/ is on the path for all test modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
