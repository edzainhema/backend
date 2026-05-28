"""
Lightweight comment / text analyser.

Pure-Python — no external NLP dependencies. Returns:
  - sentiment_label: positive | neutral | negative | question | intent_buy | mixed
  - sentiment_score: float in roughly [-1.0, 1.0]
  - keywords: list of namespaced tags ("intent:purchase", "niche:fitness", ...)

The goal is *not* state-of-the-art accuracy — it's giving the recommendation
layer a coarse signal it can aggregate over many comments per user.
"""

from __future__ import annotations
import re
from typing import Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Lexicons
# ─────────────────────────────────────────────────────────────────────────────

_POSITIVE_WORDS = {
    "love", "loved", "loving", "amazing", "beautiful", "gorgeous", "stunning",
    "awesome", "great", "incredible", "perfect", "best", "favorite", "favourite",
    "cool", "nice", "wow", "yes", "yay", "fire", "lit", "goat", "queen", "king",
    "obsessed", "iconic", "vibes", "bless", "wholesome", "cute", "sweet",
    "lovely", "fantastic", "wonderful", "brilliant", "epic", "legend",
    "blessed", "thankful", "grateful", "smart", "genius",
}
_POSITIVE_EMOJI = {
    "❤", "❤️", "🧡", "💛", "💚", "💙", "💜", "🤍", "🤎",
    "😍", "🥰", "😘", "😻", "🥹", "😊", "😁", "😄", "🤩", "🙌",
    "👏", "👍", "💯", "🔥", "✨", "🌟", "⭐", "🎉", "🎊", "💖",
    "💕", "💞", "💓", "💗", "💘", "💝",
}

_NEGATIVE_WORDS = {
    "hate", "ugly", "bad", "worst", "terrible", "awful", "horrible", "cringe",
    "trash", "garbage", "nasty", "gross", "stupid", "dumb", "boring", "lame",
    "weird", "creepy", "annoying", "fake", "scam", "ew", "yuck", "disgusting",
    "mid", "flop", "ratio", "L",
}
_NEGATIVE_EMOJI = {
    "😡", "🤬", "😠", "👎", "💩", "🤮", "🤢", "😒", "🙄", "😤",
}

# Purchase intent — strong commercial signal.
_INTENT_PATTERNS = [
    re.compile(r"\bwhere can i (?:buy|find|get|order)\b", re.I),
    re.compile(r"\bwhere (?:to|did you) (?:buy|get|find)\b", re.I),
    re.compile(r"\bhow much\b", re.I),
    re.compile(r"\bwhats?\s+the\s+price\b", re.I),
    re.compile(r"\bdrop the link\b", re.I),
    re.compile(r"\blink\??\b", re.I),
    re.compile(r"\bin stock\b", re.I),
    re.compile(r"\bavailable\b", re.I),
    re.compile(r"\bship(?:ping)?\s+to\b", re.I),
    re.compile(r"\bdo you sell\b", re.I),
    re.compile(r"\bfor sale\b", re.I),
    re.compile(r"\bsold out\b", re.I),
    re.compile(r"\bdiscount\b", re.I),
    re.compile(r"\bcoupon\b", re.I),
    re.compile(r"\bpromo code\b", re.I),
    re.compile(r"\border(?:ed|ing)?\b", re.I),
    re.compile(r"\bpurchas(?:e|ing|ed)\b", re.I),
    re.compile(r"\bwant\s+(?:this|that|one|to buy)\b", re.I),
    re.compile(r"\bneed\s+(?:this|that|one)\b", re.I),
]

# Niche / topic dictionary — short hand-tuned starter list.
_NICHE_DICT: Dict[str, List[str]] = {
    "fashion":   ["outfit", "fit", "ootd", "fashion", "style", "stylish", "designer",
                  "vintage", "thrift", "wear", "wearing", "dress", "jeans", "shoes",
                  "sneakers", "heels", "bag", "purse", "handbag", "jewelry", "earrings",
                  "necklace", "ring"],
    "beauty":   ["makeup", "lipstick", "eyeliner", "mascara", "blush", "skincare",
                  "serum", "moisturizer", "skin", "glow", "routine", "haircare",
                  "hair", "salon", "manicure", "nails", "lashes"],
    "fitness":   ["gym", "workout", "lift", "lifting", "squat", "deadlift", "bench",
                  "cardio", "run", "running", "yoga", "pilates", "abs", "muscle",
                  "fit", "fitness", "trainer", "diet", "macros", "protein"],
    "food":      ["recipe", "cook", "cooking", "baking", "delicious", "yum", "yummy",
                  "tasty", "restaurant", "menu", "dish", "meal", "snack", "dinner",
                  "lunch", "breakfast", "brunch", "coffee", "matcha", "espresso"],
    "travel":    ["travel", "trip", "vacation", "holiday", "flight", "hotel", "airbnb",
                  "destination", "wanderlust", "passport", "country", "city", "visit",
                  "tour", "tourist", "adventure"],
    "tech":      ["tech", "code", "coding", "developer", "programmer", "ai", "ml",
                  "iphone", "android", "phone", "laptop", "computer", "startup",
                  "saas", "app", "software", "github"],
    "gaming":    ["game", "gaming", "gamer", "stream", "streamer", "twitch", "ps5",
                  "xbox", "switch", "playstation", "fortnite", "valorant", "lol",
                  "league", "speedrun"],
    "music":     ["music", "song", "track", "album", "concert", "festival", "vinyl",
                  "playlist", "spotify", "artist", "band", "rapper", "singer",
                  "producer", "beat"],
    "art":       ["art", "artist", "drawing", "painting", "sketch", "canvas",
                  "illustration", "design", "designer", "tattoo", "ink"],
    "fitness_running": ["marathon", "5k", "10k", "ultramarathon", "pace", "splits"],
    "pets":      ["dog", "cat", "puppy", "kitten", "pet", "pup", "doggo", "kitty"],
    "parenting": ["baby", "toddler", "kid", "kids", "mom", "dad", "parent", "newborn",
                  "pregnancy", "pregnant"],
    "finance":   ["invest", "stock", "stocks", "crypto", "bitcoin", "btc", "eth",
                  "portfolio", "wealth", "rich", "money", "business", "entrepreneur"],
    "auto":      ["car", "cars", "drift", "drive", "engine", "supercar", "honda",
                  "toyota", "tesla", "porsche", "ferrari", "lambo", "bmw"],
}

_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{1,50})")


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z']+", (text or "").lower())


def extract_hashtags(text: str) -> List[str]:
    """Return lowercased hashtags (without the '#'). De-duplicated, order preserved."""
    seen: List[str] = []
    out: List[str] = []
    for raw in _HASHTAG_RE.findall(text or ""):
        tag = raw.lower()
        if tag in seen:
            continue
        seen.append(tag)
        out.append(tag)
    return out


def extract_post_keywords(text: str) -> List[str]:
    """
    Return the same keyword-tag shape that `analyze_comment` produces, but
    for post-side text (description / caption). The viewer's keyword
    affinity dict in `api.feed.affinity._build_activity_profile` is populated
    mostly from comment-derived keywords (niche:fitness, intent:purchase,
    hashtag:hiking, etc.). To do a real overlap match, the candidate post
    has to surface the same tag vocabulary — which is what this function
    is for.

    Cheap on purpose: one tokenize, one regex for hashtags, plus the
    niche-dict / intent-pattern scans. Called on every candidate post
    during activity-rail ranking (~ACTIVITY_POOL posts per rank), so the
    cost has to stay sub-millisecond. No sentiment/score work here, which
    is what differentiates this from `analyze_comment` — keywords only.
    """
    if not text:
        return []

    tokens = set(_tokenize(text))

    keywords: List[str] = []
    for niche, words in _NICHE_DICT.items():
        if tokens & set(words):
            keywords.append(f"niche:{niche}")

    for pat in _INTENT_PATTERNS:
        if pat.search(text):
            keywords.append("intent:purchase")
            break  # one flag is enough; multiple intent matches don't multiply

    for tag in extract_hashtags(text):
        keywords.append(f"hashtag:{tag}")

    # De-dup while preserving order
    seen: set = set()
    deduped: List[str] = []
    for k in keywords:
        if k in seen:
            continue
        seen.add(k)
        deduped.append(k)
    return deduped


def analyze_comment(text: str) -> Tuple[str, float, List[str]]:
    """
    Returns (sentiment_label, sentiment_score, keywords).

    sentiment_score is a normalised score in [-1.0, 1.0] computed from the
    ratio of positive to negative tokens. Purchase intent overrides the label
    to "intent_buy" (it's a much more useful signal than raw sentiment).
    """
    if not text:
        return ("neutral", 0.0, [])

    tokens = set(_tokenize(text))

    pos_hits = len(tokens & _POSITIVE_WORDS)
    for emoji in _POSITIVE_EMOJI:
        if emoji in text:
            pos_hits += 1

    neg_hits = len(tokens & _NEGATIVE_WORDS)
    for emoji in _NEGATIVE_EMOJI:
        if emoji in text:
            neg_hits += 1

    intent_hits = sum(1 for pat in _INTENT_PATTERNS if pat.search(text))
    is_question = "?" in text or text.lower().lstrip().startswith(
        ("how ", "where ", "when ", "what ", "why ", "who ", "is ", "do ", "can ", "could ", "would ")
    )

    # Niche keyword tagging
    keywords: List[str] = []
    for niche, words in _NICHE_DICT.items():
        if tokens & set(words):
            keywords.append(f"niche:{niche}")

    if intent_hits > 0:
        keywords.append("intent:purchase")

    # Hashtag keywords (useful for topic clustering)
    for tag in extract_hashtags(text):
        keywords.append(f"hashtag:{tag}")

    # ── Score & label ────────────────────────────────────────────────────────
    total = pos_hits + neg_hits
    score = 0.0 if total == 0 else (pos_hits - neg_hits) / total

    if intent_hits > 0:
        label = "intent_buy"
    elif pos_hits > 0 and neg_hits > 0:
        label = "mixed"
    elif pos_hits > 0:
        label = "positive"
    elif neg_hits > 0:
        label = "negative"
    elif is_question:
        label = "question"
    else:
        label = "neutral"

    # De-dup while preserving order
    seen = set()
    deduped = []
    for k in keywords:
        if k in seen:
            continue
        seen.add(k)
        deduped.append(k)

    return (label, score, deduped)
