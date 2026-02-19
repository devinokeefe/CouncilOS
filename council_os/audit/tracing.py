from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceConfig:
    enabled: bool = False
    provider: str = "noop"


_TRACE_CONFIG = TraceConfig()


def configure_tracing(enabled: bool, provider: str = "noop") -> None:
    global _TRACE_CONFIG
    _TRACE_CONFIG = TraceConfig(enabled=enabled, provider=provider)


def tracing_enabled() -> bool:
    return _TRACE_CONFIG.enabled


@contextmanager
def trace_span(name: str) -> Iterator[None]:
    if not _TRACE_CONFIG.enabled:
        yield
        return

    if _TRACE_CONFIG.provider == "opentelemetry":
        try:
            from opentelemetry import trace as ot_trace  # type: ignore[import-not-found]

            tracer = ot_trace.get_tracer("council_os")
            with tracer.start_as_current_span(name):
                yield
            return
        except Exception:
            # Non-blocking by design for optional tracing.
            yield
            return

    yield
