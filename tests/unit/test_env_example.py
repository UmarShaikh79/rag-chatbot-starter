"""`.env.example` names every environment variable the template reads, without values.

Names are found from the settings (every ``*_env`` field, including the API keys in
``config/models.yaml``), ``${VAR}`` in ``docker/compose.yaml``, and ``os.environ`` /
``os.getenv`` reads in ``src`` and ``tests``, so a new provider, option or test switch fails here
until it is listed. ``COHERE_API_KEY`` was missing before this test.

Every line is ``NAME=`` with nothing after it: examples live in the comments, so copying the file
never sends a placeholder anywhere. Only the variables the default setup needs are uncommented,
and ``CHATBOT__`` overrides stay commented (an empty override is rejected at startup).
"""

import re
from typing import Any

from pydantic import BaseModel

from chatbot.core.settings import load_settings
from tests.conftest import ROOT

ENV_EXAMPLE = ROOT / ".env.example"
REQUIRED = ["CHATBOT_PROFILE", "OPENAI_API_KEY", "CHATBOT_API_KEYS", "DATABASE_URL"]
# Set by the platform, never by you.
NOT_YOURS = {"GITHUB_STEP_SUMMARY", "PATH"}
ASSIGNMENT = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")


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


def _code_reads() -> set[str]:
    pattern = re.compile(r"""(?:environ\.get|environ\[|getenv)\(?\s*["']([A-Z][A-Z0-9_]*)["']""")
    names: set[str] = set()
    for folder in ("src", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            names |= set(pattern.findall(path.read_text()))
    return names - NOT_YOURS


def _lines() -> list[tuple[bool, str, str]]:
    """(commented, name, value) for every NAME= line."""
    out = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = ASSIGNMENT.match(line.strip())
        if m:
            out.append((m.group(1) is not None, m.group(2), m.group(3)))
    return out


def test_env_example_lists_every_variable_the_template_reads():
    settings = load_settings(root=ROOT, environ={})
    wanted = _env_fields(settings) | _compose_vars() | _code_reads()
    assert {"COHERE_API_KEY", "OPENAI_API_KEY", "TEST_DATABASE_URL"} <= wanted  # the scan works
    missing = sorted(wanted - {name for _, name, _ in _lines()})
    assert not missing, f".env.example does not mention {missing}"


def test_env_example_has_no_values_and_only_required_lines_uncommented():
    lines = _lines()
    with_values = [name for _, name, value in lines if value.strip()]
    assert not with_values, f"put examples in comments, not values: {with_values}"
    assert [name for commented, name, _ in lines if not commented] == REQUIRED
    names = [name for _, name, _ in lines]
    assert len(names) == len(set(names)), "a variable is listed twice"


def test_empty_required_lines_still_load():
    """Copying the file unchanged must not break config loading: an empty CHATBOT_PROFILE
    means dev, and the empty keys are only checked when something needs them."""
    s = load_settings(root=ROOT, environ=dict.fromkeys(REQUIRED, ""))
    assert s.models.answer == "openai:answer"
