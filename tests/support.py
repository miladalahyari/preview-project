"""Load lifecycle modules against synthetic settings, independent of site configuration."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, script="preview.py"):
    config = (ROOT / "tests/fixtures/platform.json").read_text()
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    with patch.object(Path, "read_text", return_value=config):
        spec.loader.exec_module(module)
    return module
