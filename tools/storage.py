"""
SQLite storage for content curation.
Uses aiosqlite for async database operations.
"""

import aiosqlite
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

# Database path - uses Render disk in production
DB_PATH = os.environ.get("DB_PATH", "db/curator.db")


def get_db_path() -> Path:
    """Get database path, ensuring parent directory exists."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


async def init_db():
    """Initialize database with schema."""
    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    async with aiosqlite.connect(get_db_path()) as db:
        with open(schema_path) as f:
            await db.executescript(f.read())
        await db.commit()


# Candidates operations

async def add_candidate(
    niche: str,
    image_url: str,
    source_url: Optional[str] = None,
    source_name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    curator_notes: Optional[str] = None,
    quality_score: Optional[int] = None,
) -> int:
    """Add a new candidate image for review."""
    async with aiosqlite.connect(get_db_path()) as db:
        try:
            cursor = await db.execute(
                """
                INSERT INTO candidates
                (niche, image_url, source_url, source_name, title, description, curator_notes, quality_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (niche, image_url, source_url, source_name, title, description, curator_notes, quality_score),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            # Duplicate image URL
            return -1


async def get_pending_candidates(niche: str, limit: int = 10) -> list[dict]:
    """Get pending candidates for review."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM candidates
            WHERE niche = ? AND status = 'pending'
            ORDER BY quality_score DESC, found_at DESC
            LIMIT ?
            """,
            (niche, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_candidate(candidate_id: int) -> Optional[dict]:
    """Get a specific candidate by ID."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM candidates WHERE id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def approve_candidate(candidate_id: int) -> bool:
    """Approve a candidate for posting."""
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            UPDATE candidates
            SET status = 'approved', reviewed_at = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), candidate_id),
        )
        await db.commit()
        return True


async def reject_candidate(candidate_id: int, reason: Optional[str] = None) -> bool:
    """Reject a candidate."""
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            UPDATE candidates
            SET status = 'rejected', reviewed_at = ?, rejection_reason = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), reason, candidate_id),
        )
        await db.commit()
        return True


# Approved operations

async def add_to_approved(
    candidate_id: int,
    caption: str,
    hashtags: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
) -> int:
    """Add approved content to posting queue."""
    async with aiosqlite.connect(get_db_path()) as db:
        # Get candidate info
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT niche, image_url FROM candidates WHERE id = ?",
            (candidate_id,),
        )
        candidate = await cursor.fetchone()
        if not candidate:
            return -1

        cursor = await db.execute(
            """
            INSERT INTO approved
            (niche, candidate_id, image_url, caption, hashtags, scheduled_for)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate["niche"],
                candidate_id,
                candidate["image_url"],
                caption,
                hashtags,
                scheduled_for.isoformat() if scheduled_for else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_ready_to_post(niche: str, limit: int = 1) -> list[dict]:
    """Get approved content ready to post."""
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM approved
            WHERE niche = ? AND status = 'pending'
            AND (scheduled_for IS NULL OR scheduled_for <= ?)
            ORDER BY approved_at ASC
            LIMIT ?
            """,
            (niche, datetime.utcnow().isoformat(), limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_as_posted(approved_id: int, platform: str, post_id: str) -> int:
    """Move approved content to posted."""
    async with aiosqlite.connect(get_db_path()) as db:
        # Get approved info
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM approved WHERE id = ?",
            (approved_id,),
        )
        approved = await cursor.fetchone()
        if not approved:
            return -1

        # Insert into posted
        cursor = await db.execute(
            """
            INSERT INTO posted
            (niche, approved_id, platform, post_id, image_url, caption)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                approved["niche"],
                approved_id,
                platform,
                post_id,
                approved["image_url"],
                approved["caption"],
            ),
        )

        # Update approved status
        await db.execute(
            "UPDATE approved SET status = 'posted' WHERE id = ?",
            (approved_id,),
        )

        await db.commit()
        return cursor.lastrowid


# Analytics

async def get_stats(niche: str) -> dict:
    """Get statistics for a niche."""
    async with aiosqlite.connect(get_db_path()) as db:
        stats = {}

        # Candidates count by status
        cursor = await db.execute(
            """
            SELECT status, COUNT(*) as count
            FROM candidates WHERE niche = ?
            GROUP BY status
            """,
            (niche,),
        )
        rows = await cursor.fetchall()
        stats["candidates"] = {row[0]: row[1] for row in rows}

        # Approved count by status
        cursor = await db.execute(
            """
            SELECT status, COUNT(*) as count
            FROM approved WHERE niche = ?
            GROUP BY status
            """,
            (niche,),
        )
        rows = await cursor.fetchall()
        stats["approved"] = {row[0]: row[1] for row in rows}

        # Posted count
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posted WHERE niche = ?",
            (niche,),
        )
        stats["posted_total"] = (await cursor.fetchone())[0]

        return stats


async def is_image_known(image_url: str) -> bool:
    """Check if we've already seen this image URL."""
    async with aiosqlite.connect(get_db_path()) as db:
        cursor = await db.execute(
            "SELECT 1 FROM candidates WHERE image_url = ? LIMIT 1",
            (image_url,),
        )
        return await cursor.fetchone() is not None


async def log_source_scrape(
    source_url: str,
    source_name: str,
    images_found: int,
    status: str = "success",
):
    """Log a source scrape attempt."""
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO source_log (source_url, source_name, images_found, status)
            VALUES (?, ?, ?, ?)
            """,
            (source_url, source_name, images_found, status),
        )
        await db.commit()
