"""Compatibility shim for ragas 0.4.x with langchain-community 0.4.x.

``ragas.llms.base`` imports ``ChatVertexAI`` / ``VertexAI`` from langchain-community,
which removed those modules. ragas only uses the two classes inside an ``isinstance``
check (which providers support ``n>1`` completions), so placeholder classes are safe:
no real object is ever an instance of them. This must run before ``import ragas``.
"""

import sys
import types


def install_vertexai_placeholders() -> None:
    try:
        from langchain_community.chat_models.vertexai import ChatVertexAI  # noqa: F401

        return
    except Exception:
        pass

    class ChatVertexAI:  # pragma: no cover - placeholder only
        """Placeholder for the removed langchain-community Vertex AI chat model."""

    class VertexAI:  # pragma: no cover - placeholder only
        """Placeholder for the removed langchain-community Vertex AI LLM."""

    module = types.ModuleType("langchain_community.chat_models.vertexai")
    module.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = module

    import langchain_community.llms as community_llms

    try:
        community_llms.VertexAI  # noqa: B018
    except Exception:
        community_llms.VertexAI = VertexAI


install_vertexai_placeholders()
