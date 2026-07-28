from __future__ import annotations

from typing import Any, Protocol, Sequence

from .config import settings


class ModelProviderError(RuntimeError):
    """Raised when a configured model provider cannot run a safe agent turn."""


class ModelProvider(Protocol):
    name: str
    model_name: str

    def bind_tools(self, tools: Sequence[Any]) -> Any:
        """Return a chat model bound to the allow-listed LangChain tools."""


class DeepSeekModelProvider:
    name = "deepseek"

    def __init__(self) -> None:
        if not settings.deepseek_api_key:
            raise ModelProviderError(
                "DEEPSEEK_API_KEY is not configured; LLM mode cannot start"
            )
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise ModelProviderError(
                "DeepSeek LangChain dependencies are not installed"
            ) from exc

        self.model_name = settings.deepseek_model
        self._model = ChatDeepSeek(
            model=self.model_name,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
        )

    def bind_tools(self, tools: Sequence[Any]) -> Any:
        return self._model.bind_tools(list(tools))


class FakeModelProvider:
    """Small deterministic provider used by integration tests, never by config."""

    name = "fake"
    model_name = "fake-skill-agent"

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools: Sequence[Any]) -> Any:
        provider = self

        class BoundFakeModel:
            def invoke(self, _: Sequence[Any]) -> Any:
                if not provider._responses:
                    raise ModelProviderError("Fake model has no response left")
                return provider._responses.pop(0)

        return BoundFakeModel()


def build_model_provider() -> ModelProvider:
    provider_name = settings.model_provider.lower().strip()
    if provider_name == "deepseek":
        return DeepSeekModelProvider()
    raise ModelProviderError(f"Unsupported MODEL_PROVIDER: {settings.model_provider}")


def runtime_descriptor() -> dict[str, str | bool | int]:
    mode = "llm" if settings.llm_runtime_enabled else "rules"
    return {
        "mode": mode,
        "provider": settings.model_provider if mode == "llm" else "rules",
        "model": settings.deepseek_model if mode == "llm" else "deterministic-rules",
        "configured": bool(settings.deepseek_api_key) if mode == "llm" else True,
        "maxToolRounds": settings.agent_max_tool_rounds,
    }
