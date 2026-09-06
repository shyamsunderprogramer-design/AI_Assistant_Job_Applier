"""Loads config.yaml and .env into a single settings object."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class Config:
    """Thin wrapper over the parsed YAML with dotted-path lookup."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        """Look up a nested key, e.g. cfg.get("http.min_delay_seconds")."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, path: str) -> Any:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            raise KeyError(f"Missing required config key: {path}")
        return value

    @property
    def user_agent(self) -> str:
        contact = os.getenv("SCRAPER_CONTACT_EMAIL", "").strip() or "not provided"
        template = self.get("http.user_agent", "JobApplierAgent/0.1")
        return template.replace("{contact}", contact)

    @property
    def database_url(self) -> str:
        return os.getenv("DATABASE_URL") or self.get(
            "database.url", "sqlite:///data/jobs.db"
        )


_MISSING = object()


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data)


def setup_logging(cfg: Config) -> None:
    """Console + rotating-free file logging. Called once from main."""
    level = getattr(logging, str(cfg.get("logging.level", "INFO")).upper(), logging.INFO)
    log_file = PROJECT_ROOT / cfg.get("logging.file", "logs/agent.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")],
    )
    # requests/urllib3 are noisy at DEBUG
    logging.getLogger("urllib3").setLevel(logging.WARNING)
