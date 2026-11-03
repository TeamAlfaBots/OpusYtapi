#!/bin/bash

# Prompt for URL and Type
read -p "Enter YouTube URL: " URL
read -p "Audio or Video? (audio/video): " TYPE

# Create a downloads directory to avoid conflicts
mkdir -p downloads
cd downloads || exit

# Use the yt-dlp from virtualenv if available
if [ -f "../yvenv/bin/yt-dlp" ]; then
    YTDLP="../yvenv/bin/yt-dlp"
else
    YTDLP="yt-dlp"
fi

echo "Downloading $TYPE..."
if [ "$TYPE" == "audio" ]; then
    $YTDLP -x --audio-format mp3 "$URL"
else
    $YTDLP -f "bestvideo[ext=mp4]+bestaudio[m4a]/best[ext=mp4]/best" "$URL"
fi

# Get the most recently downloaded file
FILE=$(ls -t | head -n1)
if [ -z "$FILE" ]; then
    echo "Download failed!"
    exit 1
fi

FULL_PATH="$(pwd)/$FILE"
echo ""
echo "Downloaded successfully!"
echo "Path: $FULL_PATH"
echo "------------------------------------------------"
