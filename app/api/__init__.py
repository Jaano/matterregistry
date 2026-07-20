from fastapi import APIRouter

from .attachments import router as attachments_router
from .devices import router as devices_router
from .export import router as export_router
from .import_ import router as import_router
from .integrations import router as integrations_router
from .products import router as products_router
from .properties import router as properties_router

api_router = APIRouter()
api_router.include_router(devices_router)
api_router.include_router(properties_router)
api_router.include_router(products_router)
api_router.include_router(attachments_router)
api_router.include_router(export_router)
api_router.include_router(import_router)
api_router.include_router(integrations_router)
