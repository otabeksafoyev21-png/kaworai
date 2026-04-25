from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database.engine import Base


class User(Base):
    __tablename__ = "users"
    telegram_id = Column(BigInteger, primary_key=True)
    username = Column(String(100), nullable=True)
    full_name = Column(String(200), nullable=True)
    joined_at = Column(DateTime, server_default=func.now())
    is_pro = Column(Boolean, default=False)
    pro_until = Column(DateTime, nullable=True)
    # Foydalanuvchi tanlagan viloyat kodi (masalan `tashkent_city`).
    # NULL = hali tanlanmagan. Birinchi /start'da so'raladi.
    region = Column(String(40), nullable=True)
    # Re-engagement (qayta-faollashtirish) tizimi:
    #   last_active      — userning oxirgi aktivligi (har xabar/callback'da yangilanadi).
    #   last_reminder_at — eslatma xabari oxirgi yuborilgan vaqt (NULL = hech qachon).
    #   reminder_stage   — qaysi bosqichdagi eslatma: 0=hech, 1=21-kun, 2=24-kun,
    #                     3=30-kun, 4=45-kun, 5=sukut (60+ kun).
    last_active = Column(DateTime, server_default=func.now(), index=True)
    last_reminder_at = Column(DateTime, nullable=True)
    reminder_stage = Column(Integer, default=0)
    # Pro foydalanuvchi tanlagan UX rejimi:
    #   "edit" (default) — xabar tahrirlanadi (silliq, bitta xabar qoladi)
    #   "send" — har bosishda yangi video xabar yuboriladi (eski xabar o'chadi)
    # Oddiy userlar uchun har doim "edit" ishlatiladi (bu sozlama Pro uchun).
    ux_mode = Column(String(10), default="edit")
    # Pro foydalanuvchi tanlagan `/start` menyusidagi qo'shimcha tugmalar —
    # JSON ro'yxat (shortcut kalitlar: "pro_recommend", "pro_mood", "pro_trending"
    # va h.k.). Qo'shilgan tartibi = menyudagi tartibi (eng tepada). Default
    # menyu tugmalari doim quyi qismida qoladi. Oddiy userlar uchun e'tiborsiz.
    start_extras = Column(JSON, default=list)

    watch_history = relationship("UserWatchHistory", back_populates="user", cascade="all, delete-orphan")
    taste_profile = relationship("UserTasteProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    subscriptions = relationship("AnimeSubscription", back_populates="user", cascade="all, delete-orphan")


class Anime(Base):
    __tablename__ = "animes"
    id = Column(Integer, primary_key=True, autoincrement=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    poster_file_id = Column(String(300), nullable=True)
    trailer_file_id = Column(String(300), nullable=True)
    inline_thumbnail_url = Column(String(500), nullable=True)
    genres = Column(JSON, nullable=True)
    year = Column(Integer, nullable=True)
    rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    total_episodes = Column(Integer, default=0)
    views = Column(Integer, default=0)
    content_type = Column(String(20), default="anime")
    tags = Column(JSON, default=list)
    mood = Column(JSON, default=list)
    episodes_count = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)
    status = Column(String(20), default="ongoing")
    popularity = Column(Float, default=0.0)
    popularity_score = Column(Float, default=0.0)
    is_hidden_gem = Column(Boolean, default=False)
    is_pro_locked = Column(Boolean, default=False)
    # Kontent egasi (owner) — bosh admin uchun NULL, hamkor qo'shgan
    # kontentda uning telegram_id si. Hamkor faqat o'z kontentini ko'radi,
    # tahrirlaydi, o'chiradi. Bosh admin hammasini ko'radi.
    owner_id = Column(BigInteger, nullable=True, index=True)
    # Psixologik daraja (0..10). `Psixologik` janri tanlangan kontent uchun
    # admin qo'lda kiritadi — Pro AI tavsiyalari shu daraja bilan aniqroq
    # moslaydi. NULL = hali kiritilmagan (eski ma'lumotlar yoki boshqa janr).
    psychological_level = Column(Integer, nullable=True)
    # Fasl raqami. 1 = birinchi fasl (default). Bir xil nomdagi kontentni
    # qayta qo'shishda admin 2,3,... fasl deb belgilaydi; shu raqam title'ga
    # avto-qo'shilib saqlanadi (masalan "Naruto 2-fasl").
    season = Column(Integer, default=1)
    # Migration orqali qo'shiladi (migration_v2.py)
    # added_by_id, added_by_username, added_at

    episodes = relationship("Series", back_populates="anime", cascade="all, delete-orphan")
    ratings = relationship("AnimeRating", back_populates="anime", cascade="all, delete-orphan")
    related_to = relationship(
        "RelatedContent", foreign_keys="RelatedContent.anime_id", back_populates="anime", cascade="all, delete-orphan"
    )
    watch_records = relationship("UserWatchHistory", back_populates="anime", cascade="all, delete-orphan")
    view_records = relationship("ViewRecord", back_populates="anime", cascade="all, delete-orphan")
    subscriptions = relationship("AnimeSubscription", back_populates="anime", cascade="all, delete-orphan")


class Series(Base):
    __tablename__ = "series"
    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"))
    episode = Column(Integer, nullable=False)
    file_id = Column(String(300), nullable=False)
    anime = relationship("Anime", back_populates="episodes")


class AnimeRating(Base):
    __tablename__ = "anime_ratings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, nullable=False)
    score = Column(Integer, nullable=False)
    anime = relationship("Anime", back_populates="ratings")


class Admin(Base):
    __tablename__ = "admins"
    telegram_id = Column(BigInteger, primary_key=True)
    nickname = Column(String(100), nullable=True)
    role = Column(String(20), default="admin")
    # `permissions` — qaysi bo'limlardan foydalana oladi: JSON list (ruxsat
    # kalitlari). Masalan: ["add_anime", "add_episode"]. Agar NULL bo'lsa
    # (eski adminlar uchun) — backward-compat uchun "hammasi" deb qaraymiz.
    # Ruxsat tekshiruvi: handlers/admin.py dagi `has_permission` helper.
    permissions = Column(JSON, nullable=True)
    # added_by va added_at migration_v2.py ishlatilgandan keyin qo'shiladi


class SubscriptionChannel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(BigInteger, unique=True, nullable=True)
    username = Column(String(100), nullable=True)
    channel_url = Column(String(256), nullable=False)
    channel_name = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True)
    require_check = Column(Boolean, default=False)
    is_news = Column(Boolean, default=False)
    # Majburiy kanallar uchun region cheklovi (region kod yoki NULL).
    # NULL = hamma uchun; aks holda faqat shu viloyat foydalanuvchilarga
    # tekshiruv qo'llaniladi. News va optional kanallarga ta'sir qilmaydi.
    region = Column(String(40), nullable=True)
    # Kanal egasi. NULL = global (bosh admin qo'shgan, hamma userga
    # qo'llaniladi). Hamkor telegram_id'si bo'lsa — bu kanal faqat shu
    # hamkor qo'shgan kontent ochilganida tekshiriladi.
    owner_id = Column(BigInteger, nullable=True, index=True)


class AnimeSubscription(Base):
    """User animega obuna — yangi qism chiqsa xabar beradi."""

    __tablename__ = "anime_subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    anime = relationship("Anime", back_populates="subscriptions")
    user = relationship("User", back_populates="subscriptions")

    __table_args__ = (UniqueConstraint("anime_id", "user_id", name="uq_anime_user_sub"),)


class RelatedContent(Base):
    __tablename__ = "related_content"
    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"), nullable=False)
    related_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"), nullable=False)
    relation_type = Column(String(20), default="similar")

    anime = relationship("Anime", foreign_keys=[anime_id], back_populates="related_to")
    related_anime = relationship("Anime", foreign_keys=[related_id])


class UserWatchHistory(Base):
    __tablename__ = "user_watch_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"), nullable=False)
    watched_at = Column(DateTime, default=func.now())
    last_episode = Column(Integer, default=1)
    is_completed = Column(Boolean, default=False)

    user = relationship("User", back_populates="watch_history")
    anime = relationship("Anime", back_populates="watch_records")


class UserTasteProfile(Base):
    __tablename__ = "user_taste_profiles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, unique=True)
    fav_genres = Column(JSON, default=dict)
    fav_tags = Column(JSON, default=dict)
    fav_moods = Column(JSON, default=dict)
    fav_type = Column(String(20), nullable=True)
    avg_rating_pref = Column(Float, default=7.0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="taste_profile")


class ViewRecord(Base):
    __tablename__ = "view_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    anime_id = Column(Integer, ForeignKey("animes.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(BigInteger, nullable=True)
    viewed_at = Column(DateTime, default=func.now())

    anime = relationship("Anime", back_populates="view_records")


class AdBanner(Base):
    """Oddiy (non-Pro) foydalanuvchilarga video ostida ko'rsatiladigan reklama bannerlari."""

    __tablename__ = "ad_banners"
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)
    url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
