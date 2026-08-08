import sys
from pathlib import Path

SDK_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SDK_SRC))
