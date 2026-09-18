# conftest.py: make the repo root importable for tests moved from root.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))