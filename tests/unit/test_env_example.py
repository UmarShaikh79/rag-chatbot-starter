"""`.env.example` names every environment variable the template reads by default.

Keys are found from the settings themselves (every ``*_env`` field, including the API keys in
``config/models.yaml``) and from ``${VAR}`` in ``docker/compose.yaml``, so a new provider or
option fails here until it is documented. ``COHERE_API_KEY`` was missing before this test.
"""

import re
from typing import Any

from pydantic import BaseModel

from chatbot.core.settings import load_settings
from tests.conftest import ROOT

ENV_EXAMPLE = ROOT / ".env.example"


def _env_fields(obj: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(obj, BaseModel):
        for key, value in obj:
            if key.endswith("_env") and isinstance(value, str):
                names.add(value)
            names |= _env_fields(value)
    elif isinstance(obj, dict):
        for value in obj.values():
            names |= _env_fields(value)
    elif isinstance(obj, list):
        for value in obj:
            names |= _env_fields(value)
    return names


def _compose_vars() -> set[str]:
    text = (ROOT / "docker" / "compose.yaml").read_text()
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))


def _documented() -> set[str]:
    # "NAME=" at the start of a line, commented out or not.
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.M))


def test_env_example_lists_every_variable_read_by_default():
    settings = load_settings(root=ROOT, environ={})
    wanted = _env_fields(settings) | _compose_vars()
    assert "COHERE_API_KEY" in wanted and "OPENAI_API_KEY" in wanted  # the scan works
    missing = sorted(wanted - _documented())
    assert not missing, f".env.example does not mention {missing}"
