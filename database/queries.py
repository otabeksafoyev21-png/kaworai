import re
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AdBanner, Admin, Anime, AnimeRating, AnimeSubscription, Series, SubscriptionChannel, User

_SEASON_SUFFIX_RE = re.compile(r"\s+\d+\s*-?\s*fasl\s*$", re.IGNORECASE)


def strip_season_suffix(title: str) -> str:
    """'Naruto 2-fasl' → 'Naruto'. Fasl qo'shimchasi bo'lmasa — o'zi."""
    return _SEASON_SUFFIX_RE.sub("", title or "").strip()


# ═══════════════════════════════════════════════════════════
#  USER
# ═══════════════════════════════════════════════════════════


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    full_name: str,
    username: str | None = None,
) -> tuple:
    user = await session.get(User, telegram_id)
    if user:
        if username and user.username != username:
            user.username = username
            await session.commit()
        return user, False
    user = User(telegram_id=telegram_id, full_name=full_name, username=username)
    session.add(user)
    await session.commit()
    return user, True


async def get_user_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(User.telegram_id)))
    return result.scalar()


async def get_user_ux_mode(session: AsyncSession, user_id: int) -> str:
    """Pro foydalanuvchi tanlagan UX rejimi. Default: 'edit' (silliq).

    "edit" — xabar tahrirlanadi (bitta xabar saqlanadi).
    "send" — har bosishda yangi video xabar yuboriladi (eski o'chadi).
    Oddiy user yoki yo'q bo'lsa — har doim 'edit'.
    """
    res = await session.execute(select(User.ux_mode).where(User.telegram_id == user_id))
    mode = res.scalar_one_or_none()
    return mode if mode in ("edit", "send") else "edit"


async def set_user_ux_mode(session: AsyncSession, user_id: int, mode: str) -> bool:
    """UX rejimini o'zgartiradi. `mode` ∈ {'edit','send'}. True — saqlandi."""
    if mode not in ("edit", "send"):
        return False
    user = await session.get(User, user_id)
    if not user:
        return False
    user.ux_mode = mode
    await session.commit()
    return True


# Pro userlar /start menyusiga qo'shishi mumkin bo'lgan shortcut'lar ro'yxati.
# Kalit = button callback_data (boshqa handlerda tutilgan). Kun sayin yangilanishi
# mumkin — shu yerga yangi Pro action qo'shilsa, avtomatik tanlanish uchun
# ko'rinadi.
ALLOWED_START_EXTRAS: set[str] = {
    "pro_recommend",
    "pro_mood",
    "pro_trending",
    "pro_top",
    "pro_rising",
    "pro_hidden",
    "pro_continue",
    "pro_taste",
}

# Bir vaqtda /start menyusiga nechta Pro shortcut qo'shish mumkinligi —
# menyuni juda uzun qilib yubormaslik uchun chegarasini qo'yamiz.
MAX_START_EXTRAS = 6


async def get_user_start_extras(session: AsyncSession, user_id: int) -> list[str]:
    """Pro user /start menyusiga qo'shgan shortcut kalitlarini qaytaradi.

    Tartib — userning qo'shgan tartibi. Noto'g'ri (eskirgan) kalitlar
    avtomatik filterlanadi. User topilmasa — bo'sh ro'yxat.
    """
    res = await session.execute(select(User.start_extras).where(User.telegram_id == user_id))
    raw = res.scalar_one_or_none()
    if not raw or not isinstance(raw, list):
        return []
    return [k for k in raw if isinstance(k, str) and k in ALLOWED_START_EXTRAS]


async def set_user_start_extras(session: AsyncSession, user_id: int, keys: list[str]) -> bool:
    """`keys` — Pro shortcut kalitlar ro'yxati (tartibi muhim).

    Dubliklar olib tashlanadi, noto'g'ri kalitlar filterlanadi,
    `MAX_START_EXTRAS` dan ortig'i qirqiladi. User topilmasa False.
    """
    user = await session.get(User, user_id)
    if not user:
        return False
    clean: list[str] = []
    seen: set[str] = set()
    for k in keys or []:
        if not isinstance(k, str):
            continue
        if k not in ALLOWED_START_EXTRAS or k in seen:
            continue
        clean.append(k)
        seen.add(k)
        if len(clean) >= MAX_START_EXTRAS:
            break
    user.start_extras = clean
    await session.commit()
    return True


async def toggle_user_start_extra(session: AsyncSession, user_id: int, key: str) -> tuple[bool, list[str]]:
    """Shortcut kalitini qo'shadi/olib tashlaydi (toggle).

    Return: (ok, yangi ro'yxat). `ok=False` — user topilmadi yoki kalit noto'g'ri
    yoki limitga yetib qolgan edi.
    """
    if key not in ALLOWED_START_EXTRAS:
        return False, []
    current = await get_user_start_extras(session, user_id)
    if key in current:
        current.remove(key)
    else:
        if len(current) >= MAX_START_EXTRAS:
            return False, current
        current.append(key)
    ok = await set_user_start_extras(session, user_id, current)
    return ok, current


# ═══════════════════════════════════════════════════════════
#  CHANNELS
# ═══════════════════════════════════════════════════════════


async def get_active_channels(session: AsyncSession) -> list:
    """
    GLOBAL majburiy kanallar (bosh admin qo'shgan). Middleware shu ro'yxatni
    ishlatadi — barcha foydalanuvchilarga qo'llanadi.

    Hamkor kanallari (`owner_id != NULL`) bu yerga kirmaydi — ular faqat
    o'zlari qo'shgan kontent ochilganida tekshiriladi.
    """
    result = await session.execute(
        select(SubscriptionChannel).where(
            SubscriptionChannel.is_active == True,
            SubscriptionChannel.owner_id.is_(None),
        )
    )
    return result.scalars().all()


async def get_partner_channels(session: AsyncSession, owner_id: int) -> list:
    """Hamkorning o'z majburiy kanallari (faqat shu owner uchun)."""
    result = await session.execute(
        select(SubscriptionChannel).where(
            SubscriptionChannel.is_active == True,
            SubscriptionChannel.owner_id == owner_id,
        )
    )
    return result.scalars().all()


async def get_all_channels(session: AsyncSession) -> list:
    result = await session.execute(select(SubscriptionChannel))
    return result.scalars().all()


async def get_news_channels(session: AsyncSession) -> list:
    result = await session.execute(
        select(SubscriptionChannel).where(SubscriptionChannel.is_news == True, SubscriptionChannel.is_active == True)
    )
    return result.scalars().all()


async def add_channel(
    session: AsyncSession,
    channel_name: str,
    channel_url: str,
    require_check: bool = False,
    is_news: bool = False,
    channel_id: int | None = None,
    username: str | None = None,
    owner_id: int | None = None,
) -> tuple[SubscriptionChannel, str]:
    """
    Kanal qo'shadi yoki allaqachon mavjud bo'lsa bayroqlarni birlashtiradi.

    Qoidalar:
      - Bir kanalni bir vaqtda "Majburiy" VA "News" qilib qo'yish mumkin —
        bitta qatorda ikkala bayroq ham True bo'ladi.
      - Agar kanal allaqachon shu kategoriyada bo'lsa — takroriy qo'shishga
        yo'l qo'yilmaydi.

    Qaytaradi: (channel, status) — status quyidagilardan biri:
      - "created"           — yangi qator qo'shildi.
      - "merged"            — mavjud qator yangi kategoriyani oldi.
      - "duplicate_mandatory" — allaqachon majburiy ro'yxatda.
      - "duplicate_news"    — allaqachon news ro'yxatda.
    """
    existing: SubscriptionChannel | None = None

    # Avval `channel_id` orqali qidiramiz (u unique). Topilmasa — `channel_url`.
    if channel_id is not None:
        existing = (
            await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.channel_id == channel_id))
        ).scalar_one_or_none()

    if existing is None and channel_url:
        existing = (
            await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.channel_url == channel_url))
        ).scalar_one_or_none()

    if existing is not None:
        # Takror ogohlantirishlari — faqat shu kategoriyada takrorlash taqiqlanadi.
        if require_check and existing.require_check:
            return existing, "duplicate_mandatory"
        if is_news and existing.is_news:
            return existing, "duplicate_news"

        # Birlashtirish — mavjud qatorga yangi bayroqni qo'shamiz, eskini o'chirmaymiz.
        if require_check:
            existing.require_check = True
        if is_news:
            existing.is_news = True
        # channel_id avval None bo'lib, endi kelgan bo'lsa — yangilaymiz.
        if channel_id is not None and existing.channel_id is None:
            existing.channel_id = channel_id
        if username and not existing.username:
            existing.username = username
        existing.is_active = True
        await session.commit()
        await session.refresh(existing)
        return existing, "merged"

    ch = SubscriptionChannel(
        channel_id=channel_id,
        username=username,
        channel_url=channel_url,
        channel_name=channel_name,
        is_active=True,
        require_check=require_check,
        is_news=is_news,
        owner_id=owner_id,
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    return ch, "created"


async def remove_channel(session: AsyncSession, ch_id: int) -> bool:
    result = await session.execute(delete(SubscriptionChannel).where(SubscriptionChannel.id == ch_id))
    await session.commit()
    return result.rowcount > 0


async def toggle_channel(session: AsyncSession, ch_id: int) -> bool | None:
    result = await session.execute(select(SubscriptionChannel).where(SubscriptionChannel.id == ch_id))
    ch = result.scalar_one_or_none()
    if not ch:
        return None
    ch.is_active = not ch.is_active
    await session.commit()
    return ch.is_active


# ═══════════════════════════════════════════════════════════
#  ANIME
# ═══════════════════════════════════════════════════════════


async def get_anime_by_id(session: AsyncSession, anime_id: int) -> Anime | None:
    return await session.get(Anime, anime_id)


async def get_all_animes(session: AsyncSession) -> list:
    result = await session.execute(select(Anime).order_by(Anime.id.desc()))
    return result.scalars().all()


async def find_next_season_anime(session: AsyncSession, anime_id: int) -> Anime | None:
    """Shu anime uchun keyingi fasl (bir xil asosiy nom + season+1).

    Topilmasa — None. 1-faslni ko'rib tugatgan user avtomatik 2-faslga
    o'tishni taklif etish uchun ishlatiladi.
    """
    anime = await session.get(Anime, anime_id)
    if not anime:
        return None
    base_title = strip_season_suffix(anime.title or "").lower()
    if not base_title:
        return None
    current_season = int(getattr(anime, "season", 1) or 1)
    next_season = current_season + 1
    # Seasonga mos kelgan kontentlarni olamiz, keyin title bo'yicha aniq
    # solishtiramiz (fasl qo'shimchasisiz). Bu case-insensitive.
    result = await session.execute(select(Anime).where(Anime.season == next_season))
    for candidate in result.scalars().all():
        if candidate.id == anime.id:
            continue
        if strip_season_suffix(candidate.title or "").lower() == base_title:
            return candidate
    return None


async def get_animes_by_owner(session: AsyncSession, owner_id: int | None) -> list:
    """Egalik bo'yicha kontentlar: owner_id=None => bosh admin kontenti (global)."""
    stmt = select(Anime).order_by(Anime.id.desc())
    if owner_id is None:
        stmt = stmt.where(Anime.owner_id.is_(None))
    else:
        stmt = stmt.where(Anime.owner_id == owner_id)
    return (await session.execute(stmt)).scalars().all()


# ═══════════════════════════════════════════════════════════
#  PARTNERS (hamkorlar) — admins.role = 'partner'
# ═══════════════════════════════════════════════════════════


async def get_all_partners(session: AsyncSession) -> list:
    """role='partner' bo'lgan hamma adminlarni qaytaradi."""
    result = await session.execute(select(Admin).where(Admin.role == "partner"))
    return result.scalars().all()


async def is_partner(session: AsyncSession, user_id: int) -> bool:
    row = (
        await session.execute(select(Admin).where(Admin.telegram_id == user_id, Admin.role == "partner"))
    ).scalar_one_or_none()
    return row is not None


async def add_partner(
    session: AsyncSession,
    telegram_id: int,
    nickname: str | None = None,
    added_by: int | None = None,
) -> tuple:
    """Hamkor qo'shadi. Agar admin jadvalida avvaldan bor bo'lsa — role=partner qilib yangilaydi."""
    existing = (await session.execute(select(Admin).where(Admin.telegram_id == telegram_id))).scalar_one_or_none()
    if existing:
        existing.role = "partner"
        if nickname and not existing.nickname:
            existing.nickname = nickname
        await session.commit()
        return existing, False
    row = Admin(telegram_id=telegram_id, nickname=nickname, role="partner")
    # added_by/added_at — migration orqali qo'shilgan ustunlar, mavjud bo'lsa qo'yamiz
    if added_by is not None:
        try:
            row.added_by = added_by
        except Exception:
            pass
    session.add(row)
    await session.commit()
    return row, True


async def remove_partner(session: AsyncSession, telegram_id: int) -> bool:
    """Hamkorni o'chiradi (admins.role='partner' satrini)."""
    result = await session.execute(delete(Admin).where(Admin.telegram_id == telegram_id, Admin.role == "partner"))
    await session.commit()
    return result.rowcount > 0


async def count_owner_animes(session: AsyncSession, owner_id: int) -> int:
    res = await session.execute(select(func.count(Anime.id)).where(Anime.owner_id == owner_id))
    return int(res.scalar() or 0)


# ═══════════════════════════════════════════════════════════
#  RATING
# ═══════════════════════════════════════════════════════════


async def get_user_rating(session: AsyncSession, anime_id: int, user_id: int) -> AnimeRating | None:
    result = await session.execute(
        select(AnimeRating).where(AnimeRating.anime_id == anime_id, AnimeRating.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def add_or_update_rating(session: AsyncSession, anime_id: int, user_id: int, score: int) -> float:
    existing = await get_user_rating(session, anime_id, user_id)
    if existing:
        existing.score = score
    else:
        session.add(AnimeRating(anime_id=anime_id, user_id=user_id, score=score))
    await session.commit()

    avg = (
        await session.execute(select(func.avg(AnimeRating.score)).where(AnimeRating.anime_id == anime_id))
    ).scalar() or 0.0

    count = (
        await session.execute(select(func.count(AnimeRating.id)).where(AnimeRating.anime_id == anime_id))
    ).scalar() or 0

    anime = await session.get(Anime, anime_id)
    if anime:
        anime.rating = round(float(avg), 1)
        anime.rating_count = count
        await session.commit()

    return round(float(avg), 1)


# ═══════════════════════════════════════════════════════════
#  PRO USER
# ═══════════════════════════════════════════════════════════


async def is_pro_user(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(User, user_id)
    if not user or not user.is_pro:
        return False
    if user.pro_until and user.pro_until < datetime.utcnow():
        user.is_pro = False
        user.pro_until = None
        await session.commit()
        return False
    return True


# ═══════════════════════════════════════════════════════════
#  ANIME OBUNA
# ═══════════════════════════════════════════════════════════


async def subscribe_anime(session: AsyncSession, anime_id: int, user_id: int) -> None:
    existing = await session.execute(
        select(AnimeSubscription).where(AnimeSubscription.anime_id == anime_id, AnimeSubscription.user_id == user_id)
    )
    if existing.scalar_one_or_none():
        return
    session.add(AnimeSubscription(anime_id=anime_id, user_id=user_id))
    await session.commit()


async def unsubscribe_anime(session: AsyncSession, anime_id: int, user_id: int) -> None:
    await session.execute(
        delete(AnimeSubscription).where(AnimeSubscription.anime_id == anime_id, AnimeSubscription.user_id == user_id)
    )
    await session.commit()


async def is_subscribed_anime(session: AsyncSession, anime_id: int, user_id: int) -> bool:
    result = await session.execute(
        select(AnimeSubscription).where(AnimeSubscription.anime_id == anime_id, AnimeSubscription.user_id == user_id)
    )
    return result.scalar_one_or_none() is not None


async def get_anime_subscribers(session: AsyncSession, anime_id: int) -> list[int]:
    result = await session.execute(select(AnimeSubscription.user_id).where(AnimeSubscription.anime_id == anime_id))
    return [r[0] for r in result.fetchall()]


# ═══════════════════════════════════════════════════════════
#  WATCH HISTORY — LIMIT YO'Q, TO'G'RI is_completed
# ═══════════════════════════════════════════════════════════


async def add_to_watch_history(
    session: AsyncSession,
    user_id: int,
    anime_id: int,
    episode: int = 1,
    is_completed: bool = False,
) -> None:
    """
    Watch historyga yozadi.
    MUHIM o'zgarishlar:
      - Limit YO'Q (eski 5 ta limit olib tashlandi)
      - is_completed: haqiqiy oxirgi qismga yetganda True
      - Taste profile ham yangilanadi
    """
    try:
        from database.models import UserWatchHistory

        result = await session.execute(
            select(UserWatchHistory).where(
                UserWatchHistory.user_id == user_id,
                UserWatchHistory.anime_id == anime_id,
            )
        )
        hw = result.scalar_one_or_none()

        if hw:
            # Faqat yuqori episode saqlanadi
            if episode > hw.last_episode:
                hw.last_episode = episode
            # is_completed faqat True ga o'tadi, False ga qaytmaydi
            if is_completed:
                hw.is_completed = True
            hw.watched_at = func.now()
        else:
            session.add(
                UserWatchHistory(
                    user_id=user_id,
                    anime_id=anime_id,
                    last_episode=episode,
                    is_completed=is_completed,
                )
            )

        await session.commit()

        # Taste profile yangilash
        anime = await session.get(Anime, anime_id)
        if anime:
            try:
                from utils.recommendation import update_taste_profile

                await update_taste_profile(session, user_id, anime)
            except Exception:
                pass

    except Exception:
        pass


async def record_view(
    session: AsyncSession,
    anime_id: int,
    user_id: int | None = None,
) -> None:
    """Ko'rishni yozadi va views counter oshiradi."""
    try:
        from database.models import ViewRecord

        session.add(ViewRecord(anime_id=anime_id, user_id=user_id))
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.views = (anime.views or 0) + 1
        await session.commit()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  ADMIN: ANIME TO'LIQ MA'LUMOT
# ═══════════════════════════════════════════════════════════


async def get_anime_full_info(session: AsyncSession, anime_id: int) -> dict | None:
    anime = await session.get(Anime, anime_id)
    if not anime:
        return None

    ep_count = (await session.execute(select(func.count(Series.id)).where(Series.anime_id == anime_id))).scalar() or 0

    sub_count = (
        await session.execute(
            select(func.count(AnimeSubscription.user_id)).where(AnimeSubscription.anime_id == anime_id)
        )
    ).scalar() or 0

    try:
        pro_sub_count = (
            await session.execute(
                select(func.count(AnimeSubscription.user_id))
                .join(User, AnimeSubscription.user_id == User.telegram_id)
                .where(AnimeSubscription.anime_id == anime_id, User.is_pro == True)
            )
        ).scalar() or 0
    except Exception:
        pro_sub_count = 0

    return {
        "id": anime.id,
        "owner_id": getattr(anime, "owner_id", None),
        "title": anime.title,
        "type": getattr(anime, "content_type", None) or "anime",
        "year": anime.year,
        "genres": anime.genres or [],
        "tags": getattr(anime, "tags", None) or [],
        "mood": getattr(anime, "mood", None) or [],
        "rating": anime.rating or 0.0,
        "rating_count": anime.rating_count or 0,
        "episodes_count": ep_count,
        "status": getattr(anime, "status", None) or "ongoing",
        "is_pro_locked": getattr(anime, "is_pro_locked", False),
        "is_hidden_gem": getattr(anime, "is_hidden_gem", False),
        "views": anime.views or 0,
        "subscribers": sub_count,
        "pro_subscribers": pro_sub_count,
        "added_by_id": getattr(anime, "added_by_id", None),
        "added_by_username": getattr(anime, "added_by_username", None),
        "added_at": getattr(anime, "added_at", None),
        "description": anime.description,
    }


# ═══════════════════════════════════════════════════════════
#  AD BANNERS — oddiy userlar uchun video ostida reklama
# ═══════════════════════════════════════════════════════════


async def get_random_active_ad(session: AsyncSession) -> AdBanner | None:
    """Faol reklamalardan tasodifiy bittasini qaytaradi."""
    result = await session.execute(select(AdBanner).where(AdBanner.is_active == True).order_by(func.random()).limit(1))
    return result.scalar_one_or_none()


async def get_all_ads(session: AsyncSession) -> list[AdBanner]:
    result = await session.execute(select(AdBanner).order_by(AdBanner.id.desc()))
    return list(result.scalars().all())


async def add_ad(session: AsyncSession, text: str, url: str | None = None) -> AdBanner:
    ad = AdBanner(text=text, url=url)
    session.add(ad)
    await session.commit()
    await session.refresh(ad)
    return ad


async def remove_ad(session: AsyncSession, ad_id: int) -> bool:
    result = await session.execute(delete(AdBanner).where(AdBanner.id == ad_id))
    await session.commit()
    return result.rowcount > 0


async def toggle_ad(session: AsyncSession, ad_id: int) -> bool | None:
    ad = await session.get(AdBanner, ad_id)
    if not ad:
        return None
    ad.is_active = not ad.is_active
    await session.commit()
    return ad.is_active


# ═══════════════════════════════════════════════════════════
#  REGION STATISTIKASI
# ═══════════════════════════════════════════════════════════


async def get_user_count_by_region(session: AsyncSession) -> list[tuple[str | None, int]]:
    """Har bir viloyatdagi foydalanuvchilar sonini qaytaradi."""
    result = await session.execute(
        select(User.region, func.count(User.telegram_id))
        .group_by(User.region)
        .order_by(func.count(User.telegram_id).desc())
    )
    return list(result.all())


async def get_pro_user_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(User.telegram_id)).where(User.is_pro == True))
    return result.scalar() or 0


async def get_today_active_users(session: AsyncSession) -> int:
    """Bugun faol bo'lgan foydalanuvchilar soni."""

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(select(func.count(User.telegram_id)).where(User.last_active >= today_start))
    return result.scalar() or 0


async def search_users_by_id_or_name(
    session: AsyncSession,
    query: str,
    limit: int = 10,
) -> list[User]:
    """Telegram ID yoki ism bo'yicha qidirish."""
    if query.isdigit():
        result = await session.execute(select(User).where(User.telegram_id == int(query)).limit(limit))
    else:
        result = await session.execute(select(User).where(User.full_name.ilike(f"%{query}%")).limit(limit))
    return list(result.scalars().all())
