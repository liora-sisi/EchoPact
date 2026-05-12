from fastapi import FastAPI
from .routes import router
from ..utils.db import init_db

app = FastAPI(
    title="Echo Pact API",
    description="保长的崽，野AI搭的骨架",
    version="0.1.0"
)

@app.on_event("startup")
async def startup():
    init_db()

app.include_router(router, prefix="/api")

@app.get("/")
async def root():
    return {"msg": "Echo Pact 活着呢", "status": "ok"}
