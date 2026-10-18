"""Adaptación de configuración y recursos del agente."""

from pathlib import Path

from collections_lab.config.settings import load_settings


def data_directory() -> Path:
    settings = load_settings()
    return settings.data_directory or Path(__file__).resolve().parent / "data"
