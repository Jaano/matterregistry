from sqlmodel import Session, create_engine

from .settings import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


def get_session():
    with Session(engine) as session:
        yield session
