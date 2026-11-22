class ChatbotError(Exception):
    """Base error for the template."""


class IndexMismatchError(ChatbotError):
    """The configured embedding model differs from the one the index was built with (ADR 005)."""


class NotFoundError(ChatbotError):
    pass


class ForbiddenError(ChatbotError):
    pass
