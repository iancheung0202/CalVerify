import time
import re
import html
import feedparser
import requests
import os
from dotenv import load_dotenv

load_dotenv()

FEED_URL = os.getenv("INSTAGRAM_FEED_URL")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
CHECK_INTERVAL_SECONDS = 60
LAST_POST_FILE = "last_post.txt"

try:
    with open(LAST_POST_FILE, "r") as f:
        last_seen = f.read().strip()
except FileNotFoundError:
    last_seen = None

print("Starting RSS monitor...")

while True:
    feed = feedparser.parse(FEED_URL)

    if feed.entries:
        latest = feed.entries[0]
        if latest.link != last_seen:
            print(f"New post: {latest.link}")
            raw_summary = latest.get("summary", "")

            # image/video poster is embedded as an HTML attribute in the summary
            img_match = re.search(r'poster="([^"]+)"', raw_summary) or re.search(r'src="([^"]+\.jpg[^"]*)"', raw_summary)
            image_url = html.unescape(img_match.group(1)) if img_match else None

            # strip all HTML tags to get plain caption text
            caption = re.sub(r"<[^>]+>", "", raw_summary)
            caption = html.unescape(caption)
            caption = re.sub(r"\n\s*\n+", "\n\n", caption).strip()  # collapse extra blank lines
            caption = caption or "No caption provided."

            payload = {
                "content": latest.link,
                "embeds": [
                    {"title": caption.split("\n")[0], "url": latest.link, "description": caption, "color": 0x002676, "image": {"url": image_url}}
                ],
            }
            requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)

            last_seen = latest.link
            with open(LAST_POST_FILE, "w") as f:
                f.write(latest.link)
        else:
            print("No new post.")
    else:
        print("Feed returned no entries.")

    time.sleep(CHECK_INTERVAL_SECONDS)