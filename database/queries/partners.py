# database/queries/partners.py

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Partner, User

# ===== PARTNER QUERIES =====


async def get_active_partners(session: AsyncSession) -> list[Partner]:
    """Barcha aktiv hamkorlarni olish"""
    result = await session.execute(select(Partner).where(Partner.is_active == True))
    return result.scalars().all()


async def add_partner(session: AsyncSession, channel_id: int, channel_name: str, channel_url: str) -> Partner:
    """Yangi hamkor kanal qo'shish"""
    partner = Partner(channel_id=channel_id, channel_name=channel_name, channel_url=channel_url)
    session.add(partner)
    await session.commit()
    await session.refresh(partner)
    return partner


async def remove_partner(session: AsyncSession, channel_id: int) -> bool:
    """Hamkorni o'chirish"""
    result = await session.execute(delete(Partner).where(Partner.channel_id == channel_id))
    await session.commit()
    return result.rowcount > 0


async def get_all_partners(session: AsyncSession) -> list[Partner]:
    """Admin uchun — aktiv + nоaktiv hamkorlar"""
    result = await session.execute(select(Partner))
    return result.scalars().all()


async def toggle_partner(session: AsyncSession, channel_id: int) -> bool | None:
    """Hamkorni yoqish/o'chirish"""
    result = await session.execute(select(Partner).where(Partner.channel_id == channel_id))
    partner = result.scalar_one_or_none()
    if not partner:
        return None
    partner.is_active = not partner.is_active
    await session.commit()
    return partner.is_active


# ===== USER QUERIES =====


async def get_or_create_user(
    session: AsyncSession, user_id: int, username: str | None, full_name: str, ref_by: int | None = None
) -> tuple[User, bool]:
    """Foydalanuvchini olish yoki yaratish. (user, is_new) qaytaradi"""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        return user, False

    user = User(id=user_id, username=username, full_name=full_name, ref_by=ref_by)
    session.add(user)
    await session.commit()
    return user, True


async def get_user_count(session: AsyncSession) -> int:
    """Jami foydalanuvchilar soni"""
    from sqlalchemy import func

    result = await session.execute(select(func.count(User.id)))
    return result.scalar()
