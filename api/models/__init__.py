# Auto-split from the former monolithic api/models.py by domain.
# All models keep app_label 'api' and identical fields, so this split is
# migration-neutral (verified via `makemigrations --check`). Re-exported
# from api/models/__init__.py so `from api.models import X` still works.

from .users import (
    UserProfile,
    Follow,
    FollowRequest,
)
from .pages import (
    Page,
    PageChatMessage,
    PageChatMessageMedia,
    PageFollow,
    PageFollowRequest,
    PageInvite,
    Memory,
    PagePoster,
    PinnedPage,
    MutedPage,
    PageReport,
)
from .posts import (
    Media,
    Comment,
    CommentMention,
    Post,
    PostMedia,
    PostMediaTag,
    PostHashtag,
    PostLike,
    SavedPost,
    CommentLike,
    ReelWatch,
    VideoWatch,
)
from .messaging import (
    Conversation,
    Message,
    MessageReaction,
    MessageMedia,
    ConversationHidden,
)
from .devices import (
    Device,
)
from .notifications import (
    Notification,
)
from .feed import (
    NotInterested,
    RecommendedAuthor,
    UserAffinityProfile,
    UserCloseFriends,
    ProfileVisit,
    SearchHistory,
    Activity,
)
from .moderation import (
    PostReport,
    MutedUser,
    BlockedUserQuerySet,
    BlockedUser,
    UserReport,
)
