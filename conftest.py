"""Pytest bootstrap: ensure the project root (containing ``app``) is
importable regardless of the invocation directory or pytest version."""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
