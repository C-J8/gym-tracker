from sqlalchemy import select
from sqlalchemy.orm import Session

from gym_tracker.models import DataRevision

DATA_REVISION_ID = 1


def current_data_revision(session: Session) -> int:
    revision = session.get(DataRevision, DATA_REVISION_ID)
    return revision.version if revision else 0


def bump_data_revision(session: Session) -> int:
    revision = session.scalar(select(DataRevision).where(DataRevision.id == DATA_REVISION_ID).with_for_update())
    if revision is None:
        revision = DataRevision(id=DATA_REVISION_ID, version=1)
        session.add(revision)
    else:
        revision.version += 1
    session.flush()
    return revision.version
