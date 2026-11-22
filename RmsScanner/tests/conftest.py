"""Load the independent pure image stages for their production unit tests."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for project in ("image-preprocess", "chart-enhance"):
    sys.path.insert(0, str(ROOT / project / "src"))
