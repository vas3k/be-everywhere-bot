from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, select

from db.schema import accounts, projects, source_posts, sync_mappings


@dataclass(frozen=True)
class HistoryEntry:
    occurred_at: datetime
    kind: str
    project: str
    summary: str
    detail: str = ""


def recent_history(engine, limit: int = 100) -> list[HistoryEntry]:
    if limit <= 0:
        return []
    source = accounts.alias("source")
    destination = accounts.alias("destination")
    entries = []
    with engine.connect() as connection:
        deliveries = connection.execute(select(
            sync_mappings.c.synced_at, sync_mappings.c.source_post_id, sync_mappings.c.dest_post_id,
            source.c.network.label("source_network"), source.c.label.label("source_label"),
            source.c.deleted_at.label("source_removed"), source.c.id.label("source_id"),
            destination.c.network.label("dest_network"), destination.c.label.label("dest_label"),
            destination.c.deleted_at.label("dest_removed"), destination.c.id.label("dest_id"),
            projects.c.name.label("project"), source_posts.c.payload,
        ).select_from(
            sync_mappings.join(source, source.c.id == sync_mappings.c.source_account_id)
            .join(destination, destination.c.id == sync_mappings.c.dest_account_id)
            .join(projects, projects.c.id == source.c.project_id)
            .outerjoin(source_posts, and_(
                source_posts.c.account_id == sync_mappings.c.source_account_id,
                source_posts.c.post_id == sync_mappings.c.source_post_id,
            ))
        ).order_by(sync_mappings.c.synced_at.desc(), sync_mappings.c.id.desc()).limit(limit)).mappings()
        for row in deliveries:
            source_name = f"removed #{row['source_id']}" if row["source_removed"] else row["source_label"]
            dest_name = f"removed #{row['dest_id']}" if row["dest_removed"] else row["dest_label"]
            text = " ".join((row["payload"] or {}).get("text", "").split())
            preview = text[:180] + ("…" if len(text) > 180 else "")
            detail = f"Post {row['source_post_id']} → {row['dest_post_id']}"
            if preview:
                detail = f"{preview}\n{detail}"
            entries.append(HistoryEntry(
                row["synced_at"], "SYNCED", row["project"],
                f"{row['source_network']}/{source_name} → {row['dest_network']}/{dest_name}", detail,
            ))

        cycles = connection.execute(select(projects).where(
            projects.c.last_sync_finished_at.is_not(None),
        ).order_by(projects.c.last_sync_finished_at.desc(), projects.c.id.desc()).limit(limit)).mappings()
        for row in cycles:
            errors = row["last_sync_error_count"]
            entries.append(HistoryEntry(
                row["last_sync_finished_at"], "ERRORS" if errors else "CYCLE", row["name"],
                f"Latest cycle · {row['last_sync_count']} deliveries · {errors} errors",
            ))

        for column, kind, action in (
            (accounts.c.created_at, "ACCOUNT", "Connected"),
            (accounts.c.deleted_at, "REMOVED", "Removed"),
        ):
            changes = connection.execute(select(
                column.label("occurred_at"), accounts.c.id, accounts.c.network, accounts.c.label,
                accounts.c.deleted_at, projects.c.name.label("project"),
            ).select_from(accounts.join(projects)).where(column.is_not(None)).order_by(
                column.desc(), accounts.c.id.desc(),
            ).limit(limit)).mappings()
            for row in changes:
                label = f"removed #{row['id']}" if row["deleted_at"] else row["label"]
                entries.append(HistoryEntry(
                    row["occurred_at"], kind, row["project"], f"{action} {row['network']}/{label}",
                ))

        created = connection.execute(select(projects.c.created_at, projects.c.name).order_by(
            projects.c.created_at.desc(), projects.c.id.desc(),
        ).limit(limit))
        for occurred_at, name in created:
            entries.append(HistoryEntry(occurred_at, "PROJECT", name, "Project created"))

    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries[:limit]
