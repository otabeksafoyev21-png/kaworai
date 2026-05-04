"""
utils/recommendation.py — Kaworai Pro AI Engine v2
Tuzatilgan muammolar:
  1. Ko'rilgan anime HECH QACHON qayta tavsiya qilinmaydi (limit yo'q)
  2. 1 qismlik va tugagan seriyalar "davom ettirish"ga chiqmaydi
  3. 12 qismlik seriya 13-qismdan emas, to'g'ri ko'rsatiladi
  4. Diversity: bir janrdan ko'pi bilan 2 ta tavsiya
  5. Taste profile: barcha history hisobga olinadi
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Anime, RelatedContent, Series, UserTasteProfile, UserWatchHistory, ViewRecord

# ═══════════════════════════════════════════════════════════
#  MOOD XARITASI
# ═══════════════════════════════════════════════════════════

MOOD_MAP: dict[str, dict] = {
    "sad": {
        "tags": ["emotional", "tragedy", "loss", "grief", "tearjerker"],
        "genres": ["drama", "slice of life", "romance"],
    },
    "romantic": {
        "tags": ["romance", "love", "wholesome", "heartwarming"],
        "genres": ["romance", "slice of life", "shoujo"],
    },
    "dark": {
        "tags": ["dark", "psychological", "horror", "survival", "dystopia"],
        "genres": ["psychological", "horror", "thriller", "seinen"],
    },
    "motivational": {
        "tags": ["redemption", "growth", "sports", "underdog", "determination"],
        "genres": ["sports", "action", "shounen"],
    },
    "action": {"tags": ["battle", "fight", "war", "revenge", "power"], "genres": ["action", "adventure", "shounen"]},
    "funny": {"tags": ["comedy", "parody", "wholesome", "cute"], "genres": ["comedy", "slice of life"]},
    "mystery": {
        "tags": ["mystery", "detective", "plot twist", "conspiracy"],
        "genres": ["mystery", "thriller", "psychological"],
    },
    "chill": {"tags": ["wholesome", "iyashikei", "relaxing", "cute"], "genres": ["slice of life", "comedy"]},
    "fantasy": {
        "tags": ["magic", "isekai", "fantasy world", "adventure"],
        "genres": ["fantasy", "adventure", "isekai"],
    },
    "scary": {"tags": ["horror", "gore", "supernatural", "monsters"], "genres": ["horror", "psychological"]},
}

TEXT_TO_MOOD: dict[str, list[str]] = {
    "sad": ["sad", "xafa", "g'amgin", "yig'lay", "depressed", "cry"],
    "romantic": ["romantic", "sevgi", "love", "muhabbat", "romance"],
    "dark": ["dark", "qorong'u", "psychological", "og'ir", "heavy"],
    "motivational": ["motivational", "ilhomlantiruvchi", "sport", "motivate"],
    "action": ["action", "jangari", "fight", "battle", "war"],
    "funny": ["funny", "kulgi", "kulgili", "comedy", "lol", "humor"],
    "mystery": ["mystery", "sirli", "detective", "jumboq", "puzzle"],
    "chill": ["chill", "yengil", "relax", "easy", "tinch"],
    "fantasy": ["fantasy", "sehr", "magic", "isekai", "boshqa dunyo"],
    "scary": ["scary", "qo'rqinchli", "horror", "dahshat"],
}


def detect_mood_from_text(text: str) -> list[str]:
    text_lower = text.lower()
    detected = []
    for mood, keywords in TEXT_TO_MOOD.items():
        if any(kw in text_lower for kw in keywords):
            detected.append(mood)
    return detected or ["chill"]


def mood_to_filters(moods: list[str]) -> dict:
    tags, genres = [], []
    for mood in moods:
        if mood in MOOD_MAP:
            tags.extend(MOOD_MAP[mood]["tags"])
            genres.extend(MOOD_MAP[mood]["genres"])
    return {"tags": list(set(tags)), "genres": list(set(genres))}


# ═══════════════════════════════════════════════════════════
#  SCORING ENGINE
# ═══════════════════════════════════════════════════════════


def compute_score(
    anime: Anime,
    user_genres: dict[str, int],
    user_tags: dict[str, int],
    user_moods: dict[str, int],
    target_genres: list[str] | None = None,
    target_tags: list[str] | None = None,
    target_moods: list[str] | None = None,
) -> float:
    anime_genres = [g.lower() for g in (anime.genres or [])]
    anime_tags = [t.lower() for t in (anime.tags or [])]
    anime_moods = [m.lower() for m in (anime.mood or [])]

    # Genre match (0–3.5)
    total_g = max(sum(user_genres.values()), 1)
    g_score = sum(user_genres.get(g, 0) / total_g for g in anime_genres)
    if target_genres:
        g_score += len(set(anime_genres) & set(t.lower() for t in target_genres)) * 0.4
    g_score = min(g_score, 1.0) * 3.5

    # Tag match (0–2.5)
    total_t = max(sum(user_tags.values()), 1)
    t_score = sum(user_tags.get(t, 0) / total_t for t in anime_tags)
    if target_tags:
        t_score += len(set(anime_tags) & set(t.lower() for t in target_tags)) * 0.3
    t_score = min(t_score, 1.0) * 2.5

    # Mood match (0–2.0)
    total_m = max(sum(user_moods.values()), 1)
    m_score = sum(user_moods.get(m, 0) / total_m for m in anime_moods)
    if target_moods:
        m_score += len(set(anime_moods) & set(m.lower() for m in target_moods)) * 0.4
    m_score = min(m_score, 1.0) * 2.0

    # Rating (0–1.2)
    r_score = (anime.rating / 10.0) * 1.2

    # Popularity (0–0.5)
    p_score = min((anime.popularity_score or 0) / 100.0, 1.0) * 0.5

    # Year bonus (0–0.3)
    y_score = 0.3 if (anime.year and anime.year >= 2020) else 0.15 if (anime.year and anime.year >= 2015) else 0.0

    return round(g_score + t_score + m_score + r_score + p_score + y_score, 4)


# ═══════════════════════════════════════════════════════════
#  TASTE PROFILE
# ═══════════════════════════════════════════════════════════


async def get_or_create_taste_profile(session: AsyncSession, user_id: int) -> UserTasteProfile:
    result = await session.execute(select(UserTasteProfile).where(UserTasteProfile.user_id == user_id))
    profile = result.scalar_one_or_none()
    if not profile:
        profile = UserTasteProfile(user_id=user_id, fav_genres={}, fav_tags={}, fav_moods={})
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
    return profile


async def update_taste_profile(session: AsyncSession, user_id: int, anime: Anime) -> None:
    """
    User kontent ko'rganda taste profilini yangilaydi.
    BARCHA ko'rilgan history hisobga olinadi (limit yo'q).
    """
    profile = await get_or_create_taste_profile(session, user_id)

    g_counter = dict(profile.fav_genres or {})
    for g in anime.genres or []:
        k = g.lower()
        g_counter[k] = g_counter.get(k, 0) + 1

    t_counter = dict(profile.fav_tags or {})
    for t in anime.tags or []:
        k = t.lower()
        t_counter[k] = t_counter.get(k, 0) + 1

    m_counter = dict(profile.fav_moods or {})
    for m in anime.mood or []:
        k = m.lower()
        m_counter[k] = m_counter.get(k, 0) + 1

    # Sevimli content type — BARCHA history dan
    history = await session.execute(select(UserWatchHistory.anime_id).where(UserWatchHistory.user_id == user_id))
    type_counter: dict[str, int] = {}
    for (aid,) in history.fetchall():
        a = await session.get(Anime, aid)
        if a:
            ct = a.content_type or "anime"
            type_counter[ct] = type_counter.get(ct, 0) + 1
    fav_type = max(type_counter, key=type_counter.get) if type_counter else "anime"

    profile.fav_genres = g_counter
    profile.fav_tags = t_counter
    profile.fav_moods = m_counter
    profile.fav_type = fav_type
    await session.commit()


def build_identity_label(profile: UserTasteProfile) -> str:
    if not profile or not profile.fav_genres:
        return "🎌 Anime muxlisi"

    genres = dict(profile.fav_genres or {})
    tags = dict(profile.fav_tags or {})

    top_genre = max(genres, key=genres.get) if genres else None
    top_tag = max(tags, key=tags.get) if tags else None
    fav_type = profile.fav_type or "anime"

    type_labels = {"anime": "anime", "movie": "kino", "serial": "serial", "dorama": "dorama"}
    tag_labels = {
        "dark": "🌑 Qorong'u",
        "psychological": "🧠 Psixologik",
        "romance": "💕 Romantik",
        "action": "⚔️ Jangari",
        "comedy": "😂 Kulgili",
        "supernatural": "👻 G'ayritabiiy",
        "sports": "🏆 Sport",
        "horror": "😱 Qo'rqinchli",
        "wholesome": "🌸 Iliq",
        "survival": "🔥 Omon qolish",
        "emotional": "💔 Hissiy",
        "mystery": "🔍 Sirli",
    }

    t_label = tag_labels.get(top_tag, top_tag) if top_tag else None
    type_str = type_labels.get(fav_type, fav_type)

    if t_label and top_genre:
        return f"{t_label} {top_genre} {type_str} muxlisisan! 🔥"
    elif top_genre:
        return f"🎯 Sen {top_genre} {type_str} muxlisisan!"
    return f"🎌 {type_str.capitalize()} muxlisi"


# ═══════════════════════════════════════════════════════════
#  ASOSIY RECOMMENDATION — TO'LIQ TUZATILGAN
# ═══════════════════════════════════════════════════════════


async def _get_all_watched_ids(session: AsyncSession, user_id: int) -> set[int]:
    """
    Ko'rilgan BARCHA anime ID larini qaytaradi.
    Limit yo'q — shunda ko'rilganlar hech qachon qayta tavsiya qilinmaydi.
    """
    result = await session.execute(select(UserWatchHistory.anime_id).where(UserWatchHistory.user_id == user_id))
    return set(row[0] for row in result.fetchall())


async def _get_episode_count(session: AsyncSession, anime_id: int) -> int:
    """Animening haqiqiy DB dagi qismlar soni."""
    result = await session.execute(select(func.count(Series.id)).where(Series.anime_id == anime_id))
    return result.scalar() or 0


def _apply_diversity(scored: list[tuple], limit: int, max_per_genre: int = 2) -> list[tuple]:
    """
    Bir janrdan ko'pi bilan max_per_genre ta tavsiya.
    Diversity ta'minlaydi.
    """
    genre_count: dict[str, int] = {}
    result = []
    for anime, score in scored:
        if len(result) >= limit:
            break
        top_genre = (anime.genres or ["unknown"])[0].lower()
        if genre_count.get(top_genre, 0) < max_per_genre:
            result.append((anime, score))
            genre_count[top_genre] = genre_count.get(top_genre, 0) + 1
    # Agar diversity tufayli yetarli bo'lmasa — qolganlarni qo'shamiz
    if len(result) < limit:
        added_ids = {a.id for a, _ in result}
        for anime, score in scored:
            if len(result) >= limit:
                break
            if anime.id not in added_ids:
                result.append((anime, score))
    return result


async def get_recommendations(
    session: AsyncSession,
    user_id: int,
    content_type: str | None = None,
    mood_text: str | None = None,
    target_moods: list[str] | None = None,
    limit: int = 10,
    is_pro: bool = False,
) -> list[dict]:
    """
    Asosiy recommendation funksiyasi.
    Ko'rilganlar hech qachon qayta chiqmaydi.
    Diversity ta'minlangan.
    """
    profile = await get_or_create_taste_profile(session, user_id)
    user_genres = dict(profile.fav_genres or {})
    user_tags = dict(profile.fav_tags or {})
    user_moods = dict(profile.fav_moods or {})

    # Mood filtrlari
    t_genres, t_tags, t_moods = [], [], []
    if mood_text:
        detected = detect_mood_from_text(mood_text)
        f = mood_to_filters(detected)
        t_genres, t_tags, t_moods = f["genres"], f["tags"], detected
    elif target_moods:
        f = mood_to_filters(target_moods)
        t_genres, t_tags, t_moods = f["genres"], f["tags"], target_moods

    # ✅ TUZATISH 1: Ko'rilgan BARCHA animelar (limit yo'q)
    watched_ids = await _get_all_watched_ids(session, user_id)

    # Kontentlarni olish
    query = select(Anime)
    if content_type:
        query = query.where(Anime.content_type == content_type)
    if not is_pro:
        query = query.where(Anime.is_pro_locked == False)

    result = await session.execute(query)
    all_content = result.scalars().all()

    # Scoring — ko'rilganlarni skip
    scored = []
    for anime in all_content:
        if anime.id in watched_ids:  # ✅ Ko'rilgan → skip
            continue
        score = compute_score(anime, user_genres, user_tags, user_moods, t_genres, t_tags, t_moods)
        scored.append((anime, score))

    # Sort
    scored.sort(key=lambda x: x[1], reverse=True)

    # ✅ TUZATISH 4: Diversity — bir janrdan ko'pi bilan 2 ta
    top = _apply_diversity(scored, limit, max_per_genre=2)

    return [_anime_to_dict(a, score=s) for a, s in top]


# ═══════════════════════════════════════════════════════════
#  SMART CONTINUE — TO'LIQ TUZATILGAN
# ═══════════════════════════════════════════════════════════


async def get_smart_continue(
    session: AsyncSession,
    user_id: int,
) -> list[dict]:
    """
    Smart Continue — FAQAT haqiqatan davom ettirish mumkin bo'lgan kontentlar.

    Qoidalar:
    1. Ko'rib tugalmagan (is_completed=False)
    2. Haqiqiy qismlar soni > ko'rilgan qism (DB dan tekshiriladi)
    3. 1 qismlik va ko'rib bo'lingan kontentlar chiqmaydi
    4. resume_from = last_episode + 1 (lekin max episode dan oshmasin)
    """
    result = await session.execute(
        select(UserWatchHistory, Anime)
        .join(Anime, UserWatchHistory.anime_id == Anime.id)
        .where(
            UserWatchHistory.user_id == user_id,
            UserWatchHistory.is_completed == False,
            UserWatchHistory.last_episode >= 1,
        )
        .order_by(UserWatchHistory.watched_at.desc())
        .limit(20)  # Ko'proq olib, filtrlaymiz
    )

    items = []
    for hw, anime in result.fetchall():
        # ✅ TUZATISH 2 & 3: Haqiqiy qismlar sonini DB dan tekshirish
        real_ep_count = await _get_episode_count(session, anime.id)

        if real_ep_count == 0:
            continue  # Qismlar hali qo'shilmagan

        if real_ep_count == 1:
            continue  # 1 qismlik — ko'rib bo'lingan

        # ✅ Ko'rilgan qism >= haqiqiy qismlar soni → tugagan, ko'rsatma
        if hw.last_episode >= real_ep_count:
            continue

        # resume_from = keyingi qism
        resume_from = hw.last_episode + 1

        # Keyingi qism haqiqatan mavjudmi?
        next_ep_exists = await session.execute(
            select(Series).where(Series.anime_id == anime.id, Series.episode == resume_from)
        )
        if not next_ep_exists.scalar_one_or_none():
            continue  # Keyingi qism hali yuklanmagan

        d = _anime_to_dict(anime)
        d["last_episode"] = hw.last_episode
        d["resume_from"] = resume_from
        d["remaining"] = real_ep_count - hw.last_episode
        items.append(d)

        if len(items) >= 5:
            break

    return items


# ═══════════════════════════════════════════════════════════
#  TRENDING / TOP / RISING / HIDDEN GEMS
# ═══════════════════════════════════════════════════════════


async def get_trending(
    session: AsyncSession,
    content_type: str | None = None,
    limit: int = 10,
    is_pro: bool = False,
) -> list[dict]:
    """So'nggi 7 kunda eng ko'p ko'rilganlar."""
    from datetime import datetime, timedelta

    week_ago = datetime.utcnow() - timedelta(days=7)

    subq = (
        select(ViewRecord.anime_id, func.count(ViewRecord.id).label("cnt"))
        .where(ViewRecord.viewed_at >= week_ago)
        .group_by(ViewRecord.anime_id)
        .order_by(func.count(ViewRecord.id).desc())
        .limit(limit * 2)
        .subquery()
    )

    query = select(Anime, subq.c.cnt).join(subq, Anime.id == subq.c.anime_id)
    if content_type:
        query = query.where(Anime.content_type == content_type)
    if not is_pro:
        query = query.where(Anime.is_pro_locked == False)

    result = await session.execute(query)
    rows = result.fetchall()[:limit]
    return [_anime_to_dict(a, extra={"trend_views": cnt}) for a, cnt in rows]


async def get_top_rated(
    session: AsyncSession,
    content_type: str | None = None,
    limit: int = 10,
    is_pro: bool = False,
) -> list[dict]:
    """Kamida 3 ovoz, eng yuqori reyting."""
    query = select(Anime).where(Anime.rating_count >= 3).order_by(Anime.rating.desc())
    if content_type:
        query = query.where(Anime.content_type == content_type)
    if not is_pro:
        query = query.where(Anime.is_pro_locked == False)

    result = await session.execute(query.limit(limit))
    return [_anime_to_dict(a) for a in result.scalars().all()]


async def get_rising(
    session: AsyncSession,
    content_type: str | None = None,
    limit: int = 10,
    is_pro: bool = False,
) -> list[dict]:
    """So'nggi 3 kunda tez o'sayotganlar."""
    from datetime import datetime, timedelta

    three_days = datetime.utcnow() - timedelta(days=3)

    subq = (
        select(ViewRecord.anime_id, func.count(ViewRecord.id).label("cnt"))
        .where(ViewRecord.viewed_at >= three_days)
        .group_by(ViewRecord.anime_id)
        .order_by(func.count(ViewRecord.id).desc())
        .limit(limit * 2)
        .subquery()
    )

    query = select(Anime, subq.c.cnt).join(subq, Anime.id == subq.c.anime_id)
    if content_type:
        query = query.where(Anime.content_type == content_type)
    if not is_pro:
        query = query.where(Anime.is_pro_locked == False)

    result = await session.execute(query)
    rows = result.fetchall()[:limit]
    return [_anime_to_dict(a, extra={"rising_views": cnt}) for a, cnt in rows]


async def get_hidden_gems(
    session: AsyncSession,
    content_type: str | None = None,
    limit: int = 8,
) -> list[dict]:
    """Kam mashhur lekin yuqori reyting."""
    query = (
        select(Anime)
        .where(Anime.rating >= 8.0)
        .where(Anime.views <= 1000)
        .where(Anime.rating_count >= 2)
        .order_by(Anime.rating.desc())
    )
    if content_type:
        query = query.where(Anime.content_type == content_type)

    result = await session.execute(query.limit(limit))
    return [_anime_to_dict(a) for a in result.scalars().all()]


async def get_related_content(
    session: AsyncSession,
    anime_id: int,
    limit: int = 6,
    is_pro: bool = False,
) -> list[dict]:
    """Related: sequel/prequel/similar + buni ko'rganlar buni ham ko'rgan."""
    results = []
    seen_ids = {anime_id}

    # 1. Bevosita related (DB)
    rel_result = await session.execute(
        select(RelatedContent, Anime)
        .join(Anime, RelatedContent.related_id == Anime.id)
        .where(RelatedContent.anime_id == anime_id)
    )
    for rel, anime in rel_result.fetchall():
        if anime.id in seen_ids:
            continue
        if not is_pro and anime.is_pro_locked:
            continue
        seen_ids.add(anime.id)
        results.append(_anime_to_dict(anime, extra={"relation": rel.relation_type}))

    # 2. Co-watch
    if len(results) < limit:
        watchers = await session.execute(
            select(UserWatchHistory.user_id).where(UserWatchHistory.anime_id == anime_id).limit(50)
        )
        watcher_ids = [r[0] for r in watchers.fetchall()]

        if watcher_ids:
            also_watched = await session.execute(
                select(UserWatchHistory.anime_id, func.count().label("cnt"))
                .where(UserWatchHistory.user_id.in_(watcher_ids), UserWatchHistory.anime_id.notin_(seen_ids))
                .group_by(UserWatchHistory.anime_id)
                .order_by(func.count().desc())
                .limit(limit * 2)
            )
            for row in also_watched.fetchall():
                if len(results) >= limit:
                    break
                a = await session.get(Anime, row[0])
                if a and (is_pro or not a.is_pro_locked):
                    results.append(_anime_to_dict(a, extra={"relation": "also_watched"}))

    return results[:limit]


async def get_next_recommendation(
    session: AsyncSession,
    user_id: int,
    current_anime_id: int,
    is_pro: bool = False,
) -> dict | None:
    """Kontent tugagach keyingi tavsiya. Sequel → personalized."""
    rel = await session.execute(
        select(RelatedContent, Anime)
        .join(Anime, RelatedContent.related_id == Anime.id)
        .where(RelatedContent.anime_id == current_anime_id, RelatedContent.relation_type == "sequel")
        .limit(1)
    )
    row = rel.fetchone()
    if row:
        _, anime = row
        if is_pro or not anime.is_pro_locked:
            return _anime_to_dict(anime, extra={"reason": "sequel"})

    recs = await get_recommendations(session, user_id, limit=1, is_pro=is_pro)
    if recs:
        recs[0]["reason"] = "personalized"
        return recs[0]
    return None


async def get_pro_locked_teaser(
    session: AsyncSession,
    content_type: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Pro-locked kontentlar teaser — FOMO uchun."""
    query = select(Anime).where(Anime.is_pro_locked == True)
    if content_type:
        query = query.where(Anime.content_type == content_type)
    query = query.order_by(Anime.rating.desc()).limit(limit)

    result = await session.execute(query)
    items = []
    for anime in result.scalars().all():
        d = _anime_to_dict(anime)
        d["locked"] = True
        d["description"] = "🔒 Bu kontent Kaworai Pro foydalanuvchilariga ochiq..."
        items.append(d)
    return items


# ═══════════════════════════════════════════════════════════
#  WATCH HISTORY & VIEW RECORD (recommendation.py versiyasi)
# ═══════════════════════════════════════════════════════════


async def record_view(
    session: AsyncSession,
    anime_id: int,
    user_id: int | None = None,
) -> None:
    """Ko'rishni yozadi va views counter oshiradi."""
    try:
        session.add(ViewRecord(anime_id=anime_id, user_id=user_id))
        anime = await session.get(Anime, anime_id)
        if anime:
            anime.views = (anime.views or 0) + 1
        await session.commit()
        if anime and anime.views % 10 == 0:
            await recalculate_popularity(session, anime_id)
    except Exception:
        pass


async def add_to_watch_history(
    session: AsyncSession,
    user_id: int,
    anime_id: int,
    episode: int = 1,
    is_completed: bool = False,
) -> None:
    """
    Watch historyga yozadi.
    LIMIT YO'Q — barcha ko'rilganlar saqlanadi.
    Taste profile ham yangilanadi.
    """
    try:
        existing = await session.execute(
            select(UserWatchHistory).where(
                UserWatchHistory.user_id == user_id,
                UserWatchHistory.anime_id == anime_id,
            )
        )
        hw = existing.scalar_one_or_none()

        if hw:
            if episode > hw.last_episode:
                hw.last_episode = episode
            # ✅ is_completed: faqat haqiqiy oxirgi qism bo'lsa true
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
            await update_taste_profile(session, user_id, anime)

    except Exception:
        pass


async def recalculate_popularity(
    session: AsyncSession,
    anime_id: int,
) -> float:
    """Popularity score = views_7d × 0.4 + rating × 0.4 + admin_pop × 0.2"""
    from datetime import datetime, timedelta

    week_ago = datetime.utcnow() - timedelta(days=7)

    cnt = (
        await session.execute(
            select(func.count(ViewRecord.id)).where(ViewRecord.anime_id == anime_id, ViewRecord.viewed_at >= week_ago)
        )
    ).scalar() or 0

    anime = await session.get(Anime, anime_id)
    if not anime:
        return 0.0

    score = (
        min(cnt / 500.0, 1.0) * 0.4 + (anime.rating / 10.0) * 0.4 + min((anime.popularity or 0) / 10.0, 1.0) * 0.2
    ) * 100

    anime.popularity_score = round(score, 2)
    anime.is_hidden_gem = anime.rating >= 8.0 and cnt <= 100
    await session.commit()
    return anime.popularity_score


# ═══════════════════════════════════════════════════════════
#  YORDAMCHI
# ═══════════════════════════════════════════════════════════


def _anime_to_dict(
    anime: Anime,
    score: float = 0.0,
    extra: dict | None = None,
) -> dict:
    d = {
        "id": anime.id,
        "title": anime.title,
        "type": anime.content_type or "anime",
        "year": anime.year,
        "genres": anime.genres or [],
        "tags": anime.tags or [],
        "mood": anime.mood or [],
        "rating": anime.rating,
        "episodes": anime.episodes_count,
        "status": anime.status,
        "description": anime.description,
        "poster_file_id": anime.poster_file_id,
        "inline_thumbnail_url": anime.inline_thumbnail_url,
        "trailer_file_id": anime.trailer_file_id,
        "popularity_score": anime.popularity_score,
        "is_hidden_gem": anime.is_hidden_gem,
        "is_pro_locked": anime.is_pro_locked,
        "score": score,
    }
    if extra:
        d.update(extra)
    return d
