from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from database.db_session import Base

def utc_now():
    return datetime.now(timezone.utc)

class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True, index=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class FacebookPage(Base):
    """
    One managed Fanspage. Credentials, schedule and content preferences live per
    page, so every page produces its own content on its own rhythm.
    """
    __tablename__ = "facebook_pages"

    id = Column(Integer, primary_key=True, index=True)
    page_id = Column(String(100), unique=True, nullable=False, index=True)  # Facebook's numeric id
    name = Column(String(200), nullable=False, default="")
    access_token = Column(Text, nullable=False, default="")
    picture_url = Column(String(500), nullable=True)
    link = Column(String(500), nullable=True)
    fan_count = Column(Integer, default=0)

    # Per-page content preferences
    content_language = Column(String(10), default="id")
    aspect_ratio = Column(String(10), default="3:4")
    auto_post_times = Column(String(200), default="10:00,19:00")
    autopilot_enabled = Column(Boolean, default=False)

    # Comment auto-reply: 'auto' posts replies directly, 'review' only drafts them
    auto_reply_enabled = Column(Boolean, default=False)
    auto_reply_mode = Column(String(20), default="auto")
    reply_max_per_hour = Column(Integer, default=20)
    reply_last_run = Column(DateTime, nullable=True)
    reply_last_error = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True, index=True)  # false = paused entirely
    token_status = Column(String(50), default="Belum diverifikasi")
    last_verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    posts = relationship("Post", back_populates="page")
    topic_weights = relationship("PageTopicWeight", back_populates="page", cascade="all, delete-orphan")
    comment_replies = relationship("CommentReply", back_populates="page", cascade="all, delete-orphan")


class PageTopicWeight(Base):
    """
    Per-page learning state. Each Fanspage has its own audience, so each one
    learns its own topic weights instead of sharing a single global ranking.
    """
    __tablename__ = "page_topic_weights"

    id = Column(Integer, primary_key=True, index=True)
    page_id = Column(Integer, ForeignKey("facebook_pages.id", ondelete="CASCADE"), index=True, nullable=False)
    topic_id = Column(Integer, ForeignKey("content_topics.id", ondelete="CASCADE"), index=True, nullable=False)

    weight = Column(Float, default=1.0)
    posts_count = Column(Integer, default=0)
    total_score = Column(Float, default=0.0)
    avg_reach = Column(Float, default=0.0)
    last_used_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    page = relationship("FacebookPage", back_populates="topic_weights")
    topic = relationship("ContentTopic")

    __table_args__ = (UniqueConstraint("page_id", "topic_id", name="uq_page_topic"),)


class ContentTopic(Base):
    __tablename__ = "content_topics"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(50), index=True)  # Fluvial, Geology, Minerals, Strategy
    topic_key = Column(String(100), unique=True, index=True)
    title = Column(String(200), nullable=False)
    core_concept = Column(Text, nullable=False)
    visual_blueprint = Column(Text, nullable=False)

    # Adaptive weights for the learning loop
    weight = Column(Float, default=1.0)
    posts_count = Column(Integer, default=0)
    total_score = Column(Float, default=0.0)
    avg_reach = Column(Float, default=0.0)
    last_used_at = Column(DateTime, nullable=True)

    # Topic evolution: topics the AI derived from a recent winner.
    # 'seed' topics are the permanent base curriculum the system learns from;
    # 'ai' topics are variants that always trace back to one base topic.
    source = Column(String(20), default="seed", index=True)   # 'seed' | 'ai'
    parent_topic_id = Column(Integer, ForeignKey("content_topics.id"), nullable=True)
    base_topic_id = Column(Integer, ForeignKey("content_topics.id"), nullable=True, index=True)
    origin_note = Column(Text, nullable=True)      # why this topic was created
    created_at = Column(DateTime, default=utc_now)
    is_active = Column(Boolean, default=True, index=True)

    posts = relationship("Post", back_populates="topic")
    parent = relationship("ContentTopic", remote_side=[id], foreign_keys=[parent_topic_id], backref="children")
    base_topic = relationship("ContentTopic", remote_side=[id], foreign_keys=[base_topic_id], backref="variants")


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    page_id = Column(Integer, ForeignKey("facebook_pages.id"), nullable=True, index=True)
    topic_id = Column(Integer, ForeignKey("content_topics.id"), nullable=True)
    topic_title = Column(String(200), nullable=False)
    language = Column(String(10), default="id")
    visual_title = Column(String(250), nullable=False)
    prompt_used = Column(Text, nullable=False)
    image_filename = Column(String(255), nullable=False)
    image_path = Column(String(500), nullable=False)
    caption = Column(Text, nullable=False)
    
    status = Column(String(50), default="ready")  # 'draft', 'ready', 'published', 'failed'
    fb_post_id = Column(String(100), nullable=True, index=True)
    fb_post_url = Column(String(500), nullable=True)
    
    created_at = Column(DateTime, default=utc_now)
    published_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    topic = relationship("ContentTopic", back_populates="posts")
    page = relationship("FacebookPage", back_populates="posts")
    metrics = relationship("PostMetric", back_populates="post", cascade="all, delete-orphan")


class PostMetric(Base):
    __tablename__ = "post_metrics"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), index=True)
    fb_post_id = Column(String(100), nullable=False)
    
    reactions = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    reach = Column(Integer, default=0)
    impressions = Column(Integer, default=0)
    calculated_score = Column(Float, default=0.0)
    
    last_checked_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    post = relationship("Post", back_populates="metrics")


class CommentReply(Base):
    """
    One audience comment the app has handled. The unique comment id is what
    guarantees a comment is never answered twice, even by overlapping runs.
    """
    __tablename__ = "comment_replies"

    id = Column(Integer, primary_key=True, index=True)
    page_id = Column(Integer, ForeignKey("facebook_pages.id", ondelete="CASCADE"), index=True, nullable=False)

    fb_post_id = Column(String(100), nullable=False, index=True)
    post_message = Column(Text, nullable=True)
    post_url = Column(String(500), nullable=True)

    fb_comment_id = Column(String(100), nullable=False, unique=True, index=True)
    comment_message = Column(Text, nullable=False, default="")
    commenter_name = Column(String(200), nullable=True)
    commenter_id = Column(String(100), nullable=True)
    comment_created_at = Column(DateTime, nullable=True)

    reply_message = Column(Text, nullable=True)
    reply_fb_id = Column(String(100), nullable=True)
    # 'pending' (draft awaiting approval), 'replying' (being sent), 'replied',
    # 'skipped' (AI chose not to answer), 'failed', 'dismissed'
    status = Column(String(20), default="pending", index=True)
    note = Column(Text, nullable=True)    # why it was skipped, or the error

    created_at = Column(DateTime, default=utc_now)
    replied_at = Column(DateTime, nullable=True, index=True)

    page = relationship("FacebookPage", back_populates="comment_replies")


class User(Base):
    """A dashboard login. Only the PBKDF2 hash of the password is stored."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    password_hash = Column(String(300), nullable=False)
    created_at = Column(DateTime, default=utc_now)
    password_changed_at = Column(DateTime, default=utc_now)


class UserSession(Base):
    """
    A signed-in browser. The cookie carries a random token; only its SHA-256 is
    stored, so a leaked database cannot be replayed as a login.
    """
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now)
    expires_at = Column(DateTime, nullable=False)
