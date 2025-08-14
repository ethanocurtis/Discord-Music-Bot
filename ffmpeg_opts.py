# audio/ffmpeg_opts.py
# Centralized FFmpeg options for utila-music-bot

FFMPEG_BASE_OPTS = [
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
    "-vn",
    "-af", "aresample=async=1:min_hard_comp=0.100000:first_pts=0",
]
