"""Dependencias por ejecución; el reloj de la demo es explícito y sustituible."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from agents.collections.memory import SQLiteRepository
from collections_lab.config.settings import load_settings


@dataclass
class AppContext:
    repository: SQLiteRepository
    now: datetime
    provider: str = "mock"
    model: str = "claude-haiku-4-5-20251001"

    @property
    def today(self):
        return self.now.astimezone(ZoneInfo("America/Bogota")).date()


@lru_cache(maxsize=8)
def repository_for(path: str) -> SQLiteRepository:
    return SQLiteRepository(Path(path))


def default_context() -> AppContext:
    settings = load_settings()
    return AppContext(
        repository_for(str(settings.database_path.resolve())),
        datetime.fromisoformat(settings.demo_now),
        settings.llm_provider,
        settings.anthropic_model,
    )


def resolve_context(runtime) -> AppContext:
    context = runtime.context
    if isinstance(context, AppContext):
        return context
    # Studio mirrors transport thread/checkpoint metadata into context, not business state.
    if context is None or (
        isinstance(context, dict)
        and context.keys() <= {"thread_id", "thread_ts"}
        and all(isinstance(value, str) for value in context.values())
    ):
        return default_context()
    raise ValueError("El contexto de ejecución solo puede inyectarlo el backend local.")
