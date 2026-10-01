"""FastAPI entry point for the MediBot application."""

from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .guardrails import GuardrailService
from .ingestion import ingest_documents
from .llm import GroqLLM
from .observability import EventLogger
from .pipeline import ChatPipeline
from .rbac import authenticate, allowed_collections, create_session, revoke_session
from .retrieval import HybridRetriever
from .schemas import ChatRequest, ChatResponse, LoginRequest, LoginResponse, LogoutRequest, LogoutResponse

app = FastAPI(title="MediBot API", version="0.2.0", description="RBAC-aware Advanced RAG assistant for MediAssist, wrapped in an evaluation & guardrail layer.")
settings = get_settings()
DATA_DIR = settings.database_file.parents[1].parent / "mediassist_data"
INGESTION_CACHE = DATA_DIR.parent / ".cache" / "docling_chunks.json"
retriever = HybridRetriever(ingest_documents(DATA_DIR, INGESTION_CACHE), settings=settings)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@lru_cache
def get_llm() -> GroqLLM:
    """Create one shared Groq gateway."""

    return GroqLLM(settings)


@lru_cache
def get_event_logger() -> EventLogger:
    """Create one shared structured-log writer."""

    return EventLogger(settings.log_path)


@lru_cache
def get_guardrails() -> GuardrailService:
    """Create one shared guardrail service (the OpenEvals judge is built lazily)."""

    return GuardrailService(settings, get_event_logger())


def get_pipeline() -> ChatPipeline:
    """Assemble the guarded chat pipeline from the shared components."""

    return ChatPipeline(retriever, get_llm(), get_guardrails(), get_event_logger(), settings)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a lightweight service health response."""

    return {"status": "ok", "service": "medibot-backend"}


@app.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    """Authenticate a demo user and return the role plus accessible collections."""

    user = authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = create_session(user)
    return LoginResponse(username=user.username, role=user.role, token=token, collections=allowed_collections(user.role))


@app.post("/logout", response_model=LogoutResponse)
def logout(payload: LogoutRequest) -> LogoutResponse:
    """Invalidate a demo session token."""

    return LogoutResponse(logged_out=revoke_session(payload.token))


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """Screen the question, route to SQL RAG or Hybrid RAG, then screen the answer."""

    return get_pipeline().run(payload.question, payload.role).to_response()


@app.get("/metrics")
def metrics() -> dict[str, object]:
    """Aggregate guardrail allow/block counts, latency, and token usage from the logs."""

    return get_event_logger().metrics()


@app.get("/requests/{request_id}")
def request_detail(request_id: str) -> dict[str, object]:
    """Reconstruct one request (retrieval, prompt, answer, guardrail decisions) from the logs."""

    detail = get_event_logger().find_request(request_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Unknown request id.")
    return detail


@app.get("/collections/{role}")
def collections(role: str) -> dict[str, object]:
    """Return permitted collections for a role, useful for the frontend badge."""

    if role not in {"doctor", "nurse", "billing_executive", "technician", "admin"}:
        raise HTTPException(status_code=404, detail="Unknown role.")
    return {"role": role, "collections": allowed_collections(role)}
