from typing import Any

from fastapi import APIRouter

from schemas.presets_api import (
    MutationResponse,
    NodeCreateRequest,
    NodeResponse,
    NodeUpdateRequest,
    PresetDownloadRecord,
    RefCreateRequest,
    TreeResponse,
)
from services.presets_api_service import PresetsApiService

router = APIRouter()

presets_api_service = PresetsApiService()


# ===========================================================================
# Reads
# ===========================================================================

@router.get("/api/presets/tree", response_model=TreeResponse)
async def get_presets_tree(config: str = "presets"):
    """Return the whole preset config dict (root nodes in tree["presets"])."""
    return presets_api_service.get_tree(config)


@router.get("/api/presets/tree/{node_id}", response_model=NodeResponse)
async def get_preset_node(node_id: str, config: str = "presets"):
    """Return one preset node (with children, downloads and refs)."""
    return presets_api_service.get_node(node_id, config)


# ===========================================================================
# Nodes (inner presets)
# ===========================================================================

@router.post("/api/presets/nodes", response_model=MutationResponse)
async def create_preset_node(req: NodeCreateRequest, config: str = "presets"):
    """Create a preset node under `parent_id` (or at the tree root)."""
    return presets_api_service.create_node(req, config)


@router.patch("/api/presets/nodes/{node_id}", response_model=MutationResponse)
async def update_preset_node(node_id: str, req: NodeUpdateRequest, config: str = "presets"):
    """Rename a node / change its description (partial update)."""
    return presets_api_service.update_node(node_id, req, config)


@router.delete("/api/presets/nodes/{node_id}", response_model=MutationResponse)
async def delete_preset_node(node_id: str, recursive: bool = False, config: str = "presets"):
    """Remove the node object from its parent's `presets[]`.

    recursive=false refuses to delete nodes that have children, hold downloads
    or are referenced by other nodes' preset_ids.
    """
    return presets_api_service.delete_node(node_id, recursive, config)


# ===========================================================================
# Download records (inline)
# ===========================================================================

@router.post("/api/presets/nodes/{node_id}/downloads", response_model=MutationResponse)
async def add_preset_download(node_id: str, record: PresetDownloadRecord, config: str = "presets"):
    """Append a download record to the node's `downloads[]`."""
    return presets_api_service.add_download(node_id, record, config)


@router.patch("/api/presets/nodes/{node_id}/downloads/{download_id}", response_model=MutationResponse)
async def update_preset_download(
        node_id: str, download_id: str, patch: dict[str, Any], config: str = "presets"
):
    """Partially update a download record — merged and re-validated as a whole."""
    return presets_api_service.update_download(node_id, download_id, patch, config)


@router.delete("/api/presets/nodes/{node_id}/downloads/{download_id}", response_model=MutationResponse)
async def delete_preset_download(node_id: str, download_id: str, config: str = "presets"):
    """Remove a download record from the node's `downloads[]`."""
    return presets_api_service.delete_download(node_id, download_id, config)


# ===========================================================================
# By-label references (preset_ids / download_ids)
# ===========================================================================

@router.post("/api/presets/nodes/{node_id}/refs", response_model=MutationResponse)
async def add_preset_ref(node_id: str, req: RefCreateRequest, config: str = "presets"):
    """Add a reference id to the node's `preset_ids[]` or `download_ids[]`."""
    return presets_api_service.add_ref(node_id, req, config)


@router.delete("/api/presets/nodes/{node_id}/refs/{kind}/{ref_id}", response_model=MutationResponse)
async def delete_preset_ref(node_id: str, kind: str, ref_id: str, config: str = "presets"):
    """Remove a reference id from the node's `preset_ids[]` or `download_ids[]`."""
    return presets_api_service.delete_ref(node_id, kind, ref_id, config)
