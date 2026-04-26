"""Strategy abstraction. MVP ships only DEFAULT (LLM judges freely).

When users want to add custom strategies (e.g., "PE<20 only", "must hold above
year-line"), they'll register Strategy instances here and the prompt builder
will inject `rules` into the system prompt as a hard-rules section.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Strategy:
    name: str
    description: str = ""
    rules: list[str] = field(default_factory=list)  # natural-language hard rules


DEFAULT = Strategy(
    name="default",
    description="自由判断 — LLM 完全凭借 snapshot 与新闻自行权衡，无硬规则约束",
    rules=[],
)


_REGISTRY: dict[str, Strategy] = {DEFAULT.name: DEFAULT}


def register(s: Strategy) -> None:
    _REGISTRY[s.name] = s


def get(name: str | None) -> Strategy:
    if not name:
        return DEFAULT
    return _REGISTRY.get(name, DEFAULT)
