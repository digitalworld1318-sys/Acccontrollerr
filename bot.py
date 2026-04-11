from flask import Flask, jsonify, request
import requests
import time
import socket
from functools import lru_cache

app = Flask(__name__)

# -------------------------------
# Instagram Fetch Function (Cached)
# -------------------------------
@lru_cache(maxsize=1024)
def fetch_instagram_profile(username, proxy=None):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "x-ig-app-id": "936619743392459",
        "Referer": f"https://www.instagram.com/{username}/",
    }

    session = requests.Session()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    backoff = 1

    for _ in range(4):
        try:
            resp = session.get(url, headers=headers, timeout=10, proxies=proxies)

            if resp.status_code == 200:
                return resp.json()

            elif resp.status_code in (429, 403):
                time.sleep(backoff)
                backoff *= 2

            elif resp.status_code == 404:
                return {"error": "not_found", "status_code": 404}

            else:
                return {
                    "error": "http_error",
                    "status_code": resp.status_code,
                    "body": resp.text[:300],
                }

        except requests.RequestException:
            time.sleep(backoff)
            backoff *= 2

    return {"error": "request_failed"}


# -------------------------------
# API Route
# -------------------------------
@app.route("/api/insta/<username>", methods=["GET"])
def insta_info(username):
    proxy = request.args.get("proxy")

    data = fetch_instagram_profile(username, proxy)

    if not data:
        return jsonify({"error": "no_response"}), 502

    if "error" in data:
        return jsonify(data), data.get("status_code", 400)

    try:
        user = data.get("data", {}).get("user") or data.get("user")

        if not user:
            return jsonify({"error": "invalid_response", "raw": data})

        result = {
            "id": user.get("id"),
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "biography": user.get("biography"),
            "is_private": user.get("is_private"),
            "is_verified": user.get("is_verified"),
            "profile_pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
            "followers": user.get("edge_followed_by", {}).get("count") or user.get("followers_count"),
            "following": user.get("edge_follow", {}).get("count") or user.get("following_count"),
            "posts": user.get("edge_owner_to_timeline_media", {}).get("count") or user.get("media_count"),
            "recent_posts": []
        }

        media = user.get("edge_owner_to_timeline_media", {})
        edges = media.get("edges", [])

        for item in edges[:8]:
            node = item.get("node", {})

            caption = None
            cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
            if cap_edges:
                caption = cap_edges[0].get("node", {}).get("text")

            result["recent_posts"].append({
                "id": node.get("id"),
                "shortcode": node.get("shortcode"),
                "image": node.get("display_url"),
                "timestamp": node.get("taken_at_timestamp"),
                "caption": caption
            })

        return jsonify(result)

    except Exception as e:
        return jsonify({
            "error": "parse_error",
            "details": str(e)
        }), 500


# -------------------------------
# Auto Free Port Finder
# -------------------------------
def find_free_port(start=8080, end=9000):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    raise RuntimeError("No free port found")


# -------------------------------
# Run Server
# -------------------------------
if __name__ == "__main__":
    port = find_free_port()
    print(f"✅ Server running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port)
