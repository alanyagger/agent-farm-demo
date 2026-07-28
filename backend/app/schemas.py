from typing import Literal

from pydantic import BaseModel


class AutomationRequest(BaseModel):
    enabled: bool


class SimulateCredentialStatusRequest(BaseModel):
    status: Literal["ACTIVE", "PENDING", "REVOKED", "EXPIRED", "REJECTED"]
