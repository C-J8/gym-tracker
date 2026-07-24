from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError

from gym_tracker.config import Settings
from gym_tracker.schemas import ExercisePayload, SetPayload, WorkoutExtraction
from gym_tracker.services.llm_extractor import MockWorkoutExtractor, OpenAIWorkoutExtractor
from gym_tracker.services.metrics import estimate_epley_1rm, prepare_dashboard_dataframe


def extraction() -> WorkoutExtraction:
    return WorkoutExtraction(
        workout_date="2026-01-01",
        exercises=[
            ExercisePayload(
                raw_name="Supino",
                canonical_name="Supino reto",
                muscle_group="Peito",
                equipment="Halter",
                sets=[SetPayload(weight_kg=20, reps=10)],
            )
        ],
    )


def test_mock_extractor_valid_and_invalid_outputs() -> None:
    mock = MockWorkoutExtractor(result=extraction())
    assert mock.extract("texto", "2026-01-01").exercises[0].sets[0].reps == 10
    with pytest.raises(ValidationError):
        WorkoutExtraction.model_validate({"workout_date": "2026-01-01", "exercises": [{"raw_name": "x"}]})


def test_openai_extractor_does_not_call_without_key() -> None:
    extractor = OpenAIWorkoutExtractor(Settings(openai_api_key=None))
    assert extractor.available is False
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        extractor.extract("texto", "2026-01-01")


def test_openai_extractor_uses_responses_structured_output() -> None:
    result = extraction()

    class Responses:
        def parse(self, **kwargs):
            assert kwargs["text_format"] is WorkoutExtraction
            return SimpleNamespace(output_parsed=result)

    client = SimpleNamespace(responses=Responses())
    extractor = OpenAIWorkoutExtractor(Settings(openai_api_key="test-key"), client=client)
    assert extractor.extract("texto", "2026-01-01") == result


def test_metrics_and_dashboard_adapter_types() -> None:
    frame = pd.DataFrame(
        [
            {
                "data": "2026-01-01",
                "grupo_muscular": "Peito",
                "exercicio": "Supino reto",
                "tipo": "Halter",
                "peso_kg": 60,
                "serie": 1,
                "repeticoes": 10,
            }
        ]
    )
    prepared = prepare_dashboard_dataframe(frame)
    assert prepared.iloc[0]["volume"] == 600
    assert prepared.iloc[0]["estimativa_1rm"] == pytest.approx(80)
    assert estimate_epley_1rm(60, 10) == pytest.approx(80)
    assert str(prepared["repeticoes"].dtype) == "Int64"
