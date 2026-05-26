"""Candidate rails for the home feed. One module per rail; see compose.py for how they are slotted."""
from .activity import _rail_activity
from .collaborative import _rail_collaborative
from .friend_network import _rail_friend_network
from .nearby import _rail_nearby, _viewer_location
from .trending import _rail_trending

__all__ = [
    "_rail_friend_network", "_rail_nearby", "_viewer_location",
    "_rail_activity", "_rail_collaborative", "_rail_trending",
]
