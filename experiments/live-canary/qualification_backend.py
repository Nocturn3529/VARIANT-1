"""Launch the normal source backend with an optional isolated qualification meter."""
import os
from pathlib import Path
import runpy
import sys

from qualification_budget import install_budget

budget = os.environ.get("VARIANT1_QUALIFICATION_BUDGET_FILE")
if budget:
    install_budget(Path(budget))
backend = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(backend))
runpy.run_path(str(backend / "server.py"), run_name="__main__")
