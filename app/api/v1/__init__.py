from fastapi import APIRouter

from app.api.v1 import auth, files, health, internal, review

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(files.router)
api_router.include_router(review.router)
api_router.include_router(internal.router)
