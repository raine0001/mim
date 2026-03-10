from fastapi import APIRouter

from core.manifest import build_manifest
from core.schemas import ManifestResponse

router = APIRouter(tags=["manifest"])


@router.get("/manifest", response_model=ManifestResponse)
def get_manifest() -> ManifestResponse:
    return ManifestResponse(**build_manifest())
