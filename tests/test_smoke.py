"""Basic smoke test for the command-line converter."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ipynb-notebook_to_pdf.py"
DEMO = ROOT / "examples" / "demo.ipynb"


def test_demo_notebook_converts_to_pdf(tmp_path):
    output = tmp_path / "demo.pdf"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(DEMO), "--no-emoji-images", "-o", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert output.exists()
    assert output.stat().st_size > 500
    assert output.read_bytes().startswith(b"%PDF-")
