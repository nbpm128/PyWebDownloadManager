from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class MirrorRef(BaseModel):
    """One mirror of a download record inside a preset node.

    extra="allow" keeps legacy fields stored in presets.json (e.g. custom
    mirror keys) instead of silently dropping them on validation round-trips.
    """

    model_config = {"extra": "allow"}

    url: str
    auth: Optional[str] = None
    priority: Optional[int] = None
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)


class PresetDownloadRecord(BaseModel):
    """A download record as stored in a preset node's `downloads` array.

    extra="allow" preserves legacy fields found in existing records
    (`extract`, `file_size`, ...) across validation round-trips.

    file_name and mirrors are optional on purpose: the UI (epAddDownload)
    creates records as `{id, mirrors: []}` and fills them in later; the API
    validates structure, not the fill-in stage.
    """

    model_config = {"extra": "allow"}

    id: str = Field(min_length=1)
    file_name: Optional[str] = None
    folder_path: Optional[str] = None
    checksum: Optional[str] = None
    hash_algorithm: Optional[str] = None
    description: Optional[str] = None
    mirrors: list[MirrorRef] = Field(default_factory=list)


class NodeCreateRequest(BaseModel):
    """Create a preset node inline under `parent_id` (or at the tree root)."""

    id: Optional[str] = None
    name: str = Field(min_length=1)
    description: Optional[str] = None
    parent_id: Optional[str] = None


class NodeUpdateRequest(BaseModel):
    """Partially update a preset node — only the fields that are passed."""

    name: Optional[str] = None
    description: Optional[str] = None


class RefCreateRequest(BaseModel):
    """Add a by-label reference (`preset_ids` / `download_ids`) to a node."""

    kind: Literal["preset", "download"]
    ref: str = Field(min_length=1)


class TreeResponse(BaseModel):
    """The whole config dict as stored on disk (`tree["presets"]` = root nodes)."""

    success: bool
    config: Optional[str] = None
    tree: Optional[dict] = None
    error: Optional[str] = None


class NodeResponse(BaseModel):
    """A single preset node (with its children/downloads/refs)."""

    success: bool
    node: Optional[dict] = None
    error: Optional[str] = None


class MutationResponse(BaseModel):
    """Result of a mutating operation (create/update/delete)."""

    success: bool
    message: Optional[str] = None
    id: Optional[str] = None
    error: Optional[str] = None
