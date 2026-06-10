"""Unit tests for the pure HLS-ladder argument builders (api/services/media/hls).

These exercise the deterministic command-construction helpers — they do NOT
invoke ffmpeg, so they're fast and run anywhere. The end-to-end encode is
validated separately against a real ffmpeg binary.
"""
from django.test import SimpleTestCase

from api.services.media.hls import (
    HLS_LADDER,
    MASTER_NAME,
    build_filter_complex,
    build_hls_ffmpeg_args,
    build_var_stream_map,
)


class FilterComplexTests(SimpleTestCase):
    def test_splits_into_one_branch_per_rung(self):
        fc = build_filter_complex()
        self.assertTrue(fc.startswith(f"[0:v]split={len(HLS_LADDER)}"))
        # One scaled output label per rung.
        for i in range(len(HLS_LADDER)):
            self.assertIn(f"[v{i}out]", fc)

    def test_scale_caps_longer_edge_and_keeps_even(self):
        fc = build_filter_complex()
        # Orientation-agnostic cap: landscape -> width=target, else height.
        for rung in HLS_LADDER:
            self.assertIn(f"if(gte(iw,ih),{rung['size']},-2)", fc)
            self.assertIn(f"if(gte(iw,ih),-2,{rung['size']})", fc)


class VarStreamMapTests(SimpleTestCase):
    def test_pairs_audio_per_variant_when_present(self):
        self.assertEqual(build_var_stream_map(3, True), "v:0,a:0 v:1,a:1 v:2,a:2")

    def test_video_only_when_no_audio(self):
        self.assertEqual(build_var_stream_map(3, False), "v:0 v:1 v:2")


class FfmpegArgsTests(SimpleTestCase):
    def test_args_have_a_video_map_and_encoder_per_rung(self):
        args = build_hls_ffmpeg_args("in.mp4", "/out", has_audio=True)
        n = len(HLS_LADDER)
        # One [vNout] map + libx264 stream specifier per rung.
        for i in range(n):
            self.assertIn(f"[v{i}out]", args)
            self.assertIn(f"-c:v:{i}", args)
        # HLS muxer wiring is present and points at our segment/master names.
        self.assertIn("-f", args)
        self.assertIn("hls", args)
        self.assertIn("-master_pl_name", args)
        self.assertIn(MASTER_NAME, args)
        self.assertIn("vod", args)

    def test_audio_maps_only_emitted_when_audio_present(self):
        with_audio = build_hls_ffmpeg_args("in.mp4", "/out", has_audio=True)
        without_audio = build_hls_ffmpeg_args("in.mp4", "/out", has_audio=False)
        # a:0 is mapped once per rung when audio exists; never when it doesn't.
        self.assertEqual(with_audio.count("a:0"), len(HLS_LADDER))
        self.assertNotIn("a:0", without_audio)
        self.assertIn("aac", with_audio)
        self.assertNotIn("aac", without_audio)

    def test_var_stream_map_matches_audio_flag(self):
        args = build_hls_ffmpeg_args("in.mp4", "/out", has_audio=False)
        idx = args.index("-var_stream_map")
        self.assertEqual(args[idx + 1], "v:0 v:1 v:2")
