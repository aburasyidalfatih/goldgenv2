import random
import statistics
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from database.models import ContentTopic, Post, PostMetric, PageTopicWeight
from core.fb_client import fetch_post_metrics
from core.taxonomy import SEED_TOPICS

logger = logging.getLogger(__name__)

# Ceiling for topics that produced nothing inside the analysis window. Kept just
# BELOW the neutral 1.0 so a topic performing at this week's average always ranks
# higher than a dormant one — including when only one topic ran this week and its
# relative weight lands exactly on 1.0.
STALE_TOPIC_CAP = 0.95

# The seed taxonomy is the base curriculum. Even when a fundamental performs
# poorly it keeps a floor in the rotation, so the basics never stop being taught;
# AI variants carry no such protection and can fade out entirely.
BASE_TOPIC_WEIGHT_FLOOR = 0.8
VARIANT_WEIGHT_FLOOR = 0.4

# Starting weight kept for a fresh AI variant that has not been measured yet
# (mirrors NEW_TOPIC_WEIGHT in core.topic_evolution).
NEW_VARIANT_BASELINE = 1.4

# Weight for the only topic with fresh results when the page has no earlier
# history to compare it with (e.g. its very first week).
LONE_WINNER_WEIGHT = 1.5

# How far back to look for a page's "typical post" when judging a lone winner.
TYPICAL_LOOKBACK_DAYS = 60

# How far back the nightly metric refresh reaches. Older posts keep the numbers
# already recorded; their reach barely moves after a month anyway.
METRICS_REFRESH_DAYS = 30

# Editorial baseline from the seed taxonomy, keyed by topic_key.
SEED_BASELINE = {t["topic_key"]: t.get("weight", 1.0) for t in SEED_TOPICS}

# Timestamps are stored as naive UTC in SQLite.
def _naive_utc_cutoff(days: int) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


def _as_naive_utc(dt: datetime | None) -> datetime:
    """
    Normalises a timestamp for comparison. A value just written in this session is
    timezone-aware, while the same value reloaded from SQLite is naive — comparing
    the two raises TypeError, which used to crash topic selection.
    """
    if dt is None:
        return datetime.min
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def window_performance(db: Session, days: int = 7, page_id: int | None = None) -> dict:
    """
    Ranks topics by how they performed on posts PUBLISHED within the last `days`.
    Scoped to one Fanspage when `page_id` is given — each page has its own audience.

    Note: PostMetric stores the latest snapshot per post (not a daily history), so
    this measures "posts published in the window, using their current numbers" —
    not "views that happened during the window".
    """
    cutoff = _naive_utc_cutoff(days)

    query = db.query(Post).filter(
        Post.status == "published", Post.published_at.isnot(None), Post.published_at >= cutoff
    )
    if page_id is not None:
        query = query.filter(Post.page_id == page_id)
    posts = query.all()

    per_topic = {}
    for post in posts:
        metric = post.metrics[0] if post.metrics else None
        entry = per_topic.setdefault(post.topic_id, {
            "topic_id": post.topic_id,
            "topic_title": post.topic_title,
            "posts": 0,
            "measured_posts": 0,
            "reach": 0,
            "impressions": 0,
            "reactions": 0,
            "comments": 0,
            "shares": 0,
            "score": 0.0,
            "best_post_title": None,
            "best_post_reach": -1,
        })
        entry["posts"] += 1
        if metric:
            entry["measured_posts"] += 1
            entry["reach"] += metric.reach
            entry["impressions"] += metric.impressions
            entry["reactions"] += metric.reactions
            entry["comments"] += metric.comments
            entry["shares"] += metric.shares
            entry["score"] += metric.calculated_score
            if metric.reach > entry["best_post_reach"]:
                entry["best_post_reach"] = metric.reach
                entry["best_post_title"] = post.visual_title

    ranking = []
    for entry in per_topic.values():
        measured = entry["measured_posts"] or 1
        entry["avg_reach"] = round(entry["reach"] / measured, 1)
        entry["avg_score"] = round(entry["score"] / measured, 1)
        entry["best_post_reach"] = max(0, entry["best_post_reach"])

        topic = db.query(ContentTopic).filter(ContentTopic.id == entry["topic_id"]).first()
        entry["category"] = topic.category if topic else "-"
        entry["core_concept"] = topic.core_concept if topic else ""
        entry["source"] = topic.source if topic else "seed"
        ranking.append(entry)

    # Reach is the headline metric; score breaks ties when Insights is unavailable.
    ranking.sort(key=lambda e: (e["reach"], e["score"]), reverse=True)

    return {
        "window_days": days,
        "page_id": page_id,
        "posts_in_window": len(posts),
        "topics_in_window": len(ranking),
        "total_reach": sum(e["reach"] for e in ranking),
        "ranking": ranking,
    }


def _typical_post_score(db: Session, page_id: int | None, window_days: int) -> float | None:
    """
    Median score of one post published BEFORE the analysis window (within the
    lookback). The median resists a single viral outlier skewing the baseline.
    """
    window_start = _naive_utc_cutoff(window_days)
    lookback_start = _naive_utc_cutoff(TYPICAL_LOOKBACK_DAYS)
    query = (
        db.query(PostMetric.calculated_score)
        .join(Post, Post.id == PostMetric.post_id)
        .filter(
            Post.status == "published",
            Post.published_at.isnot(None),
            Post.published_at < window_start,
            Post.published_at >= lookback_start,
        )
    )
    if page_id is not None:
        query = query.filter(Post.page_id == page_id)
    scores = [s for (s,) in query.all() if s is not None]
    return statistics.median(scores) if scores else None


def baseline_weight_for(topic: ContentTopic) -> float:
    """
    The neutral starting weight for a topic on a page that has no data yet.

    Deliberately NOT the global catalog weight: a brand new Fanspage must start
    from the base curriculum's editorial baseline and learn its OWN audience,
    instead of inheriting what another page's audience happened to like.
    """
    if (topic.source or "seed") == "seed":
        return SEED_BASELINE.get(topic.topic_key, 1.0)
    return NEW_VARIANT_BASELINE


def get_page_weight(db: Session, page_id: int, topic: ContentTopic) -> PageTopicWeight:
    """Per-page learning row, created on first use with the neutral baseline."""
    query = db.query(PageTopicWeight).filter(
        PageTopicWeight.page_id == page_id, PageTopicWeight.topic_id == topic.id
    )
    row = query.first()
    if not row:
        # INSERT OR IGNORE: a manual generate and the autopilot picking the same
        # topic at once used to both insert and fail on the UNIQUE constraint.
        db.execute(
            sqlite_insert(PageTopicWeight)
            .values(page_id=page_id, topic_id=topic.id, weight=baseline_weight_for(topic))
            .on_conflict_do_nothing()
        )
        row = query.first()
    return row

def calculate_metric_score(reactions: int, comments: int, shares: int, reach: int) -> float:
    """
    Weighted engagement score: Shares and Comments are given highest priority,
    as they signify high educational/viral value on Facebook.
    """
    return (shares * 4.0) + (comments * 3.0) + (reactions * 1.5) + (reach * 0.05)

def update_all_post_metrics(
    db: Session,
    page_id: str,
    access_token: str,
    page_row_id: int | None = None,
    max_age_days: int | None = METRICS_REFRESH_DAYS,
) -> list:
    """
    Fetches latest Facebook metrics for published posts and stores them.
    `page_id`/`access_token` are the Facebook credentials; `page_row_id` limits the
    refresh to posts belonging to that managed page (each token only works for its
    own page's posts).

    By default only posts from the last `max_age_days` — plus any post that has
    never been measured — are refreshed. Without that bound the nightly job would
    re-fetch every post ever published, growing forever and risking rate limits.
    Pass `max_age_days=None` to force a full refresh.
    """
    if not page_id or not access_token:
        return []

    query = db.query(Post).filter(
        Post.status == "published",
        Post.fb_post_id.isnot(None)
    )
    if page_row_id is not None:
        query = query.filter(Post.page_id == page_row_id)
    if max_age_days is not None:
        cutoff = _naive_utc_cutoff(max_age_days)
        query = query.filter(
            or_(
                Post.published_at >= cutoff,
                Post.published_at.is_(None),
                ~Post.metrics.any(),          # never measured yet
            )
        )
    published_posts = query.all()

    updated = []
    for post in published_posts:
        try:
            # Raises when Facebook returns nothing usable; the stored numbers
            # are then kept instead of being reset to zero.
            m_data = fetch_post_metrics(page_id, access_token, post.fb_post_id)

            metric = db.query(PostMetric).filter(PostMetric.post_id == post.id).first()
            if not metric:
                metric = PostMetric(
                    post_id=post.id,
                    fb_post_id=post.fb_post_id,
                )
                db.add(metric)

            # Insights unavailable this time (None): keep the last known reach.
            reach = m_data["reach"] if m_data["reach"] is not None else (metric.reach or 0)
            impressions = (m_data["impressions"] if m_data["impressions"] is not None
                           else (metric.impressions or 0))
            score = calculate_metric_score(
                m_data["reactions"],
                m_data["comments"],
                m_data["shares"],
                reach
            )

            metric.reactions = m_data["reactions"]
            metric.comments = m_data["comments"]
            metric.shares = m_data["shares"]
            metric.reach = reach
            metric.impressions = impressions
            metric.calculated_score = score
            metric.last_checked_at = datetime.now(timezone.utc)

            updated.append({
                "post_id": post.id,
                "title": post.visual_title,
                "score": score,
                "reach": reach
            })
        except Exception as e:
            logger.error(f"Error updating metrics for post {post.id}: {e}")

    db.commit()
    return updated

def mark_topic_used(db: Session, topic: ContentTopic, page_id: int | None = None) -> None:
    """Records that a topic was just produced, globally and for the page."""
    topic.posts_count = (topic.posts_count or 0) + 1
    topic.last_used_at = datetime.now(timezone.utc)
    if page_id is not None:
        row = get_page_weight(db, page_id, topic)
        row.posts_count = (row.posts_count or 0) + 1
        row.last_used_at = datetime.now(timezone.utc)


def optimize_topic_weights(db: Session, window_days: int = 7, page_id: int | None = None) -> dict:
    """
    Reinforcement learning cycle:
    Evaluates scores per topic and adjusts the selection weights.

    Recent performance dominates: a topic that won last week outranks one that
    won months ago, so the autopilot follows what the audience wants *now*.

    With `page_id` the calculation uses only that Fanspage's posts and writes to
    that page's own weight table — every page learns its own audience. Without it,
    the global catalog weights are refreshed from all pages combined.
    """
    topics = db.query(ContentTopic).filter(ContentTopic.is_active.isnot(False)).all()
    if not topics:
        return {"message": "Tidak ada topik di database."}

    recent = {e["topic_id"]: e for e in window_performance(db, window_days, page_id)["ranking"]}

    # Baseline weights come from the seed taxonomy; topics that have never been
    # published must keep their editorial priority instead of being reset to 1.0.
    baseline_weights = {t["topic_key"]: t.get("weight", 1.0) for t in SEED_TOPICS}

    # Pass 1: collect per-topic performance (scoped to the page when given).
    stats = []
    for topic in topics:
        post_query = db.query(Post).filter(Post.topic_id == topic.id, Post.status == "published")
        if page_id is not None:
            post_query = post_query.filter(Post.page_id == page_id)
        posts = post_query.all()

        total_sc = 0.0
        total_r = 0
        measured_posts = 0
        for p in posts:
            if p.metrics:
                metric = p.metrics[0]
                total_sc += metric.calculated_score
                total_r += metric.reach
                measured_posts += 1

        avg_reach = float(total_r / measured_posts) if measured_posts else 0.0
        target = get_page_weight(db, page_id, topic) if page_id is not None else topic
        target.posts_count = len(posts)
        target.total_score = total_sc
        target.avg_reach = avg_reach

        recent_entry = recent.get(topic.id)
        lifetime_avg = (total_sc / measured_posts) if measured_posts else 0.0
        recent_avg = recent_entry["avg_score"] if (recent_entry and recent_entry["measured_posts"]) else 0.0

        in_window = recent_avg > 0
        if in_window and lifetime_avg > 0:
            # 70% recent, 30% lifetime: follow the audience's current taste.
            blended = (0.7 * recent_avg) + (0.3 * lifetime_avg)
        elif in_window:
            blended = recent_avg
        elif lifetime_avg > 0:
            blended = lifetime_avg
        else:
            blended = None  # never measured

        stats.append({
            "topic": topic,
            "target": target,   # ContentTopic (global) or PageTopicWeight (per page)
            "blended": blended,
            "in_window": in_window,
            "recent": recent_entry,
        })

    # Pass 2: weights are RELATIVE to the average performer, so the scale works
    # whether a page reaches 100 or 100,000 people per post. The reference is the
    # average of topics that actually ran in the window, so "1.0" means
    # "average for this week".
    in_window_scores = [s["blended"] for s in stats if s["in_window"]]
    all_scores = [s["blended"] for s in stats if s["blended"] is not None]
    reference = (
        sum(in_window_scores) / len(in_window_scores) if in_window_scores
        else (sum(all_scores) / len(all_scores) if all_scores else 0.0)
    )

    # With a single topic in the window, "relative to this week's average" is
    # meaningless: the topic IS the average and always scored exactly 1.0 — barely
    # above dormant topics, so a clear winner was produced LESS often than the rest
    # (exploration skips topics already used). Judge it against what a typical post
    # on this page achieved before the window instead.
    lone_weight = None
    if len(in_window_scores) == 1:
        lone = next(s for s in stats if s["in_window"])
        typical = _typical_post_score(db, page_id, window_days)
        recent_avg = lone["recent"]["avg_score"] if lone["recent"] else 0.0
        if typical and typical > 0:
            # Never below 1.0: it is the only topic with fresh results, so it must
            # stay ahead of topics that produced nothing this window.
            lone_weight = max(1.0, min(3.0, recent_avg / typical))
        else:
            lone_weight = LONE_WINNER_WEIGHT   # no history to compare against yet

    summary = []
    for s in stats:
        topic, target, blended, recent_entry = s["topic"], s["target"], s["blended"], s["recent"]

        is_base = (topic.source or "seed") == "seed"
        floor = BASE_TOPIC_WEIGHT_FLOOR if is_base else VARIANT_WEIGHT_FLOOR

        if s["in_window"] and lone_weight is not None:
            target.weight = round(lone_weight, 2)
        elif blended is not None and reference > 0:
            weight = max(floor, min(3.0, blended / reference))
            if not s["in_window"] and in_window_scores:
                # A topic that produced nothing this window must not outrank a topic
                # that did: it stays in rotation for exploration, capped at baseline.
                weight = max(floor, min(weight, STALE_TOPIC_CAP))
            target.weight = round(weight, 2)
        else:
            # Never measured on this page -> the seeded editorial baseline.
            weight = baseline_weights.get(topic.topic_key, 1.0 if is_base else NEW_VARIANT_BASELINE)
            if in_window_scores:
                # Once real results exist, an untried topic must not outrank a topic
                # that actually performed. Untried topics still get their turn through
                # the 30% exploration branch, which targets the least-used topics.
                weight = max(floor, min(weight, STALE_TOPIC_CAP))
            target.weight = round(weight, 2)

        summary.append({
            "id": topic.id,
            "title": topic.title,
            "category": topic.category,
            "posts_count": target.posts_count or 0,
            "weight": target.weight,
            "avg_reach": target.avg_reach or 0.0,
            "source": topic.source or "seed",
            "recent_reach": recent_entry["reach"] if recent_entry else 0
        })

    db.commit()
    # Sort by weight descending
    summary.sort(key=lambda x: x["weight"], reverse=True)
    return {
        "status": "success",
        "window_days": window_days,
        "page_id": page_id,
        "optimized_topics": summary,
        "winning_topic": summary[0]["title"] if summary else "None"
    }


def optimize_all_pages(db: Session, window_days: int = 7) -> dict:
    """Runs the learning cycle for every managed page, then the global catalog."""
    from database.models import FacebookPage

    results = []
    for page in db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all():
        res = optimize_topic_weights(db, window_days, page.id)
        results.append({
            "page_id": page.id,
            "page_name": page.name,
            "winning_topic": res.get("winning_topic"),
        })

    global_res = optimize_topic_weights(db, window_days, None)
    return {
        "status": "success",
        "window_days": window_days,
        "pages": results,
        "global": global_res,
    }


def page_topic_weights(db: Session, page_id: int, topics: list) -> dict:
    """
    Weight per topic for one page. Topics this page has no learning row for fall
    back to the neutral curriculum baseline — never to another page's results.
    """
    rows = {
        r.topic_id: r for r in
        db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page_id).all()
    }
    return {
        t.id: (rows[t.id].weight if t.id in rows and rows[t.id].weight else baseline_weight_for(t))
        for t in topics
    }


def get_next_recommended_topic(db: Session, page_id: int | None = None) -> ContentTopic:
    """
    70% Exploit (Weighted selection based on performance)
    30% Explore (Picks underrepresented topics to test fresh ideas)

    When `page_id` is given the draw uses that page's own learned weights and its
    own usage history, so two Fanspages do not converge on identical content.
    """
    topics = db.query(ContentTopic).filter(ContentTopic.is_active.isnot(False)).all()
    if not topics:
        raise ValueError("Database topik kosong.")

    weights_by_topic = (
        page_topic_weights(db, page_id, topics) if page_id is not None
        else {t.id: (t.weight or 1.0) for t in topics}
    )

    if page_id is not None:
        rows = {
            r.topic_id: r for r in
            db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page_id).all()
        }
        usage = {
            t.id: (
                rows[t.id].posts_count or 0 if t.id in rows else 0,
                _as_naive_utc(rows[t.id].last_used_at if t.id in rows else None),
            )
            for t in topics
        }
    else:
        usage = {t.id: (t.posts_count or 0, _as_naive_utc(t.last_used_at)) for t in topics}

    # 30% chance for Exploration: pick from topics this page used least.
    # Ties are broken randomly across ALL equally-unused topics, otherwise a fresh
    # page would always start with the same three topics (lowest id wins the sort).
    if random.random() < 0.3:
        least_used_topics = sorted(topics, key=lambda t: usage[t.id])
        fewest = usage[least_used_topics[0].id][0]
        tied = [t for t in least_used_topics if usage[t.id][0] == fewest]
        pool = tied if len(tied) >= 3 else least_used_topics[:3]
        selected = random.choice(pool)
        logger.info(f"[Feedback Loop] EXPLORE Mode selected topic: {selected.title}")
        return selected

    # 70% chance for Exploitation: weighted lottery.
    # Weights are squared so a clear weekly winner actually dominates the draw
    # instead of being diluted across ten near-equal topics.
    weights = [max(0.1, weights_by_topic[t.id]) ** 2 for t in topics]
    selected = random.choices(topics, weights=weights, k=1)[0]
    logger.info(
        f"[Feedback Loop] EXPLOIT Mode selected topic: {selected.title} "
        f"(weight: {weights_by_topic[selected.id]})"
    )
    return selected
