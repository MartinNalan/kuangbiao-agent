from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPAT = ROOT / "src" / "sitecustomize.py"


class Python310RuntimeCompatibilityTests(unittest.TestCase):
    def test_strenum_is_installed_when_the_interpreter_does_not_supply_it(self) -> None:
        script = textwrap.dedent(
            f"""
            import enum
            import runpy
            if hasattr(enum, "StrEnum"):
                delattr(enum, "StrEnum")
            runpy.run_path({str(COMPAT)!r})
            class State(enum.StrEnum):
                READY = "ready"
            assert str(State.READY) == "ready"
            assert State.READY == "ready"
            assert State.READY.value == "ready"
            """
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_existing_standard_library_strenum_is_not_replaced(self) -> None:
        script = textwrap.dedent(
            f"""
            import enum
            import runpy
            sentinel = object()
            enum.StrEnum = sentinel
            runpy.run_path({str(COMPAT)!r})
            assert enum.StrEnum is sentinel
            """
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
