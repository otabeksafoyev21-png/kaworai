from .admin import admin_router
from .callbacks import callback_router
from .genres import genre_router
from .users import user_router

__all__ = ["admin_router", "callback_router", "genre_router", "user_router"]
