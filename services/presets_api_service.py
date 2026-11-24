from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Optional

from schemas.presets_api import (
    MutationResponse,
    NodeCreateRequest,
    NodeResponse,
    NodeUpdateRequest,
    PresetDownloadRecord,
    RefCreateRequest,
    TreeResponse,
)
from services.config_loader_service import ConfigLoaderService

logger = logging.getLogger(__name__)


class PresetsApiService:
    """CRUD over the preset tree stored in a config JSON file.

    The tree is a recursive structure: every node may hold inline children
    (`presets`), inline download records (`downloads`) and by-label references
    to other nodes / records (`preset_ids` / `download_ids`).

    Lock scope: this service's instance-level lock serialises its own
    read-modify-write cycles and persists the file atomically (temp file +
    os.replace) so a dropped connection cannot corrupt it. Other writers of
    the same file (e.g. ConfigLoaderService.save_config behind the old
    /api/configs endpoints) do NOT take this lock — concurrent edits from the
    legacy path and this API can still lose updates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loader = ConfigLoaderService()
        logger.debug("PresetsApiService initialised")

    # ------------------------------------------------------------------
    # Public API — reads
    # ------------------------------------------------------------------

    def get_tree(self, config_name: str = "presets") -> TreeResponse:
        """Return the whole config dict; root nodes live in tree["presets"]."""
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return TreeResponse(success=False, config=config_name, error=err)
            return TreeResponse(success=True, config=config_name, tree=data)

    def get_node(self, node_id: str, config_name: str = "presets") -> NodeResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return NodeResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return NodeResponse(success=False, error=f"Node '{node_id}' not found")
            return NodeResponse(success=True, node=node)

    # ------------------------------------------------------------------
    # Public API — nodes
    # ------------------------------------------------------------------

    def create_node(self, req: NodeCreateRequest, config_name: str = "presets") -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)

            node_id = req.id or f"preset_{uuid.uuid4().hex[:8]}"
            if self._find_node(data, node_id) is not None:
                return MutationResponse(
                    success=False, id=node_id, error=f"Node '{node_id}' already exists"
                )

            node: dict[str, Any] = {"id": node_id, "name": req.name}
            if req.description is not None:
                node["description"] = req.description

            if req.parent_id:
                parent = self._find_node(data, req.parent_id)
                if parent is None:
                    return MutationResponse(
                        success=False, id=node_id, error=f"Parent node '{req.parent_id}' not found"
                    )
                parent.setdefault("presets", []).append(node)
            else:
                data.setdefault("presets", []).append(node)

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, id=node_id, error=save_err)
            logger.info("Preset node created | id=%s | parent=%s", node_id, req.parent_id)
            return MutationResponse(success=True, id=node_id, message=f"Node '{node_id}' created")

    def update_node(
        self, node_id: str, req: NodeUpdateRequest, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            if req.name is not None:
                node["name"] = req.name
            if req.description is not None:
                node["description"] = req.description

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Preset node updated | id=%s", node_id)
            return MutationResponse(success=True, id=node_id, message=f"Node '{node_id}' updated")

    def delete_node(
        self, node_id: str, recursive: bool = False, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)

            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            if not recursive:
                if node.get("presets"):
                    return MutationResponse(
                        success=False,
                        error=f"Node '{node_id}' has child presets; use recursive=true",
                    )
                if node.get("downloads") or node.get("download_ids"):
                    return MutationResponse(
                        success=False,
                        error=f"Node '{node_id}' holds downloads; use recursive=true",
                    )
                if self._is_referenced(data, node_id):
                    return MutationResponse(
                        success=False,
                        error=f"Node '{node_id}' is referenced by other nodes' preset_ids",
                    )

            parent_list = self._find_parent_list(data, node_id)
            if parent_list is None:
                return MutationResponse(success=False, error=f"Parent of '{node_id}' not found")
            parent_list[:] = [n for n in parent_list if n.get("id") != node_id]
            self._remove_preset_refs(data, node_id)

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Preset node deleted | id=%s | recursive=%s", node_id, recursive)
            return MutationResponse(success=True, id=node_id, message=f"Node '{node_id}' deleted")

    # ------------------------------------------------------------------
    # Public API — downloads
    # ------------------------------------------------------------------

    def add_download(
        self, node_id: str, record: PresetDownloadRecord, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            downloads = node.setdefault("downloads", [])
            if any(d.get("id") == record.id for d in downloads):
                return MutationResponse(
                    success=False, id=record.id,
                    error=f"Download '{record.id}' already exists in node '{node_id}'",
                )
            downloads.append(record.model_dump(exclude_none=True))

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Download added | id=%s | node=%s", record.id, node_id)
            return MutationResponse(success=True, id=record.id, message=f"Download '{record.id}' added")

    def update_download(
        self, node_id: str, download_id: str, patch: dict, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            existing = next((d for d in node.get("downloads", []) if d.get("id") == download_id), None)
            if existing is None:
                return MutationResponse(
                    success=False, error=f"Download '{download_id}' not found in node '{node_id}'"
                )

            merged = {**existing, **patch}
            if "id" in patch and patch["id"] != download_id:
                # PATCH may not re-id a record onto an existing one
                if any(d.get("id") == patch["id"] for d in node.get("downloads", [])):
                    return MutationResponse(
                        success=False, id=download_id,
                        error=f"Download id '{patch['id']}' already exists in node '{node_id}'",
                    )
            try:
                # validation only — the record is stored as `merged` verbatim,
                # so legacy fields outside the schema survive the round-trip
                PresetDownloadRecord.model_validate(merged)
            except Exception as exc:
                logger.warning("Download patch rejected | node=%s | id=%s | error=%s", node_id, download_id, exc)
                return MutationResponse(
                    success=False, id=download_id,
                    error=f"Invalid patch for download '{download_id}': {exc}",
                )
            node["downloads"] = [merged if d.get("id") == download_id else d for d in node.get("downloads", [])]

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Download updated | id=%s | node=%s", download_id, node_id)
            return MutationResponse(
                success=True, id=download_id, message=f"Download '{download_id}' updated"
            )

    def delete_download(
        self, node_id: str, download_id: str, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            before = len(node.get("downloads", []))
            node["downloads"] = [d for d in node.get("downloads", []) if d.get("id") != download_id]
            if len(node["downloads"]) == before:
                return MutationResponse(
                    success=False, error=f"Download '{download_id}' not found in node '{node_id}'"
                )
            # by-label references from other nodes are left as-is (dangling refs allowed)

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Download deleted | id=%s | node=%s", download_id, node_id)
            return MutationResponse(
                success=True, id=download_id, message=f"Download '{download_id}' deleted"
            )

    # ------------------------------------------------------------------
    # Public API — by-label references
    # ------------------------------------------------------------------

    def add_ref(
        self, node_id: str, req: RefCreateRequest, config_name: str = "presets"
    ) -> MutationResponse:
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            if not self._ref_exists(data, req.kind, req.ref):
                return MutationResponse(
                    success=False, error=f"{req.kind} '{req.ref}' does not exist in the tree"
                )

            key = f"{req.kind}_ids"
            refs = node.setdefault(key, [])
            if req.ref in refs:
                return MutationResponse(
                    success=False, id=req.ref, error=f"Ref '{req.ref}' already in {key}"
                )
            refs.append(req.ref)

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Ref added | kind=%s | ref=%s | node=%s", req.kind, req.ref, node_id)
            return MutationResponse(success=True, id=req.ref, message=f"Ref '{req.ref}' added to {key}")

    def delete_ref(
        self, node_id: str, kind: str, ref_id: str, config_name: str = "presets"
    ) -> MutationResponse:
        if kind not in ("preset", "download"):
            return MutationResponse(
                success=False, error=f"Invalid kind '{kind}': expected 'preset' or 'download'"
            )
        with self._lock:
            data, err = self._load(config_name)
            if err:
                return MutationResponse(success=False, error=err)
            node = self._find_node(data, node_id)
            if node is None:
                return MutationResponse(success=False, error=f"Node '{node_id}' not found")

            key = f"{kind}_ids"
            refs = node.get(key, [])
            if ref_id not in refs:
                return MutationResponse(
                    success=False, error=f"Ref '{ref_id}' not found in {key} of '{node_id}'"
                )
            node[key] = [r for r in refs if r != ref_id]

            save_err = self._save(config_name, data)
            if save_err:
                return MutationResponse(success=False, error=save_err)
            logger.info("Ref deleted | kind=%s | ref=%s | node=%s", kind, ref_id, node_id)
            return MutationResponse(success=True, id=ref_id, message=f"Ref '{ref_id}' removed from {key}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self, config_name: str) -> tuple[Optional[dict], Optional[str]]:
        """Read the config file via ConfigLoaderService and unwrap the response."""
        resp = self._loader.load_config(config_name)
        if not resp.success or resp.config is None:
            return None, resp.error or f"Config '{config_name}' not found"
        return resp.config, None

    def _save(self, config_name: str, data: dict) -> Optional[str]:
        """Atomic write: temp file next to the target, then os.replace.

        Never raises: any failure (including a path-traversal ValueError from
        _safe_filepath) comes back as an error string — the service contract.
        """
        tmp = None
        try:
            filepath = self._loader._safe_filepath(config_name)
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, filepath)
            return None
        except Exception as exc:
            logger.error("Atomic save failed | config=%s | error=%s", config_name, exc)
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return f"Error saving config: {exc}"

    def _find_node(self, data: dict, node_id: str) -> Optional[dict]:
        for node in data.get("presets", []):
            if node.get("id") == node_id:
                return node
            found = self._find_node(node, node_id)
            if found is not None:
                return found
        return None

    def _find_parent_list(self, data: dict, node_id: str) -> Optional[list]:
        """The `presets` list that directly holds the node with this id."""
        for node in data.get("presets", []):
            if node.get("id") == node_id:
                return data["presets"]
            found = self._find_parent_list(node, node_id)
            if found is not None:
                return found
        return None

    def _is_referenced(self, data: dict, node_id: str) -> bool:
        """Whether any node other than the target lists node_id in its preset_ids."""
        for node in self._iter_nodes(data):
            if node.get("id") == node_id:
                continue
            if node_id in (node.get("preset_ids") or []):
                return True
        return False

    def _remove_preset_refs(self, data: dict, node_id: str) -> None:
        for node in self._iter_nodes(data):
            if node_id in (node.get("preset_ids") or []):
                node["preset_ids"] = [r for r in node["preset_ids"] if r != node_id]

    def _ref_exists(self, data: dict, kind: str, ref: str) -> bool:
        if kind == "preset":
            return self._find_node(data, ref) is not None
        return self._find_download(data, ref) is not None

    def _find_download(self, data: dict, download_id: str) -> Optional[dict]:
        for node in self._iter_nodes(data):
            for d in node.get("downloads", []):
                if d.get("id") == download_id:
                    return d
        return None

    def _iter_nodes(self, data: dict):
        """Yield every node of the tree, depth-first."""
        for node in data.get("presets", []):
            yield node
            yield from self._iter_nodes(node)
