"""Groq-backed language model gateway with an offline development fallback."""

from collections.abc import Sequence

from langsmith import traceable

from mediassist.config import Settings
from mediassist.observability import add_trace_metadata, record_usage, stage_timer


class GroqLLM:
    """Call Groq for generation while keeping tests and demos deterministic offline."""

    def __init__(self, settings: Settings):
        """Create the client lazily so importing the API never requires a key."""

        self.settings = settings
        self.client = None
        if settings.groq_api_key and settings.groq_model:
            from groq import Groq

            # The Groq SDK also reads GROQ_BASE_URL from the environment, but that
            # variable holds the OpenAI-compatible URL (".../openai/v1") for the
            # judge model; the SDK appends "/openai/v1" itself, so pass the bare host.
            base_url = settings.groq_base_url.rstrip("/").removesuffix("/openai/v1")
            # Bounded timeout/retries: a rate-limited call must fail fast, not stall a request for minutes.
            self.client = Groq(api_key=settings.groq_api_key, base_url=base_url, timeout=60.0, max_retries=2)

    @traceable(name="MediBot: Groq Generation", run_type="llm")
    def complete(self, system: str, user: str) -> str:
        """Generate one response using the configured Groq model or a safe fallback."""

        if self.client is None:
            return "Groq is not configured. The retrieved context is available in the source citations."
        try:
            with stage_timer("generation"):
                response = self.client.chat.completions.create(
                    model=self.settings.groq_model,
                    temperature=0,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
        except Exception:
            # Keep the local demo usable when the key, model, or network is unavailable.
            return "The language model is unavailable right now. Please check the Groq configuration; the source citations below show the retrieved context."
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            record_usage("generation", prompt_tokens, completion_tokens)
            add_trace_metadata(
                {
                    "ls_provider": "groq",
                    "ls_model_name": self.settings.groq_model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            )
        return response.choices[0].message.content or "No answer was generated."

    def generate_sql(self, question: str, schema: str) -> str:
        """Ask Groq for a read-only SQL statement from a constrained schema."""

        return self.complete(
            "Return only one safe SQLite SELECT statement. Never modify data.",
            f"Schema:\n{schema}\n\nQuestion: {question}",
        )

    def answer_from_rows(self, question: str, rows: Sequence[dict]) -> str:
        """Turn SQL result rows into a concise natural-language answer."""

        return self.complete("Answer from the supplied database rows only.", f"Question: {question}\nRows: {list(rows)}")


def build_judge_chat_model(settings: Settings, temperature: float = 0.0):
    """Return a LangChain chat model for the separate judge/guardrail model on Groq.

    Groq exposes an OpenAI-compatible endpoint, so ``langchain-openai`` is used
    (``langchain-groq`` pins ``groq<1`` which conflicts with this project).
    """

    if not settings.groq_api_key or not settings.judge_model:
        raise RuntimeError("GROQ_API_KEY and JUDGE_MODEL must be set for the judge model.")
    from langchain_openai import ChatOpenAI

    options = {"reasoning_effort": settings.judge_reasoning_effort} if settings.judge_reasoning_effort else {}
    return ChatOpenAI(
        model=settings.judge_model,
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        temperature=temperature,
        max_retries=2,
        timeout=60,
        **options,
    )
