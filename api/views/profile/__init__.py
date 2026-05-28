"""Profile views, split by concern. Re-exported so `from ..profile import X`
and `views.X` keep resolving unchanged."""

from .read import (
    profile,
    my_avatar,
    get_user_profile,
)
from .directory import (
    list_users,
)
from .settings import (
    update_profile_settings,
    update_profile_avatar,
)

__all__ = [
    "list_users",
    "profile",
    "my_avatar",
    "get_user_profile",
    "update_profile_settings",
    "update_profile_avatar",
]
