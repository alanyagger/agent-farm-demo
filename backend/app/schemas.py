from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AutomationRequest(BaseModel):
    enabled: bool


class SimulateCredentialStatusRequest(BaseModel):
    status: Literal["ACTIVE", "PENDING", "REVOKED", "EXPIRED", "REJECTED"]


class OpenClawRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class OpenClawRunRequest(OpenClawRequest):
    agent_id: str = Field(default="agent-sprout", alias="agentId", max_length=64)
    instruction: str = Field(min_length=1, max_length=800)


class OpenClawPlotRequest(OpenClawRequest):
    plot_id: str = Field(alias="plotId", min_length=1, max_length=64)


class OpenClawPlantRequest(OpenClawPlotRequest):
    crop_type: Literal["CARROT", "TOMATO", "CORN"] = Field(alias="cropType")


class OpenClawFinishRequest(OpenClawRequest):
    summary: str = Field(min_length=1, max_length=1000)
