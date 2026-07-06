from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import models as orm
from app.db.account_scope import provider_account_history
from app.db.base import Base


def _job(job_id: str, source_account_id: str, target_account_id: str) -> orm.MigrationJob:
    return orm.MigrationJob(
        id=job_id,
        user_id="local",
        source_provider="spotify",
        source_account_id=source_account_id,
        target_provider="ytmusic",
        target_account_id=target_account_id,
        selection={},
    )


def test_provider_account_history_matches_reconnected_accounts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                orm.ProviderAccount(
                    id="current-source",
                    user_id="local",
                    provider="spotify",
                    provider_user_id="source-current",
                ),
                orm.ProviderAccount(
                    id="current-target",
                    user_id="local",
                    provider="ytmusic",
                    provider_user_id="target-current",
                ),
                orm.ProviderAccount(
                    id="prior-source",
                    user_id="local",
                    provider="spotify",
                    provider_user_id="source-prior",
                ),
                orm.ProviderAccount(
                    id="prior-target",
                    user_id="local",
                    provider="ytmusic",
                    provider_user_id="target-prior",
                ),
                orm.ProviderAccount(
                    id="other-user-target",
                    user_id="other",
                    provider="ytmusic",
                    provider_user_id="other-user",
                ),
                _job("current", "current-source", "current-target"),
                _job("prior-same-provider", "prior-source", "prior-target"),
                _job("deleted-during-reconnect", "deleted-source", "deleted-target"),
                _job("other-user-account", "current-source", "other-user-target"),
            ]
        )
        session.commit()

        rows = session.scalars(
            select(orm.MigrationJob.id)
            .where(
                orm.MigrationJob.source_provider == "spotify",
                provider_account_history(
                    orm.MigrationJob.source_account_id,
                    current_account_id="current-source",
                    user_id="local",
                    provider="spotify",
                ),
                orm.MigrationJob.target_provider == "ytmusic",
                provider_account_history(
                    orm.MigrationJob.target_account_id,
                    current_account_id="current-target",
                    user_id="local",
                    provider="ytmusic",
                ),
            )
            .order_by(orm.MigrationJob.id)
        ).all()

    assert rows == ["current", "deleted-during-reconnect", "prior-same-provider"]
