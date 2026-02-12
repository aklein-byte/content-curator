"""
Post management dashboard for @tatamispaces.
Simple web UI to preview, approve, skip posts, and pick images.

Usage: python dashboard.py [--port 8080]
Opens at http://localhost:8080
"""

import json
import os
import sys
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
POSTS_FILE = BASE_DIR / "posts.json"


def load_posts():
    data = json.loads(POSTS_FILE.read_text())
    if isinstance(data, list):
        data = {"posts": data}
    return data


def save_posts(data):
    POSTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tatamispaces — post manager</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #ddd; padding: 20px; }
  h1 { font-size: 18px; font-weight: 500; margin-bottom: 16px; color: #888; }
  .filters { margin-bottom: 20px; display: flex; gap: 8px; flex-wrap: wrap; }
  .filters button { padding: 6px 14px; border: 1px solid #333; background: #1a1a1a; color: #aaa;
    border-radius: 4px; cursor: pointer; font-size: 13px; }
  .filters button.active { border-color: #666; color: #fff; background: #2a2a2a; }
  .post { border: 1px solid #222; border-radius: 8px; margin-bottom: 12px; padding: 16px;
    background: #1a1a1a; display: flex; gap: 16px; }
  .post.posted { opacity: 0.5; }
  .post.dropped { opacity: 0.3; }
  .post-images { flex-shrink: 0; display: flex; gap: 4px; flex-wrap: wrap; max-width: 260px; }
  .post-images .img-wrap { position: relative; }
  .post-images .img-wrap img { width: 120px; height: 120px; object-fit: cover; border-radius: 4px;
    cursor: pointer; transition: outline 0.1s; }
  .post-images .img-wrap img:hover { outline: 2px solid #6ac; outline-offset: 1px; }
  .post-images .img-wrap .img-num { position: absolute; top: 3px; left: 3px; background: rgba(0,0,0,0.7);
    color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; pointer-events: none; }
  .post-images .img-wrap.selected img { outline: 2px solid #6ac; outline-offset: 1px; }
  .post-images .img-wrap.dimmed img { opacity: 0.3; }
  .img-controls { font-size: 11px; color: #666; margin-top: 4px; width: 100%; }
  .img-controls button { font-size: 10px; padding: 2px 8px; background: #222; border: 1px solid #333;
    color: #888; border-radius: 3px; cursor: pointer; }
  .img-controls button:hover { color: #fff; border-color: #555; }
  .post-body { flex: 1; min-width: 0; }
  .post-id { font-size: 11px; color: #555; margin-bottom: 4px; }
  .post-text { font-size: 14px; line-height: 1.5; margin-bottom: 8px; white-space: pre-wrap; }
  .post-meta { font-size: 12px; color: #555; margin-bottom: 8px; }
  .post-meta span { margin-right: 12px; }
  .post-actions { display: flex; gap: 6px; align-items: center; }
  .post-actions button { padding: 4px 12px; border: 1px solid #333; background: #222;
    color: #aaa; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .post-actions button:hover { border-color: #555; color: #fff; }
  .post-actions button.btn-approve { border-color: #2a5a2a; color: #6c6; }
  .post-actions button.btn-skip { border-color: #5a2a2a; color: #c66; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
    font-weight: 500; margin-right: 6px; }
  .badge-posted { background: #1a3a1a; color: #6c6; }
  .badge-approved { background: #1a2a3a; color: #6ac; }
  .badge-dropped { background: #3a1a1a; color: #c66; }
  .badge-skipped { background: #3a3a1a; color: #cc6; }
  .badge-ig { background: #2a1a3a; color: #a6c; }
  .badge-x { background: #1a2a2a; color: #6cc; }
  .toast { position: fixed; bottom: 20px; right: 20px; padding: 10px 20px; background: #2a2a2a;
    border: 1px solid #444; border-radius: 6px; font-size: 13px; display: none; z-index: 100; }

  /* Lightbox */
  .lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 200;
    justify-content: center; align-items: center; cursor: zoom-out; }
  .lightbox.open { display: flex; }
  .lightbox img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: 4px; }
  .lightbox .lb-nav { position: absolute; top: 50%; transform: translateY(-50%); font-size: 36px;
    color: #888; cursor: pointer; padding: 20px; user-select: none; }
  .lightbox .lb-nav:hover { color: #fff; }
  .lightbox .lb-prev { left: 10px; }
  .lightbox .lb-next { right: 10px; }
  .lightbox .lb-info { position: absolute; bottom: 20px; color: #888; font-size: 13px; }
  .lightbox .lb-select { position: absolute; top: 20px; right: 20px; padding: 8px 16px;
    background: #2a2a2a; border: 1px solid #555; color: #6ac; border-radius: 4px; cursor: pointer;
    font-size: 13px; }
  .lightbox .lb-select:hover { color: #fff; border-color: #6ac; }
</style>
</head>
<body>
<h1>@tatamispaces — post manager</h1>
<div class="filters">
  <button class="active" onclick="filter('all')">All (POST_COUNT_ALL)</button>
  <button onclick="filter('approved')">Ready (POST_COUNT_APPROVED)</button>
  <button onclick="filter('posted')">Posted (POST_COUNT_POSTED)</button>
  <button onclick="filter('dropped')">Dropped (POST_COUNT_DROPPED)</button>
</div>
<div id="posts">POSTS_HTML</div>
<div class="toast" id="toast"></div>

<!-- Lightbox overlay -->
<div class="lightbox" id="lightbox" onclick="closeLightbox(event)">
  <span class="lb-nav lb-prev" onclick="lbNav(event,-1)">&lsaquo;</span>
  <img id="lb-img" src="" onclick="event.stopPropagation()">
  <span class="lb-nav lb-next" onclick="lbNav(event,1)">&rsaquo;</span>
  <div class="lb-info" id="lb-info"></div>
  <button class="lb-select" id="lb-select" onclick="lbSelect(event)">Use this image only</button>
</div>

<script>
let lbImages = [];
let lbIndex = 0;
let lbPostId = null;

function filter(status) {
  document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.post').forEach(p => {
    if (status === 'all') { p.style.display = ''; return; }
    p.style.display = p.dataset.status === status ? '' : 'none';
  });
}

function openLightbox(postId, urls, startIdx) {
  lbImages = urls;
  lbIndex = startIdx;
  lbPostId = postId;
  showLbImage();
  document.getElementById('lightbox').classList.add('open');
}

function closeLightbox(e) {
  if (e.target === document.getElementById('lightbox')) {
    document.getElementById('lightbox').classList.remove('open');
  }
}

function lbNav(e, dir) {
  e.stopPropagation();
  lbIndex = (lbIndex + dir + lbImages.length) % lbImages.length;
  showLbImage();
}

function showLbImage() {
  document.getElementById('lb-img').src = lbImages[lbIndex];
  document.getElementById('lb-info').textContent = 'Image ' + (lbIndex+1) + ' of ' + lbImages.length;
}

async function lbSelect(e) {
  e.stopPropagation();
  const r = await fetch('/api/image-select', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: lbPostId, image_index: lbIndex})
  });
  const data = await r.json();
  if (data.ok) {
    showToast('#' + lbPostId + ' → image ' + (lbIndex+1) + ' of ' + lbImages.length);
    document.getElementById('lightbox').classList.remove('open');
    setTimeout(() => location.reload(), 500);
  }
}

async function selectImage(id, idx, total) {
  const r = await fetch('/api/image-select', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, image_index: idx})
  });
  const data = await r.json();
  if (data.ok) {
    showToast('#' + id + ' → image ' + (idx+1) + ' of ' + total);
    setTimeout(() => location.reload(), 500);
  }
}

async function resetImages(id) {
  const r = await fetch('/api/image-select', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, image_index: null, image_count: null})
  });
  const data = await r.json();
  if (data.ok) {
    showToast('#' + id + ' → all images');
    setTimeout(() => location.reload(), 500);
  }
}

async function setStatus(id, status) {
  const r = await fetch('/api/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, status: status})
  });
  const data = await r.json();
  if (data.ok) {
    showToast('#' + id + ' → ' + status);
    setTimeout(() => location.reload(), 500);
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2000);
}

// Keyboard nav for lightbox
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox').classList.contains('open')) return;
  if (e.key === 'Escape') document.getElementById('lightbox').classList.remove('open');
  if (e.key === 'ArrowLeft') { lbIndex = (lbIndex - 1 + lbImages.length) % lbImages.length; showLbImage(); }
  if (e.key === 'ArrowRight') { lbIndex = (lbIndex + 1) % lbImages.length; showLbImage(); }
});
</script>
</body>
</html>"""


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
    badge_class = f"badge-{status}" if status in ("posted", "approved", "dropped") else "badge-skipped"
    if status.startswith("skipped"):
        badges += f'<span class="badge badge-skipped">skipped</span>'
    else:
        badges += f'<span class="badge {badge_class}">{status}</span>'

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
    if status == "approved":
        actions = f'<button class="btn-skip" onclick="setStatus({pid},\'dropped\')">Skip</button>'
    elif status == "dropped" or status.startswith("skipped"):
        actions = f'<button class="btn-approve" onclick="setStatus({pid},\'approved\')">Approve</button>'

    css_class = "posted" if status == "posted" else ("dropped" if status == "dropped" or status.startswith("skipped") else "")

    return f'''<div class="post {css_class}" data-status="{status}" data-id="{pid}">
      <div class="post-images">{imgs_html}</div>
      <div class="post-body">
        <div class="post-id">#{pid} {badges}</div>
        <div class="post-text">{text}</div>
        <div class="post-meta">{meta}</div>
        <div class="post-actions">{actions}</div>
      </div>
    </div>'''


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.serve_dashboard()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        if self.path == "/api/status":
            post_id = body.get("id")
            new_status = body.get("status")

            if not post_id or not new_status:
                self.send_json({"ok": False, "error": "missing id or status"})
                return

            data = load_posts()
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
                save_posts(data)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "post not found"})

        elif self.path == "/api/image-select":
            post_id = body.get("id")
            if not post_id:
                self.send_json({"ok": False, "error": "missing id"})
                return

            data = load_posts()
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
                save_posts(data)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "post not found"})

        else:
            self.send_error(404)

    def serve_dashboard(self):
        data = load_posts()
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

        html = HTML_TEMPLATE
        html = html.replace("POSTS_HTML", posts_html)
        html = html.replace("POST_COUNT_ALL", str(counts["all"]))
        html = html.replace("POST_COUNT_APPROVED", str(counts["approved"]))
        html = html.replace("POST_COUNT_POSTED", str(counts["posted"]))
        html = html.replace("POST_COUNT_DROPPED", str(counts["dropped"]))

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
