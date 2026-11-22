from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    api_version: Literal["v1"] = "v1"


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health()
