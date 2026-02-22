from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from schemas.configs import (
    ConfigMetaSchema,
    DeleteConfigResponse,
    ListConfigsResponse,
    LoadConfigResponse,
    SaveConfigResponse,
)
from settings import settings


logger = logging.getLogger(__name__)


class ConfigLoaderService:
    """Manages JSON preset configs stored in a local directory."""

    def __init__(self, presets_path: str = None) -> None:
        if not presets_path:
            presets_path = settings.presets_path
        self.presets_path = presets_path
        os.makedirs(self.presets_path, exist_ok=True)
        logger.debug("ConfigLoaderService initialised | presets_path=%s", self.presets_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_configs(self) -> ListConfigsResponse:
        """Return metadata for every JSON file in the presets directory."""
        try:
            configs = self._scan_presets_path()
            logger.debug("Listed configs | count=%d | presets_path=%s", len(configs), self.presets_path)
            return ListConfigsResponse(success=True, configs=configs)
        except Exception as exc:
            logger.error("Failed to list configs | presets_path=%s | error=%s", self.presets_path, exc)
            return ListConfigsResponse(success=False, error=str(exc))

    def load_config(self, config_name: str) -> LoadConfigResponse:
        """Load and return a single config by name (with or without .json extension)."""
        logger.debug("Loading config | name=%s", config_name)
        try:
            data = self._read_config_file(config_name)
            if data is None:
                logger.warning("Config not found | name=%s", config_name)
                return LoadConfigResponse(
                    success=False, error=f"Config '{config_name}' not found"
                )
            logger.debug("Config loaded successfully | name=%s", config_name)
            return LoadConfigResponse(success=True, config=data)
        except ValueError as exc:
            logger.error("Invalid config file | name=%s | error=%s", config_name, exc)
            return LoadConfigResponse(success=False, error=str(exc))
        except Exception as exc:
            logger.error("Unexpected error loading config | name=%s | error=%s", config_name, exc)
            return LoadConfigResponse(success=False, error=f"Error loading config: {exc}")

    def save_config(
        self,
        config_name: str,
        config_data: dict[str, Any],
    ) -> SaveConfigResponse:
        """Persist a config dict to disk."""
        try:
            filepath = self._safe_filepath(config_name)
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(config_data, fh, indent=2, ensure_ascii=False)
            clean_name = config_name.removesuffix(".json")
            logger.info("Config saved | name=%s | path=%s", clean_name + ".json", filepath)
            return SaveConfigResponse(
                success=True,
                message=f"Config '{clean_name}.json' saved",
                config_name=clean_name,
            )
        except ValueError as exc:
            logger.error("Invalid config path on save | name=%s | error=%s", config_name, exc)
            return SaveConfigResponse(success=False, error=str(exc))
        except Exception as exc:
            logger.error("Failed to save config | name=%s | error=%s", config_name, exc)
            return SaveConfigResponse(success=False, error=f"Error saving config: {exc}")

    def delete_config(self, config_name: str) -> DeleteConfigResponse:
        """Delete a config file from disk."""
        try:
            filepath = self._safe_filepath(config_name)
            if not os.path.exists(filepath):
                logger.warning("Config not found for deletion | name=%s", config_name)
                return DeleteConfigResponse(
                    success=False, error=f"Config '{config_name}' not found"
                )
            os.remove(filepath)
            clean_name = config_name.removesuffix(".json")
            logger.info("Config deleted | name=%s | path=%s", clean_name + ".json", filepath)
            return DeleteConfigResponse(
                success=True, message=f"Config '{clean_name}.json' deleted"
            )
        except ValueError as exc:
            logger.error("Invalid config path on delete | name=%s | error=%s", config_name, exc)
            return DeleteConfigResponse(success=False, error=str(exc))
        except Exception as exc:
            logger.error("Failed to delete config | name=%s | error=%s", config_name, exc)
            return DeleteConfigResponse(success=False, error=f"Error deleting config: {exc}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _scan_presets_path(self) -> list[ConfigMetaSchema]:
        configs: list[ConfigMetaSchema] = []
        for filename in os.listdir(self.presets_path):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(self.presets_path, filename)
            if not os.path.isfile(filepath):
                continue
            stat = os.stat(filepath)
            configs.append(
                ConfigMetaSchema(
                    name=filename,
                    size=stat.st_size,
                    modified=stat.st_mtime,
                )
            )
        configs.sort(key=lambda c: c.modified, reverse=True)
        return configs

    def _read_config_file(self, config_name: str) -> Optional[dict[str, Any]]:
        filepath = self._safe_filepath(config_name)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in config file: {exc}") from exc

    def _safe_filepath(self, config_name: str) -> str:
        """Return an absolute path inside presets_path, raising ValueError on traversal."""
        if not config_name.endswith(".json"):
            config_name = config_name + ".json"
        filepath = os.path.join(self.presets_path, config_name)
        real_path = os.path.realpath(filepath)
        real_dir = os.path.realpath(self.presets_path)
        if not real_path.startswith(real_dir + os.sep) and real_path != real_dir:
            logger.warning(
                "Path traversal attempt blocked | input=%s | resolved=%s | allowed_dir=%s",
                config_name, real_path, real_dir,
            )
            raise ValueError(f"Invalid config path: '{config_name}'")
        return filepath