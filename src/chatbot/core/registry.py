"""Name registry: config names (``retrieval.strategy: dense``) map to factories.

Implementations register themselves at import time::

    @registry.register("retriever", "dense")
    def make_dense(ctx: BuildContext) -> Retriever: ...

``chatbot.domain`` is imported last, so your own code can override any name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class RegistryError(KeyError):
    pass


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Callable[..., Any]]] = {}

    def register(self, kind: str, name: str) -> Callable[[F], F]:
        def deco(factory: F) -> F:
            self._items.setdefault(kind, {})[name] = factory
            return factory

        return deco

    def add(self, kind: str, name: str, factory: Callable[..., Any]) -> None:
        self._items.setdefault(kind, {})[name] = factory

    def get(self, kind: str, name: str) -> Callable[..., Any]:
        try:
            return self._items[kind][name]
        except KeyError:
            known = ", ".join(sorted(self._items.get(kind, {}))) or "none"
            raise RegistryError(
                f"No {kind} named '{name}' is registered (known: {known})"
            ) from None

    def names(self, kind: str) -> list[str]:
        return sorted(self._items.get(kind, {}))

    def create(self, kind: str, name: str, *args: Any, **kwargs: Any) -> Any:
        return self.get(kind, name)(*args, **kwargs)


registry = Registry()
