"""
Housekeeping for generated poster files.

Images are written to disk before the database row that references them exists,
so any failure in between leaves a file nothing points to. These helpers keep
storage/ from silently filling up with such leftovers.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from config import IMAGES_DIR
from database.models import Post

logger = logging.getLogger(__name__)

# A freshly rendered poster may not have its database row yet, so never sweep
# anything younger than this.
ORPHAN_GRACE_HOURS = 24


def remove_generated_image(image_path: str) -> bool:
    """
    Deletes a generated poster, but only when it really lives inside the storage
    folder — never an arbitrary path that happens to be stored on a record.
    """
    if not image_path:
        return False
    try:
        img = Path(image_path).resolve()
        if img.is_file() and img.parent == IMAGES_DIR.resolve():
            img.unlink()
            return True
        logger.warning(f"Skipped deleting unexpected image path: {image_path}")
    except Exception as e:
        logger.warning(f"Could not delete image {image_path}: {e}")
    return False


def cleanup_orphan_images(db: Session, grace_hours: int = ORPHAN_GRACE_HOURS) -> dict:
    """
    Removes poster files that no post refers to. Runs with the daily job so a
    long-lived install does not accumulate dead files from failed generations.
    """
    if not IMAGES_DIR.exists():
        return {"checked": 0, "removed": 0, "freed_kb": 0}

    referenced = {
        Path(p).name
        for (p,) in db.query(Post.image_filename).filter(Post.image_filename.isnot(None)).all()
        if p
    }
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=grace_hours)).timestamp()

    checked = removed = 0
    freed = 0
    for path in IMAGES_DIR.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        checked += 1
        if path.name in referenced:
            continue
        if path.stat().st_mtime > cutoff:
            continue  # might belong to a generation still in flight
        size = path.stat().st_size
        try:
            path.unlink()
            removed += 1
            freed += size
        except Exception as e:
            logger.warning(f"Could not remove orphan image {path.name}: {e}")

    if removed:
        logger.info(f"[Maintenance] Removed {removed} orphan poster(s), freed {freed/1024:.0f} KB.")
    return {"checked": checked, "removed": removed, "freed_kb": round(freed / 1024, 1)}
