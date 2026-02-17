"""
Post management dashboard for @tatamispaces and Museum Stories.
Single page with niche toggle.

Usage: python dashboard.py [--port 8080]
Opens at http://localhost:8080
  /?niche=tatamispaces   — tatami view (default)
  /?niche=museumstories  — museum view
"""

import json
import os
import sys
import asyncio
import fcntl
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
POSTS_LOCK = BASE_DIR / ".posts.json.lock"
TEMPLATES_DIR = BASE_DIR / "templates"

# Load .env so writer agent can find ANTHROPIC_API_KEY
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR))
from config.niches import get_niche


def _get_posts_file(niche_id: str) -> Path:
    """Resolve posts file path from niche config."""
    niche = get_niche(niche_id)
    return BASE_DIR / niche.get("posts_file", "posts.json")


def _lock_posts():
    """Acquire exclusive lock on posts.json for safe read-modify-write."""
    POSTS_LOCK.touch(exist_ok=True)
    fd = open(POSTS_LOCK, "r")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock_posts(fd):
    """Release posts.json lock."""
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def load_posts(niche_id: str = "tatamispaces") -> dict:
    posts_file = _get_posts_file(niche_id)
    if not posts_file.exists():
        return {"posts": []}
    data = json.loads(posts_file.read_text())
    if isinstance(data, list):
        data = {"posts": data}
    return data


def save_posts(data: dict, niche_id: str = "tatamispaces"):
    """Atomic write: tmp file then rename (matches tools/common.py save_json)."""
    posts_file = _get_posts_file(niche_id)
    tmp = posts_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(posts_file)


def render_post_html(post, index):
    pid = post.get("id", index)
    status = post.get("status", "unknown")
    text = post.get("text", "")
    source = post.get("source", "")
    source_handle = post.get("source_handle", "")
    category = post.get("category", "")
    tweet_id = post.get("tweet_id", "")
    ig_posted = post.get("ig_posted", False)
    posted_at = post.get("posted_at", "")
    scheduled = post.get("scheduled_for", "")

    # Image URLs — build both thumbnail and full-res lists
    image_urls = post.get("image_urls") or []
    img_index = post.get("image_index")
    img_count = post.get("image_count")

    # Full-res URLs for lightbox (orig quality)
    full_urls = []
    for url in image_urls[:4]:
        full = url
        if "pbs.twimg.com" in url and "format=" not in url:
            full = url + "?format=jpg&name=orig"
        elif "format=" in url:
            full = url.replace("name=small", "name=orig")
        full_urls.append(full)

    # JSON-safe URL list for onclick
    import html as html_mod
    urls_json = html_mod.escape(json.dumps(full_urls))

    imgs_html = ""
    for idx, url in enumerate(image_urls[:4]):
        # Thumbnail URL
        thumb = url
        if "pbs.twimg.com" in url and "format=" not in url:
            thumb = url + "?format=jpg&name=small"
        elif "format=" in url:
            thumb = url.replace("name=orig", "name=small")

        selected = ""
        if img_index is not None and idx == img_index:
            selected = " selected"
        elif img_index is not None and idx != img_index:
            selected = " dimmed"
        elif img_count is not None and idx >= img_count:
            selected = " dimmed"

        imgs_html += f'<div class="img-wrap{selected}"><span class="img-num">{idx+1}</span><img src="{thumb}" loading="lazy" onerror="this.parentElement.style.display=\'none\'" onclick="openLightbox({pid}, {urls_json}, {idx})"></div>'

    if len(image_urls) > 1 and status in ("approved", "dropped") or (status.startswith("skipped") and len(image_urls) > 1):
        current = "all"
        if img_index is not None:
            current = f"#{img_index+1} only"
        elif img_count is not None:
            current = f"first {img_count}"
        imgs_html += f'<div class="img-controls">Using: {current} <button onclick="resetImages({pid})">all</button></div>'

    # Badges
    badges = ""
    badge_class = f"badge-{status}" if status in ("posted", "approved", "dropped", "draft") else "badge-skipped"
    if status.startswith("skipped"):
        badges += f'<span class="badge badge-skipped">skipped</span>'
    else:
        badges += f'<span class="badge {badge_class}">{status}</span>'

    post_type = post.get("type", "")
    if post_type == "quote-tweet":
        badges += '<span class="badge" style="background:#1d9bf0;color:#fff">QT</span>'
    if tweet_id:
        badges += '<span class="badge badge-x">X</span>'
    if ig_posted:
        badges += '<span class="badge badge-ig">IG</span>'

    # Meta info
    meta = ""
    if source_handle:
        meta += f'<span>from: {source_handle}</span>'
    elif source:
        meta += f'<span>source: {source}</span>'
    quote_id = post.get("quote_tweet_id")
    if quote_id:
        qt_handle = (source_handle or "").lstrip("@") or "i"
        meta += f'<span>quoting: <a href="https://x.com/{qt_handle}/status/{quote_id}" target="_blank" style="color:#1d9bf0">view original</a></span>'
    if category:
        meta += f'<span>category: {category}</span>'
    if posted_at:
        meta += f'<span>posted: {posted_at[:16]}</span>'
    elif scheduled:
        meta += f'<span>scheduled: {scheduled[:16]}</span>'

    score = post.get("score")
    if score is not None:
        meta += f'<span>score: {score}/10</span>'

    # Actions
    actions = ""
    if status == "draft":
        actions = f'<button class="btn-approve" onclick="setStatus({pid},\'approved\')">Approve</button> <button class="btn-skip" onclick="setStatus({pid},\'dropped\')">Delete</button>'
    elif status == "approved":
        actions = f'<button class="btn-skip" onclick="setStatus({pid},\'dropped\')">Skip</button>'
    elif status == "dropped" or status.startswith("skipped"):
        actions = f'<button class="btn-approve" onclick="setStatus({pid},\'approved\')">Approve</button>'

    css_class = "posted" if status == "posted" else ("dropped" if status == "dropped" or status.startswith("skipped") else "")

    regen_html = ""
    if status in ("draft", "approved"):
        regen_html = f'''<div class="regen-row" id="regen-{pid}">
          <button class="btn-regen" onclick="toggleRegen({pid})" title="Regenerate caption">&#x21bb;</button>
          <div class="regen-input" style="display:none">
            <input placeholder="Direction (optional)..." id="regen-fb-{pid}">
            <button onclick="regenerate({pid})">Go</button>
          </div>
        </div>'''

    return f'''<div class="post {css_class}" data-status="{status}" data-id="{pid}">
      <div class="post-images">{imgs_html}</div>
      <div class="post-body">
        <div class="post-id">#{pid} {badges}</div>
        <div class="post-text">{text}</div>
        {regen_html}
        <div class="post-meta">{meta}</div>
        <div class="post-actions">{actions}</div>
      </div>
    </div>'''


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "":
            self.serve_dashboard()
        elif parsed.path in ("/museum", "/museum/"):
            # Serve museum view directly (avoids redirect issues behind nginx proxy)
            self.path = "/?niche=museumstories"
            self.serve_dashboard()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/api/status":
            post_id = body.get("id")
            new_status = body.get("status")
            niche_id = body.get("niche", "tatamispaces")

            if not post_id or not new_status:
                self.send_json({"ok": False, "error": "missing id or status"})
                return

            lock = _lock_posts()
            try:
                data = load_posts(niche_id)
                found = False
                for p in data.get("posts", []):
                    if p.get("id") == post_id:
                        p["status"] = new_status
                        if new_status == "approved":
                            p.pop("ig_skip_reason", None)
                            p.pop("skip_reason", None)
                        found = True
                        break

                if found:
                    save_posts(data, niche_id)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "post not found"})
            finally:
                _unlock_posts(lock)

        elif self.path == "/api/image-select":
            post_id = body.get("id")
            niche_id = body.get("niche", "tatamispaces")
            if not post_id:
                self.send_json({"ok": False, "error": "missing id"})
                return

            lock = _lock_posts()
            try:
                data = load_posts(niche_id)
                found = False
                for p in data.get("posts", []):
                    if p.get("id") == post_id:
                        if body.get("image_index") is not None:
                            p["image_index"] = body["image_index"]
                            p.pop("image_count", None)
                        else:
                            p.pop("image_index", None)

                        if body.get("image_count") is not None:
                            p["image_count"] = body["image_count"]
                        elif "image_index" not in body:
                            p.pop("image_count", None)

                        found = True
                        break

                if found:
                    save_posts(data, niche_id)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "post not found"})
            finally:
                _unlock_posts(lock)

        elif self.path == "/api/museum/status":
            self.handle_museum_status(body)

        elif self.path == "/api/museum/tweet-edit":
            self.handle_museum_tweet_edit(body)

        elif self.path == "/api/museum/image-assign":
            self.handle_museum_image_assign(body)

        elif self.path == "/api/museum/notes":
            self.handle_museum_notes(body)

        elif self.path == "/api/regenerate":
            self.handle_regenerate(body)

        elif self.path == "/api/museum/regenerate":
            self.handle_museum_regenerate(body)

        else:
            self.send_error(404)

    # === Museum API handlers ===

    def handle_museum_status(self, body):
        post_id = body.get("id")
        new_status = body.get("status")
        if not post_id or not new_status:
            self.send_json({"ok": False, "error": "missing id or status"})
            return
        data = load_posts("museumstories")
        for p in data["posts"]:
            if p["id"] == post_id:
                p["status"] = new_status
                # Auto-set scheduled_for on approve so post.py picks it up
                if new_status == "approved" and not p.get("scheduled_for"):
                    p["scheduled_for"] = datetime.now(timezone.utc).isoformat()
                save_posts(data, "museumstories")
                self.send_json({"ok": True})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_tweet_edit(self, body):
        post_id = body.get("id")
        tweet_index = body.get("tweet_index")
        text = body.get("text")
        if post_id is None or tweet_index is None or text is None:
            self.send_json({"ok": False, "error": "missing id, tweet_index, or text"})
            return
        data = load_posts("museumstories")
        for p in data["posts"]:
            if p["id"] == post_id:
                if 0 <= tweet_index < len(p.get("tweets", [])):
                    p["tweets"][tweet_index]["text"] = text
                    save_posts(data, "museumstories")
                    self.send_json({"ok": True})
                    return
                self.send_json({"ok": False, "error": "invalid tweet_index"})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_image_assign(self, body):
        post_id = body.get("post_id")
        tweet_index = body.get("tweet_index")
        image_index = body.get("image_index")
        action = body.get("action")
        if post_id is None or tweet_index is None or image_index is None or action not in ("add", "remove"):
            self.send_json({"ok": False, "error": "missing post_id, tweet_index, image_index, or action"})
            return
        data = load_posts("museumstories")
        for p in data["posts"]:
            if p["id"] == post_id:
                if 0 <= tweet_index < len(p.get("tweets", [])):
                    tweet = p["tweets"][tweet_index]
                    if "images" not in tweet:
                        tweet["images"] = []
                    if action == "add":
                        if image_index not in tweet["images"]:
                            tweet["images"].append(image_index)
                    elif action == "remove":
                        tweet["images"] = [i for i in tweet["images"] if i != image_index]
                    save_posts(data, "museumstories")
                    self.send_json({"ok": True})
                    return
                self.send_json({"ok": False, "error": "invalid tweet_index"})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_notes(self, body):
        post_id = body.get("id")
        if post_id is None:
            self.send_json({"ok": False, "error": "missing id"})
            return
        data = load_posts("museumstories")
        for p in data["posts"]:
            if p["id"] == post_id:
                if "vote" in body:
                    p["vote"] = body["vote"]
                if "notes" in body:
                    p["notes"] = body["notes"]
                save_posts(data, "museumstories")
                self.send_json({"ok": True})
                return
        self.send_json({"ok": False, "error": "post not found"})

    # === Regenerate handlers ===

    def handle_regenerate(self, body):
        """Regenerate a tatami post caption via writer agent."""
        post_id = body.get("id")
        niche_id = body.get("niche", "tatamispaces")
        feedback = body.get("feedback", "Try a different angle. Keep the same facts but find a fresher way to say it.")
        if not post_id:
            self.send_json({"ok": False, "error": "missing id"})
            return
        data = load_posts(niche_id)
        for p in data.get("posts", []):
            if p.get("id") == post_id:
                original = p.get("text", "")
                if not original:
                    self.send_json({"ok": False, "error": "post has no text"})
                    return
                try:
                    from agents.writer import rewrite_caption
                    result = asyncio.run(rewrite_caption(niche_id, original, feedback))
                    p["text"] = result["caption"]
                    p["_previous_text"] = original
                    save_posts(data, niche_id)
                    self.send_json({"ok": True, "caption": result["caption"]})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})
                return
        self.send_json({"ok": False, "error": "post not found"})

    def handle_museum_regenerate(self, body):
        """Regenerate a museum tweet caption via writer agent."""
        post_id = body.get("id")
        tweet_index = body.get("tweet_index")
        feedback = body.get("feedback", "Try a different angle. Keep the same facts but find a fresher way to say it.")
        if post_id is None or tweet_index is None:
            self.send_json({"ok": False, "error": "missing id or tweet_index"})
            return
        data = load_posts("museumstories")
        for p in data["posts"]:
            if p["id"] == post_id:
                if 0 <= tweet_index < len(p.get("tweets", [])):
                    original = p["tweets"][tweet_index]["text"]
                    try:
                        from agents.writer import rewrite_caption
                        result = asyncio.run(rewrite_caption("museumstories", original, feedback))
                        p["tweets"][tweet_index]["text"] = result["caption"]
                        p["tweets"][tweet_index]["_previous_text"] = original
                        save_posts(data, "museumstories")
                        self.send_json({"ok": True, "caption": result["caption"]})
                    except Exception as e:
                        self.send_json({"ok": False, "error": str(e)})
                    return
                self.send_json({"ok": False, "error": "invalid tweet_index"})
                return
        self.send_json({"ok": False, "error": "post not found"})

    # === Dashboard serve ===

    def serve_dashboard(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        niche = qs.get("niche", ["tatamispaces"])[0]

        html = (TEMPLATES_DIR / "dashboard.html").read_text()
        html = html.replace("__NICHE__", niche)

        if niche == "tatamispaces":
            # Tatami: server-rendered post cards
            data = load_posts(niche)
            posts = data.get("posts", [])

            counts = {"all": len(posts), "approved": 0, "posted": 0, "dropped": 0}
            for p in posts:
                s = p.get("status", "")
                if s == "approved":
                    counts["approved"] += 1
                elif s == "posted":
                    counts["posted"] += 1
                elif s == "dropped" or s.startswith("skipped"):
                    counts["dropped"] += 1

            posts_html = ""
            for i, p in enumerate(posts):
                posts_html += render_post_html(p, i)

            html = html.replace("POSTS_HTML", posts_html)
            html = html.replace("POST_COUNT_ALL", str(counts["all"]))
            html = html.replace("POST_COUNT_APPROVED", str(counts["approved"]))
            html = html.replace("POST_COUNT_POSTED", str(counts["posted"]))
            html = html.replace("POST_COUNT_DROPPED", str(counts["dropped"]))
            html = html.replace("__POSTS_DATA__", "[]")
            html = html.replace("__STATS_HTML__", "")
            html = html.replace("__ACTIVE_TATAMI__", "active")
            html = html.replace("__ACTIVE_MUSEUM__", "")
        else:
            # Museum: client-rendered from JSON
            museum_data = load_posts(niche)
            posts_json = json.dumps(museum_data.get("posts", []), ensure_ascii=False)

            html = html.replace("__POSTS_DATA__", posts_json)
            html = html.replace("POSTS_HTML", "")
            html = html.replace("POST_COUNT_ALL", "0")
            html = html.replace("POST_COUNT_APPROVED", "0")
            html = html.replace("POST_COUNT_POSTED", "0")
            html = html.replace("POST_COUNT_DROPPED", "0")
            html = html.replace("__STATS_HTML__", "")
            html = html.replace("__ACTIVE_TATAMI__", "")
            html = html.replace("__ACTIVE_MUSEUM__", "active")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def main():
    parser = argparse.ArgumentParser(description="Post management dashboard")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
