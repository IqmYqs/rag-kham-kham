import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000, examples=["รองเท้า"])
    mode: Literal["auto", "ai_only", "library_only"] = Field(
        default="auto",
        description="auto = library first, AI last; ai_only = skip the library; "
        "library_only = never let the AI invent jokes",
    )


class Citation(BaseModel):
    file_id: uuid.UUID
    file_name: str
    chunk_id: uuid.UUID
    location: dict[str, Any] | None = None
    content: str


class Confidence(BaseModel):
    retrieval: float | None = Field(description="best cosine similarity from vector search")
    llm: float | None = Field(description="CONF reported by the model (md stage)")


class Usage(BaseModel):
    model: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal


class AskOut(BaseModel):
    answer_id: uuid.UUID | None = Field(description="null for the env bootstrap admin key")
    answer: str
    intent: Literal["generate", "data", "lookup", "request", "chat"]
    source_stage: Literal["cache", "md", "source_file", "ai", "sql", "none"]
    is_ai_generated: bool
    notice: str | None = Field(
        default=None, description="e.g. 'AI แต่งเอง ไม่ได้มาจากคลัง' for AI answers"
    )
    direct: bool = Field(description="original library text returned without calling an LLM")
    citations: list[Citation]
    confidence: Confidence
    stages_tried: list[str]
    usage: Usage
    latency_ms: int


class RandomOut(BaseModel):
    content: str
    citation: Citation
