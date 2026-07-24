from abc import ABC, abstractmethod
from dataclasses import dataclass

from gym_tracker.config import Settings, get_settings
from gym_tracker.schemas import WorkoutExtraction

PROMPT_VERSION = "workout-extraction-v1"
SYSTEM_PROMPT = """Extraia registros de treino da mensagem de WhatsApp.
Nao invente valores. Marque needs_review quando houver ambiguidade, unidade ausente,
exercicio desconhecido ou texto relevante nao consumido. Retorne somente o schema solicitado."""


class WorkoutExtractor(ABC):
    model: str | None = None
    prompt_version: str = PROMPT_VERSION

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def extract(self, message: str, workout_date: str) -> WorkoutExtraction: ...


class OpenAIWorkoutExtractor(WorkoutExtractor):
    def __init__(self, settings: Settings | None = None, client=None) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.openai_model
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key)

    def extract(self, message: str, workout_date: str) -> WorkoutExtraction:
        if not self.available:
            raise RuntimeError("OPENAI_API_KEY ausente; extracao por LLM desativada")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.settings.openai_api_key)
        response = self._client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Data do treino: {workout_date}\n\n{message}"},
            ],
            text_format=WorkoutExtraction,
        )
        if response.output_parsed is None:
            raise ValueError("a LLM nao retornou uma extracao estruturada")
        return response.output_parsed


@dataclass
class MockWorkoutExtractor(WorkoutExtractor):
    result: WorkoutExtraction | None = None
    error: Exception | None = None
    model: str | None = "mock-model"
    prompt_version: str = PROMPT_VERSION
    calls: int = 0

    @property
    def available(self) -> bool:
        return True

    def extract(self, message: str, workout_date: str) -> WorkoutExtraction:
        self.calls += 1
        if self.error:
            raise self.error
        if self.result is None:
            raise ValueError("mock sem resultado configurado")
        return self.result
