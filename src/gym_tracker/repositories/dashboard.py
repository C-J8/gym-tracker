import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.models import Exercise, ExerciseVariant, ParseResult, User, Workout, WorkoutSet


class DashboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def workout_dataframe(self, user_id: uuid.UUID | None = None) -> pd.DataFrame:
        statement = (
            select(
                Workout.workout_date.label("data"),
                Exercise.muscle_group.label("grupo_muscular"),
                Exercise.canonical_name.label("exercicio"),
                ExerciseVariant.equipment.label("tipo"),
                WorkoutSet.weight_kg.label("peso_kg"),
                WorkoutSet.set_number.label("serie"),
                WorkoutSet.reps.label("repeticoes"),
            )
            .join(WorkoutSet.workout)
            .join(WorkoutSet.exercise_variant)
            .join(ExerciseVariant.exercise)
            .join(WorkoutSet.parse_result)
            .where(ParseResult.is_active.is_(True))
            .order_by(Workout.workout_date, Exercise.canonical_name, WorkoutSet.set_number)
        )
        if user_id is not None:
            statement = statement.where(Workout.user_id == user_id)
        rows = self.session.execute(statement).mappings().all()
        columns = ["data", "grupo_muscular", "exercicio", "tipo", "peso_kg", "serie", "repeticoes"]
        return pd.DataFrame(rows, columns=columns)

    def users(self) -> list[User]:
        return list(self.session.scalars(select(User).order_by(User.display_name)))
