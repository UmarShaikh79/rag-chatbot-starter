from __future__ import annotations

from fastapi import Request

from chatbot.core.container import Components


def components(request: Request) -> Components:
    return request.app.state.components
