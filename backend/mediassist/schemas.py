"""Pydantic request and response schemas exposed by the API."""

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["doctor", "nurse", "billing_executive", "technician", "admin"]


class LoginRequest(BaseModel):
    """Credentials for one of the assignment demo accounts."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    """Role-tagged session response used by the frontend."""

    username: str
    role: Role
    token: str
    collections: list[str]


class LogoutRequest(BaseModel):
    """Token for the demo session to invalidate."""

    token: str = Field(min_length=1)


class LogoutResponse(BaseModel):
    """Confirmation returned after a logout request."""

    logged_out: bool


class ChatRequest(BaseModel):
    """A question and authenticated role submitted to the RAG endpoint."""

    question: str = Field(min_length=1, max_length=4000)
    role: Role


class SourceCitation(BaseModel):
    """Citation metadata returned for each retrieved document chunk."""

    source_document: str
    section_title: str
    collection: str


class ChatResponse(BaseModel):
    """Stable response contract for hybrid and SQL retrieval.

    ``retrieval_type`` is ``none`` when the input guardrail blocked the request
    before any retrieval. ``request_id`` doubles as the LangSmith trace id and the
    key into the structured logs, so support can look up any single answer.
    """

    answer: str
    sources: list[SourceCitation]
    retrieval_type: Literal["hybrid_rag", "sql_rag", "none"]
    role: Role
    request_id: str = ""
    guardrail: Literal["allowed", "blocked"] = "allowed"
