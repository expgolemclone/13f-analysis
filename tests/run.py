"""Run the full suite, with reports on stdout for strict PowerShell harnesses."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
