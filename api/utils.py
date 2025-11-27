import os
import subprocess
from django.conf import settings

def compress_video(input_path):
    # Create output path
    filename = os.path.basename(input_path)
    output_path = os.path.join(settings.MEDIA_ROOT, "uploads/compressed", f"compressed_{filename}")

    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # FFmpeg compression command (H.264)
    command = [
        "ffmpeg", "-i", input_path,
        "-vcodec", "libx264",
        "-crf", "28",               # Compression level (lower = better quality)
        "-preset", "veryfast",      # Change to "slow" for better compression
        "-acodec", "aac",
        "-b:a", "128k",
        output_path
    ]

    # Run FFmpeg
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return output_path
