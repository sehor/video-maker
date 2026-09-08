"""Public HTTP router assembly; use cases live in application modules."""
from fastapi import APIRouter

from app.assets_api import router as assets_router
from app.billing_api import router as billing_router
from app.generations_api import router as generations_router
from app.projects_api import router as projects_router

router = APIRouter()
router.include_router(projects_router)
router.include_router(assets_router)
router.include_router(billing_router)
router.include_router(generations_router)
