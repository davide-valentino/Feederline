from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_session
from app.domains import (
    capabilities,
    connections,
    content,
    control_room,
    deliveries,
    destinations,
    operator,
    routes,
    sources,
    stream_profiles,
)


app = FastAPI(title="Feederline POC", version="0.1.0")
app.include_router(capabilities.router)
app.include_router(content.router)
app.include_router(connections.router)
app.include_router(sources.router)
app.include_router(destinations.router)
app.include_router(stream_profiles.router)
app.include_router(routes.router)
app.include_router(control_room.router)
app.include_router(deliveries.router)
app.include_router(deliveries.internal_router)
app.include_router(operator.router)


@app.get("/healthz", tags=["system"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["system"])
def readiness_check(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ready"}
