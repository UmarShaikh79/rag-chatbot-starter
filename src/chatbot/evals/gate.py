"""The CI gate (ADR 010, ADR 022): fail when a metric falls below its floor, or drops more
than ``max_drop`` below the committed baseline.

evals/gate.yaml::

    profiles:
      offline:                       # CHATBOT_PROFILE the floors apply to
        min:
          retrieval.hit@3: 0.9       # metric names as in the report summary
        max: {}                      # upper bounds, e.g. latency.p95_ms: 4000
        max_drop: 0.05               # vs evals/baselines/offline.json, for every metric in min

A metric missing from the summary (``--retrieval-only`` skips answer metrics) is skipped,
not failed. Update the baseline on purpose with ``chatbot eval --update-baseline``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GateRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: dict[str, float] = {}
    max: dict[str, float] = {}
    max_drop: float | None = Field(None, ge=0.0)


class GateResult(BaseModel):
    passed: bool
    failures: list[str] = []
    checked: list[str] = []
    skipped: list[str] = []


def load_rules(path: Path, profile: str) -> GateRules:
    data = yaml.safe_load(path.read_text()) or {}
    profiles = data.get("profiles", {})
    if profile not in profiles:
        raise ValueError(f"{path} has no gate for profile '{profile}' (has: {sorted(profiles)})")
    return GateRules.model_validate(profiles[profile] or {})


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text()).get("summary")


def check(
    summary: dict[str, Any], rules: GateRules, baseline: dict[str, Any] | None = None
) -> GateResult:
    result = GateResult(passed=True)
    for name, floor in rules.min.items():
        value = summary.get(name)
        if value is None:
            result.skipped.append(name)
            continue
        result.checked.append(name)
        if value < floor:
            result.failures.append(f"{name} = {value} is below the floor {floor}")
        if baseline is not None and rules.max_drop is not None:
            before = baseline.get(name)
            if before is not None and value < before - rules.max_drop:
                result.failures.append(
                    f"{name} = {value} dropped more than {rules.max_drop} "
                    f"from the baseline {before}"
                )
    for name, ceiling in rules.max.items():
        value = summary.get(name)
        if value is None:
            result.skipped.append(name)
            continue
        result.checked.append(name)
        if value > ceiling:
            result.failures.append(f"{name} = {value} is above the ceiling {ceiling}")
    result.passed = not result.failures
    return result


def markdown(summary: dict[str, Any], gate: GateResult | None, title: str) -> str:
    lines = [f"### {title}", "", "| metric | value |", "| --- | --- |"]
    lines += [f"| `{k}` | {v} |" for k, v in summary.items()]
    if gate is not None:
        lines += ["", "**Gate: passed**" if gate.passed else "**Gate: FAILED**"]
        lines += [f"- {f}" for f in gate.failures]
    return "\n".join(lines) + "\n"
