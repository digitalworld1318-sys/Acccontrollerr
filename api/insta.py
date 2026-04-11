from http.server import BaseHTTPRequestHandler
import json
import requests
import time
from functools import lru_cache

# -------------------------------
# Fetch Instagram Profile
# -------------------------------
@lru_cache(maxsize=512)
def fetch_profile(username):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10)",
        "x-ig-app-id": "936619743392459",
        "Accept": "application/json",
        "Referer": f"https://www.instagram.com/{username}/",
        "Origin": "https://www.instagram.com"
    }

    backoff = 1

    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=10)

            if r.status_code == 200:
                return r.json()

            elif r.status_code in (403, 429):
                time.sleep(backoff)
                backoff *= 2

            else:
                return {"error": "http_error", "status": r.status_code}

        except:
            time.sleep(backoff)
            backoff *= 2

    return {"error": "failed"}


# -------------------------------
# Vercel Handler
# -------------------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if not self.path.startswith("/api/insta/"):
                self.send_response(404)
                self.end_headers()
                return

            username = self.path.split("/api/insta/")[-1].split("?")[0]

            data = fetch_profile(username)

            if "error" in data:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
                return

            user = data.get("data", {}).get("user")

            if not user:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": "invalid"}).encode())
                return

            result = {
                "id": user.get("id"),
                "username": user.get("username"),
                "full_name": user.get("full_name"),
                "bio": user.get("biography"),
                "followers": user.get("edge_followed_by", {}).get("count"),
                "following": user.get("edge_follow", {}).get("count"),
                "posts": user.get("edge_owner_to_timeline_media", {}).get("count"),
                "profile_pic": user.get("profile_pic_url_hd"),
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
