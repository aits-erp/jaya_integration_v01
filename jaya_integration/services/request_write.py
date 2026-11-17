"""Private server-side write context; cannot be supplied as document flags."""

from contextvars import ContextVar

allowed = ContextVar("jaya_ewaybill_api_write", default=False)
