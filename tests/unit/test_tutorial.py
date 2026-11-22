"""The code in docs/tutorial.md works exactly as written.

Both code blocks (the retriever and its example test) are read from the Markdown and run, so
editing the tutorial without keeping its code working fails CI.
"""

import re

import pytest

from tests.conftest import ROOT

TUTORIAL = ROOT / "docs" / "tutorial.md"


def _block(first_line: str) -> str:
    """The ```python block whose first line is ``first_line``."""
    for code in re.findall(r"```python\n(.*?)```", TUTORIAL.read_text(), re.S):
        if code.startswith(first_line):
            return code
    pytest.fail(f"docs/tutorial.md has no code block starting with {first_line!r}")


async def test_tutorial_retriever_runs_as_written():
    retriever_code = _block("# src/chatbot/domain/title_boost.py")
    exec(compile(retriever_code, "docs/tutorial.md (title_boost.py)", "exec"), {})

    test_code = _block("# tests/unit/test_title_boost.py")
    namespace: dict = {}
    exec(compile(test_code, "docs/tutorial.md (test_title_boost.py)", "exec"), namespace)
    await namespace["test_title_boost_ranks_and_gates_on_its_own_score"]()
