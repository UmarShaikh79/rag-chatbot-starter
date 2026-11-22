"""The Docker image must hold every folder the app reads at runtime.

`chatbot calibrate` and `chatbot eval` read `evals/` (dataset, gate, baselines); without it
they fail inside a container, and Kubernetes users have no other way to calibrate.
"""

import re

from chatbot.core.settings import load_settings
from tests.conftest import ROOT

RUNTIME_DIRS = {"src", "config", "prompts", "migrations", "evals"}


def _copied_dirs() -> set[str]:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    return {m.group(1).rstrip("/") for m in re.finditer(r"^COPY\s+(\S+)\s+\S+$", dockerfile, re.M)}


def _ignored() -> set[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return {line.strip().rstrip("/") for line in lines if line.strip()}


def test_image_copies_runtime_dirs():
    missing = RUNTIME_DIRS - _copied_dirs()
    assert not missing, f"docker/Dockerfile does not COPY {sorted(missing)}"
    assert not RUNTIME_DIRS & _ignored(), ".dockerignore excludes a runtime folder"


def test_eval_paths_live_in_copied_dirs():
    s = load_settings(root=ROOT, environ={})
    for path in (s.evals.dataset, s.evals.gate, s.evals.baselines_dir):
        assert path.split("/")[0] in _copied_dirs(), f"{path} is not in the image"
