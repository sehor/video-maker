"""Compatibility facade that assembles physically separated API routers."""

from fastapi import APIRouter

from app.admin_api import router as admin_router
from app.provider_api import router as provider_router
from app.public_api import router as public_router

router = APIRouter()
router.include_router(public_router)
router.include_router(admin_router)
router.include_router(provider_router)

__all__ = ["router"]
