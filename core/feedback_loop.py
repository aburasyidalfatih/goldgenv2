import random
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import or_
from sqlalchemy.orm import Session
from database.models import ContentTopic, FacebookPage, Post, PostMetric, PageTopicWeight
from core.fb_client import fetch_post_metrics
from core.taxonomy import SEED_TOPICS

logger = logging.getLogger(__name__)

# Ceiling for a topic never measured on a page once that page has real results:
# just BELOW the neutral 1.0, so an untried topic never outranks an average one.
STALE_TOPIC_CAP = 0.95

# The seed taxonomy is the base curriculum. Even when a fundamental performs
# poorly it keeps a floor in the rotation, so the basics never stop being taught;
# AI variants carry no such protection and can fade out entirely.
BASE_TOPIC_WEIGHT_FLOOR = 0.8
VARIANT_WEIGHT_FLOOR = 0.4

# Starting weight kept for a fresh AI variant that has not been measured yet
# (mirrors NEW_TOPIC_WEIGHT in core.topic_evolution).
NEW_VARIANT_BASELINE = 1.4

# A post's reach keeps growing for a day or two after it goes live. Learning only
# trusts posts at least this old, otherwise the newest post always looks weakest.
MATURE_AFTER_HOURS = 48

# Focus phase (every base topic already tested on the page): share of picks that
# still explore the least-used topics, so a shift in the audience's taste is
# noticed. The remaining 90% follow the page's own reach winners.
EXPLORE_RATE = 0.10

# Focus phase rotates over this many top reach winners: #1, #2, #3, #1, ...
FOCUS_ROTATION_SIZE = 3

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


def window_performance(db: Session, days: int = 7, page_id: int | None = None,
                       mature_only: bool = False) -> dict:
    """
    Ranks topics by how they performed on posts PUBLISHED within the last `days`.
    Scoped to one Fanspage when `page_id` is given — each page has its own audience.

    Note: PostMetric stores the latest snapshot per post (not a daily history), so
    this measures "posts published in the window, using their current numbers" —
    not "views that happened during the window".

    `mature_only` skips posts younger than MATURE_AFTER_HOURS, whose reach is still
    climbing; the learning loop uses it, the analytics view does not.
    """
    cutoff = _naive_utc_cutoff(days)

    query = db.query(Post).filter(
        Post.status == "published", Post.published_at.isnot(None), Post.published_at >= cutoff
    )
    if mature_only:
        query = query.filter(Post.published_at <= _mature_cutoff())
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


def _mature_cutoff() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(hours=MATURE_AFTER_HOURS)).replace(tzinfo=None)


def topics_for_page(db: Session, page_id: int | None) -> list:
    """
    Active topics a page may produce: the whole base curriculum plus variants that
    are shared or were grown from this page's own winners. A variant born from
    another page's audience is not forced onto this one.
    """
    query = db.query(ContentTopic).filter(ContentTopic.is_active.isnot(False))
    if page_id is not None:
        query = query.filter(or_(ContentTopic.origin_page_id.is_(None),
                                 ContentTopic.origin_page_id == page_id))
    return query.all()


def learning_phase(db: Session, page_id: int) -> dict:
    """
    Where a page stands in its curriculum. 'test' while some active base topic has
    never been produced for the page; 'focus' once every one has been. `measured`
    counts base topics with at least one published post old enough to judge.
    """
    bases = (db.query(ContentTopic)
               .filter(ContentTopic.source == "seed", ContentTopic.is_active.isnot(False))
               .all())
    produced = {tid for (tid,) in db.query(Post.topic_id).filter(Post.page_id == page_id).distinct()}
    measured = {
        tid for (tid,) in db.query(Post.topic_id)
        .join(PostMetric, PostMetric.post_id == Post.id)
        .filter(Post.page_id == page_id, Post.status == "published",
                Post.published_at.isnot(None), Post.published_at <= _mature_cutoff())
        .distinct()
    }
    untested = [t for t in bases if t.id not in produced]
    return {
        "phase": "test" if untested else "focus",
        "total": len(bases),
        "tested": len(bases) - len(untested),
        "measured": sum(1 for t in bases if t.id in measured),
        "untested": untested,
    }


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
    row = (
        db.query(PageTopicWeight)
        .filter(PageTopicWeight.page_id == page_id, PageTopicWeight.topic_id == topic.id)
        .first()
    )
    if not row:
        row = PageTopicWeight(page_id=page_id, topic_id=topic.id, weight=baseline_weight_for(topic))
        db.add(row)
        db.flush()
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
            m_data = fetch_post_metrics(page_id, access_token, post.fb_post_id)
            score = calculate_metric_score(
                m_data["reactions"],
                m_data["comments"],
                m_data["shares"],
                m_data["reach"]
            )

            metric = db.query(PostMetric).filter(PostMetric.post_id == post.id).first()
            if not metric:
                metric = PostMetric(
                    post_id=post.id,
                    fb_post_id=post.fb_post_id,
                )
                db.add(metric)

            metric.reactions = m_data["reactions"]
            metric.comments = m_data["comments"]
            metric.shares = m_data["shares"]
            metric.reach = m_data["reach"]
            metric.impressions = m_data["impressions"]
            metric.calculated_score = score
            metric.last_checked_at = datetime.now(timezone.utc)

            updated.append({
                "post_id": post.id,
                "title": post.visual_title,
                "score": score,
                "reach": m_data["reach"]
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
    Evaluates each topic's average REACH per post and adjusts the selection
    weights. Only posts at least MATURE_AFTER_HOURS old count, since a newer
    post's reach is still climbing.

    Recent performance dominates: a topic that won last week outranks one that
    won months ago, so the autopilot follows what the audience wants *now*.

    With `page_id` the calculation uses only that Fanspage's posts and writes to
    that page's own weight table — every page learns its own audience. Without it,
    the global catalog weights are refreshed from all pages combined.
    """
    topics = topics_for_page(db, page_id)
    if not topics:
        return {"message": "Tidak ada topik di database."}

    recent = {e["topic_id"]: e for e in
              window_performance(db, window_days, page_id, mature_only=True)["ranking"]}
    mature_cutoff = _mature_cutoff()

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
                total_sc += p.metrics[0].calculated_score
            if p.metrics and p.published_at and _as_naive_utc(p.published_at) <= mature_cutoff:
                total_r += p.metrics[0].reach
                measured_posts += 1

        avg_reach = float(total_r / measured_posts) if measured_posts else 0.0
        target = get_page_weight(db, page_id, topic) if page_id is not None else topic
        target.posts_count = len(posts)
        target.total_score = total_sc
        target.avg_reach = avg_reach

        recent_entry = recent.get(topic.id)
        lifetime_avg = avg_reach
        recent_avg = recent_entry["avg_reach"] if (recent_entry and recent_entry["measured_posts"]) else 0.0

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

    # Pass 2: weights are RELATIVE to the page's average measured topic, so the
    # scale works whether a page reaches 100 or 100,000 people per post, and "1.0"
    # means "an average topic for this audience". Every measured topic counts, not
    # only this week's: the test phase spreads the curriculum over weeks, and a
    # winner tested early must not lose to an average topic tested late. Topics
    # in focus are re-posted often, so their recent numbers keep them honest.
    measured_scores = [s["blended"] for s in stats if s["blended"] is not None]
    reference = sum(measured_scores) / len(measured_scores) if measured_scores else 0.0

    summary = []
    for s in stats:
        topic, target, blended = s["topic"], s["target"], s["blended"]

        is_base = (topic.source or "seed") == "seed"
        floor = BASE_TOPIC_WEIGHT_FLOOR if is_base else VARIANT_WEIGHT_FLOOR

        if blended is not None and reference > 0:
            weight = max(floor, min(3.0, blended / reference))
        else:
            # Never measured on this page -> the seeded editorial baseline.
            weight = baseline_weights.get(topic.topic_key, 1.0 if is_base else NEW_VARIANT_BASELINE)
            if measured_scores:
                # Once real results exist, an untried topic must not outrank an
                # average one; it gets its turn through the test phase or exploration.
                weight = max(floor, min(weight, STALE_TOPIC_CAP))
        target.weight = round(weight, 2)
        s["weight"] = target.weight

    # Pass 3: a variant grown from a winner and not yet measured here is "more of
    # what works", so it starts at its parent's weight on this page instead of
    # being parked below every topic that already performed.
    by_topic = {s["topic"].id: s for s in stats}
    for s in stats:
        topic = s["topic"]
        parent = by_topic.get(topic.parent_topic_id)
        if (topic.source == "ai" and s["blended"] is None and parent
                and parent["blended"] is not None):
            s["target"].weight = round(max(s["target"].weight, parent["weight"]), 2)

    for s in stats:
        topic, target, recent_entry = s["topic"], s["target"], s["recent"]
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


def _family_key(topic: ContentTopic) -> int:
    """A base topic and every variant grown from it form one family (one subject)."""
    return topic.base_topic_id or topic.id


def _produced_topic_ids(db: Session, page_id: int) -> set:
    return {tid for (tid,) in db.query(Post.topic_id).filter(Post.page_id == page_id).distinct()}


def focus_winners(db: Session, page_id: int, topics: list | None = None,
                  weights: dict | None = None) -> list:
    """
    The page's top FOCUS_ROTATION_SIZE winner families, best first. A family is
    ranked by its best member's measured average reach on this page, and its
    leader is that member. Raw reach, not the selection weight: weights are capped,
    so two strong winners could tie and come out in the wrong order. `fresh_variants` are the family's AI variants
    this page has never produced, oldest first: they are used before the leader
    itself is repeated.
    """
    topics = topics if topics is not None else topics_for_page(db, page_id)
    weights = weights if weights is not None else page_topic_weights(db, page_id, topics)
    produced = _produced_topic_ids(db, page_id)
    reach = {r.topic_id: (r.avg_reach or 0.0) for r in
             db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page_id).all()}

    def strength(t):
        return (reach.get(t.id, 0.0), weights.get(t.id, 0), t.id in produced)

    families = {}
    for topic in topics:
        families.setdefault(_family_key(topic), []).append(topic)

    ranked = []
    for key, members in families.items():
        leader = max(members, key=strength)
        fresh = sorted(
            (t for t in members if t.source == "ai" and t.id not in produced),
            key=lambda t: (_as_naive_utc(t.created_at), t.id),
        )
        ranked.append((strength(leader), {
            "key": key,
            "leader": leader,
            "avg_reach": reach.get(leader.id, 0.0),
            "weight": weights.get(leader.id, 0),
            "fresh_variants": fresh,
        }))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [family for _, family in ranked[:FOCUS_ROTATION_SIZE]]


def _rotate_focus(db: Session, page_id: int, winners: list) -> dict:
    """
    Next winner family in the rotation #1 -> #2 -> #3 -> #1. When the #1 family
    changes (a new winner emerged), the rotation restarts at the new #1. The
    position is stored on the page and committed with the post that uses it.
    """
    page = db.query(FacebookPage).filter(FacebookPage.id == page_id).first()
    leader_key = winners[0]["key"]
    if page.focus_leader_key != leader_key:
        page.focus_leader_key = leader_key
        page.focus_cursor = 0
    slot = (page.focus_cursor or 0) % len(winners)
    page.focus_cursor = slot + 1
    return {"slot": slot, **winners[slot]}


def get_next_recommended_topic(db: Session, page_id: int | None = None) -> ContentTopic:
    """
    Picks the next topic for a page in two phases.

    TEST: while the page has base topics it has never produced, it always takes
    one of them at random, so every base topic is tried exactly once on every page
    before anything repeats.

    FOCUS: EXPLORE_RATE of picks go to the least-used topics (to notice a change in
    taste). The rest rotate over the page's top reach winners, #1 -> #2 -> #3 ->
    #1, each time using a fresh AI variant of that winner (about 90% the same
    subject) and repeating the winner itself only when its variants run out.

    When `page_id` is given everything uses that page's own weights and history,
    so two Fanspages do not converge on identical content.
    """
    topics = topics_for_page(db, page_id)
    if not topics:
        raise ValueError("Database topik kosong.")

    if page_id is not None:
        untested = learning_phase(db, page_id)["untested"]
        if untested:
            selected = random.choice(untested)
            logger.info(f"[Feedback Loop] TEST mode ({len(untested)} base topic(s) left) "
                        f"selected topic: {selected.title}")
            return selected

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

    # Exploration: pick from topics this page used least. Ties are broken randomly
    # across ALL equally-unused topics, otherwise the lowest id would always win.
    if random.random() < EXPLORE_RATE:
        least_used_topics = sorted(topics, key=lambda t: usage[t.id])
        fewest = usage[least_used_topics[0].id][0]
        tied = [t for t in least_used_topics if usage[t.id][0] == fewest]
        pool = tied if len(tied) >= 3 else least_used_topics[:3]
        selected = random.choice(pool)
        logger.info(f"[Feedback Loop] EXPLORE mode selected topic: {selected.title}")
        return selected

    if page_id is not None:
        winner = _rotate_focus(db, page_id, focus_winners(db, page_id, topics, weights_by_topic))
        selected = winner["fresh_variants"][0] if winner["fresh_variants"] else winner["leader"]
        logger.info(
            f"[Feedback Loop] FOCUS mode winner #{winner['slot'] + 1} "
            f"'{winner['leader'].title}' -> {selected.title}"
        )
        return selected

    # No page (global catalog): weighted lottery, squared so winners dominate.
    weights = [max(0.1, weights_by_topic[t.id]) ** 2 for t in topics]
    selected = random.choices(topics, weights=weights, k=1)[0]
    logger.info(f"[Feedback Loop] Weighted pick: {selected.title} (weight: {weights_by_topic[selected.id]})")
    return selected


def topic_reach_summary(db: Session, page_id: int, topic: ContentTopic) -> dict:
    """A topic's lifetime results on one page (mature posts only), shaped like a window_performance entry."""
    posts = (db.query(Post)
               .filter(Post.page_id == page_id, Post.topic_id == topic.id, Post.status == "published",
                       Post.published_at.isnot(None), Post.published_at <= _mature_cutoff())
               .all())
    metrics = [p.metrics[0] for p in posts if p.metrics]
    reach = sum(m.reach for m in metrics)
    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "category": topic.category,
        "core_concept": topic.core_concept,
        "posts": len(posts),
        "measured_posts": len(metrics),
        "reach": reach,
        "avg_reach": round(reach / len(metrics), 1) if metrics else 0.0,
        "reactions": sum(m.reactions for m in metrics),
        "comments": sum(m.comments for m in metrics),
        "shares": sum(m.shares for m in metrics),
    }
