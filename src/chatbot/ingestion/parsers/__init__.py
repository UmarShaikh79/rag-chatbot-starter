"""Importing this package registers the built-in parsers."""

from chatbot.ingestion.parsers import docx, html, pdf, text  # noqa: F401
from chatbot.ingestion.parsers.base import parser_for

__all__ = ["parser_for"]
