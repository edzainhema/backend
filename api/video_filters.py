# Maps each FILTERS index (from the React Native constants/filters.ts)
# to an FFmpeg -vf filter string applied server-side.
#
# Index must match 1:1 with the FILTERS array on the frontend.

VIDEO_FILTER_CHAINS = {
    0: None,  # Normal — no processing

    # Vivid: all channels ×1.4, bias -0.1
    1: "curves=r='0/0 1/1':g='0/0 1/1':b='0/0 1/1',eq=contrast=1.4:saturation=1.4",

    # Muted: all channels ×0.8 + 0.1 bias
    2: "colorlevels=romin=0.1:romax=0.9:gomin=0.1:gomax=0.9:bomin=0.1:bomax=0.9",

    # B&W: equal channel mix → grayscale
    3: "colorchannelmixer=0.33:0.33:0.33:0:0.33:0.33:0.33:0:0.33:0.33:0.33:0",

    # Warm: R×1.2+0.05, G×1.0, B×0.8
    4: "colorchannelmixer=1.2:0:0:0:0:1.0:0:0:0:0:0.8:0,colorlevels=romin=0.05:romax=1:gomin=0:gomax=1:bomin=0:bomax=1",

    # Cool: R×0.8, G×1.0, B×1.3+0.05
    5: "colorchannelmixer=0.8:0:0:0:0:1.0:0:0:0:0:1.3:0,colorlevels=romin=0:romax=1:gomin=0:gomax=1:bomin=0.05:bomax=1",

    # Dusk: R×1.0, G×0.8, B×1.2
    6: "colorchannelmixer=1:0:0:0:0:0.8:0:0:0:0:1.2:0",

    # Fade: all channels ×0.9 + 0.1 bias
    7: "colorlevels=romin=0.1:romax=1.0:gomin=0.1:gomax=1.0:bomin=0.1:bomax=1.0",

    # Golden: R×1.3+0.05, G×1.1, B×0.6
    8: "colorchannelmixer=1.3:0:0:0:0:1.1:0:0:0:0:0.6:0,colorlevels=romin=0.05:romax=1:gomin=0:gomax=1:bomin=0:bomax=1",
}
