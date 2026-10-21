from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.assistant import router as assistant_router
from .api.admin import UPLOAD_DIR, router as admin_router
from .api.chat import router as chat_router
from .api.currency import router as currency_router
from .api.products import router as products_router
from .api.requests import router as requests_router
from .database import SessionLocal, ensure_catalog_schema
from .migration import relink_contact_requests
from .seed import seed_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Ссылки заявок снимаются до пересоздания каталога и восстанавливаются после пересидирования.
    links = ensure_catalog_schema()
    with SessionLocal() as db:
        seed_database(db)
        relink_contact_requests(db, links)
    yield


app = FastAPI(title="AI Sneaker Store Assistant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


app.include_router(products_router)
app.include_router(admin_router)
app.include_router(assistant_router)
app.include_router(chat_router)
app.include_router(currency_router)
app.include_router(requests_router)

# Owner uploads are served from the existing product-image tree, outside the admin API.
app.mount("/images/sneakers/uploads", StaticFiles(directory=UPLOAD_DIR, check_dir=False), name="uploaded-sneakers")

# In the production demo, serve the built frontend from the same origin as the API.
# This keeps the public Cloudflare URL to a single origin and avoids CORS/network issues.
frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(frontend_dist / "index.html")

    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
