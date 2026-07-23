from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent_routes import public_router as agent_public_router
from app.api.agent_routes import router as agent_router
from app.api.auth_routes import router as auth_router
from app.api.discovery_routes import router as discovery_router
from app.api.mail_routes import router as mail_router
from app.api.morning_crawl_routes import router as morning_crawl_router
from app.api.routes import router
from app.api.source_routes import router as source_router
from app.api.statistics_routes import router as statistics_router
from app.api.wechat_auth_routes import router as wechat_auth_router


def create_app(*, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="OS News Tracker", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(source_router)
    app.include_router(agent_router)
    app.include_router(agent_public_router)
    app.include_router(discovery_router)
    app.include_router(mail_router)
    app.include_router(morning_crawl_router)
    app.include_router(statistics_router)
    app.include_router(wechat_auth_router)
    return app


app = create_app()
