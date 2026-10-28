"""Run from a source checkout; installed command uses the same main function."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from career_assistant.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
