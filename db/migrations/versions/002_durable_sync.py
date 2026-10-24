"""Add a durable inbox and checkpoints for partially published threads."""

from db.schema import source_posts, sync_batches

VERSION = "002_durable_sync"


def upgrade(engine):
    with engine.begin() as connection:
        source_posts.create(connection, checkfirst=True)
        sync_batches.create(connection, checkfirst=True)
