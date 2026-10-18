"""Configuración de simulación explícita, sin conexiones al importar."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    mode: str = "simulation"
    timezone: str = "America/Bogota"
    llm_provider: str = "mock"
    anthropic_model: str = "claude-haiku-4-5-20251001"
    data_directory: Path | None = None
    database_path: Path = Path(".local/collections.sqlite3")
    demo_now: str = "2026-10-19T09:00:00-05:00"


def load_settings(env_file: Path | None = None) -> Settings:
    """Solo carga el .env indicado; jamás busca credenciales de otros proyectos."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    data_directory = os.getenv("COLLECTIONS_DATA_DIR", "").strip()
    settings = Settings(
        mode=os.getenv("COLLECTIONS_MODE", "simulation"),
        timezone=os.getenv("COLLECTIONS_TIMEZONE", "America/Bogota"),
        llm_provider=os.getenv("COLLECTIONS_LLM_PROVIDER", "mock"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        data_directory=Path(data_directory) if data_directory else None,
        database_path=Path(os.getenv("COLLECTIONS_DATABASE_PATH", ".local/collections.sqlite3")),
        demo_now=os.getenv("COLLECTIONS_DEMO_NOW", "2026-10-19T09:00:00-05:00"),
    )
    if settings.mode != "simulation":
        raise ValueError("Solo se admite simulation; no existe telefonía real.")
    if settings.llm_provider not in {"mock", "anthropic"}:
        raise ValueError("Proveedor no implementado. Usa mock o anthropic; no hay fallback.")
    if datetime.fromisoformat(settings.demo_now).tzinfo is None:
        raise ValueError("COLLECTIONS_DEMO_NOW debe incluir zona horaria.")
    return settings
