"""Versioned accepted-input contract; capture and execution wiring belongs to BL-02B."""

import uuid
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.generation_options import GenerationMode


class InputReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: uuid.UUID
    object_key: str = Field(min_length=1, max_length=512)
    reference_role: Literal["FIRST_FRAME"]

    @field_validator("object_key")
    @classmethod
    def private_object_key(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or ":" in value
            or path.is_absolute()
            or path.as_posix() != value
            or any(p in {".", ".."} for p in value.split("/"))
        ):
            raise ValueError("reference must use a canonical private object key")
        return value


class JobInputSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str | None = Field(max_length=4000)
    references: tuple[InputReference, ...] = Field(max_length=1)
    duration_ms: int = Field(strict=True, gt=0, le=2147483647)
    resolution: Literal["720P", "1080P"]
    aspect_ratio: Literal["16:9", "9:16"]
    mode: GenerationMode

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value
