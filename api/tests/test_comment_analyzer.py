"""Pure-logic tests for the comment / text analyser.

This feeds the recommendation layer (sentiment + namespaced keyword tags),
so its output contract is worth pinning down.
"""
from django.test import SimpleTestCase

from api.services.comment_analyzer import (
    analyze_comment,
    extract_hashtags,
    extract_post_keywords,
)


class ExtractHashtagsTests(SimpleTestCase):
    def test_lowercased_deduped_order_preserved(self):
        self.assertEqual(
            extract_hashtags("Love this #Sunset and #beach, #BEACH again"),
            ["sunset", "beach"],
        )

    def test_no_hashtags(self):
        self.assertEqual(extract_hashtags("just a plain caption"), [])

    def test_empty_or_none(self):
        self.assertEqual(extract_hashtags(""), [])
        self.assertEqual(extract_hashtags(None), [])


class ExtractPostKeywordsTests(SimpleTestCase):
    def test_niche_and_hashtag_tags(self):
        tags = extract_post_keywords("Hitting the gym for a workout #fitness")
        self.assertIn("niche:fitness", tags)
        self.assertIn("hashtag:fitness", tags)

    def test_purchase_intent_flag(self):
        tags = extract_post_keywords("where can I buy this jacket?")
        self.assertIn("intent:purchase", tags)

    def test_empty_returns_empty(self):
        self.assertEqual(extract_post_keywords(""), [])


class AnalyzeCommentTests(SimpleTestCase):
    def test_positive(self):
        label, score, _ = analyze_comment("I love this, it's amazing!")
        self.assertEqual(label, "positive")
        self.assertGreater(score, 0)

    def test_negative(self):
        label, score, _ = analyze_comment("this is terrible and awful")
        self.assertEqual(label, "negative")
        self.assertLess(score, 0)

    def test_purchase_intent_overrides_label(self):
        label, _, keywords = analyze_comment("where can I buy this??")
        self.assertEqual(label, "intent_buy")
        self.assertIn("intent:purchase", keywords)

    def test_empty_is_neutral(self):
        self.assertEqual(analyze_comment(""), ("neutral", 0.0, []))

    def test_mixed_sentiment(self):
        label, _, _ = analyze_comment("the design is beautiful but the price is awful")
        self.assertEqual(label, "mixed")
