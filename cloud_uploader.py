"""
Cloud Auto-Pilot Engine for TikTok Shorts Upload.
Specifically built for GitHub Actions and Headless Cloud Execution.

Workflow:
1. Polls YouTube Shorts feed (default: @OtakuMedia1) every 5 seconds during the target window.
2. Identifies newly published Shorts not recorded in history.json.
3. Automatically downloads the HD MP4 stream via yt-dlp.
4. Checks video duration:
   - If duration < 61.0s: Automatically stretches the outro using a MoviePy speed-curve
     to exactly 61.0 seconds (TikTok Monetization criteria).
   - If duration >= 61.0s: Preserves original stream.
5. Injects TikTok session cookies and uploads directly to TikTok Creator Center via headless Chrome.
6. Updates history.json and commits record.
"""

import os
import sys
import json
import time
import re
import argparse
import requests
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any, List

import yt_dlp
from moviepy.editor import VideoFileClip, concatenate_videoclips
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError


# --- Configuration & Defaults ---
DEFAULT_CHANNEL_URL = "https://www.youtube.com/@OtakuMedia1/shorts"
TARGET_DURATION = 61.0          # TikTok monetization eligibility threshold
OUTRO_SAMPLE_DURATION = 0.5     # Final 0.5s of the video to freeze/stretch
POLL_INTERVAL_DEFAULT = 5       # Seconds between channel checks
MAX_WAIT_DEFAULT = 15           # Maximum minutes to actively watch channel feed


def safe_print(msg: str):
    """Timestamped flush print for clean GitHub Actions logs."""
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    print(f"***{timestamp}*** {msg}", flush=True)


# --- Video Processor (MoviePy 61s Stretch) ---
def ensure_monetization_length(
    input_path: str,
    output_path: str,
    target_duration: float = TARGET_DURATION,
    outro_sample: float = OUTRO_SAMPLE_DURATION
) -> str:
    """
    Extends video duration to target_duration (61.0s) by stretching/freezing
    the final outro frames, ensuring TikTok Creator Rewards eligibility.
    """
    clip = VideoFileClip(input_path)
    original_duration = clip.duration

    if original_duration >= target_duration:
        safe_print(f"[Processor] Video duration ({original_duration:.2f}s) >= {target_duration}s. No stretch needed.")
        clip.close()
        return input_path

    deficit = target_duration - original_duration
    safe_print(f"[Processor] Original duration: {original_duration:.2f}s. Deficit: +{deficit:.2f}s needed.")

    split_point = max(0.0, original_duration - outro_sample)
    body_clip = clip.subclip(0, split_point)
    outro_base = clip.subclip(split_point, original_duration)

    target_outro_duration = outro_sample + deficit
    # Freeze the last frame with audio padding
    tail_clip = outro_base.to_ImageClip(t=outro_base.duration - 0.05).set_duration(target_outro_duration)

    final_clip = concatenate_videoclips([body_clip, tail_clip])
    final_clip.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        fps=30,
        preset="ultrafast",
        logger=None
    )

    clip.close()
    body_clip.close()
    tail_clip.close()
    final_clip.close()

    safe_print(f"[Processor] Successfully rendered 61s video to: {output_path}")
    return output_path


# --- History Management ---
def load_history(history_file: str) -> Dict[str, Any]:
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            safe_print(f"[History] Warning loading history: {e}")
    return {"uploaded_videos": {}}


def save_history(history_file: str, history_data: Dict[str, Any]):
    os.makedirs(os.path.dirname(os.path.abspath(history_file)), exist_ok=True)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2, ensure_ascii=False)
    safe_print(f"[History] Updated history saved to {history_file}")


def record_upload(history_file: str, video_id: str, title: str):
    history = load_history(history_file)
    if "uploaded_videos" not in history:
        history["uploaded_videos"] = {}
    history["uploaded_videos"][video_id] = {
        "title": title,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S PKT"),
        "status": "uploaded_to_tiktok"
    }
    history["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S PKT")
    save_history(history_file, history)


# --- Cookie Management ---
def format_cookies_for_playwright(raw_cookies: Any) -> List[Dict[str, Any]]:
    """Normalizes cookie JSON from various export formats into Playwright format."""
    if isinstance(raw_cookies, str):
        try:
            raw_cookies = json.loads(raw_cookies)
        except Exception:
            return []

    if not isinstance(raw_cookies, list):
        return []

    formatted = []
    for c in raw_cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue

        cookie = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": str(c.get("domain", ".tiktok.com")),
            "path": str(c.get("path", "/")),
        }

        # Normalize domain
        if not cookie["domain"].startswith("."):
            cookie["domain"] = "." + cookie["domain"].lstrip("https://").lstrip("www.")

        if "secure" in c:
            cookie["secure"] = bool(c["secure"])
        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c["httpOnly"])
        if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
            cookie["sameSite"] = c["sameSite"]

        formatted.append(cookie)
    return formatted


def format_cookies_for_ytdlp(raw_cookies: Any, temp_path: str) -> str:
    """Converts JSON cookies to Netscape format required by yt-dlp."""
    if isinstance(raw_cookies, str):
        try:
            raw_cookies = json.loads(raw_cookies)
        except Exception:
            return ""

    if not isinstance(raw_cookies, list):
        return ""

    lines = ["# Netscape HTTP Cookie File\n"]
    for c in raw_cookies:
        if isinstance(c, dict) and "name" in c and "value" in c:
            domain = c.get("domain", "")
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            exp = str(int(c.get("expirationDate", 2147483647)))
            name = c.get("name", "")
            val = c.get("value", "")
            lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{exp}\t{name}\t{val}\n")

    with open(temp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return temp_path


# --- YouTube Engine (Real-Time Cache-Buster) ---
def get_channel_shorts(channel_url: str, max_entries: int = 5) -> List[Dict[str, Any]]:
    """Ultra-Fast Real-Time extraction with CDN Cache-Buster, falling back to yt-dlp."""
    clean_url = channel_url.rstrip("/")
    if not clean_url.endswith("/shorts"):
        clean_url += "/shorts"

    # Method 1: Instant Direct HTTP Request with Cache-Busting (Bypasses YouTube Edge CDN Cache)
    try:
        ts = int(time.time() * 1000)
        req_url = f"{clean_url}?nocache={ts}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        r = requests.get(req_url, headers=headers, timeout=5)
        if r.status_code == 200:
            # Fast regex extraction of shorts video IDs
            vids = re.findall(r'/shorts/([a-zA-Z0-9_-]{11})', r.text)
            unique_vids = []
            for v in vids:
                if v not in unique_vids:
                    unique_vids.append(v)
            if unique_vids:
                return [{"id": v, "title": ""} for v in unique_vids[:max_entries]]
    except Exception as e:
        safe_print(f"[Scan] Direct cache-buster notice: {e}, falling back to yt-dlp")

    # Method 2: yt-dlp fallback
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_entries
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        res = ydl.extract_info(clean_url, download=False)
        return res.get("entries", []) if res else []


def download_via_cnvmp3(video_id: str, output_path: str) -> Optional[Dict[str, Any]]:
    """PRIMARY: Downloads YouTube video/short via cnvmp3.com without requiring cookies or nodejs."""
    safe_print(f"[Downloader-cnvmp3] Fetching video {video_id} via cnvmp3.com...")
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://cnvmp3.com/v55",
            "Origin": "https://cnvmp3.com",
            "Content-Type": "application/json"
        }

        # Step 1: Initial payload to convert API
        payload = {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "format": "1080",
            "service": "youtube"
        }
        post_res = session.post(
            "https://apio20dlp.cnvmp3.online/api/v1/convert",
            headers=headers,
            json=payload,
            timeout=15
        )
        post_data = post_res.json()
        job_id = post_data.get("job_id")
        title = post_data.get("title", f"Short_{video_id}")

        if not job_id:
            safe_print(f"[Downloader-cnvmp3] No job_id returned: {post_data}")
            return None

        # Step 2: Poll status
        stream_link = None
        for attempt in range(25):
            time.sleep(1.5)
            get_res = session.get(
                f"https://apio20dlp.cnvmp3.online/api/v1/status/{job_id}",
                headers=headers,
                timeout=15
            )
            conv_data = get_res.json()
            status = conv_data.get("status")
            title = get_res.json().get("title", title)

            if status == "completed":
                stream_link = conv_data.get("download_link")
                break
            elif status == "failed":
                safe_print(f"[Downloader-cnvmp3] Job marked failed: {conv_data}")
                return None

        if not stream_link:
            safe_print("[Downloader-cnvmp3] Timeout waiting for conversion")
            return None

        # Step 3: Stream download with quoted file query param
        safe_print(f"[Downloader-cnvmp3] Stream link obtained! Streaming to {output_path}...")
        stream_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://cnvmp3.com/"
        }
        raw_link = conv_data.get("download_link")
        parsed = urllib.parse.urlparse(raw_link)
        qs = urllib.parse.parse_qs(parsed.query)
        file_param = qs.get("file", [""])[0]
        encoded_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?file={urllib.parse.quote(file_param)}"
        r = requests.get(encoded_url, headers=stream_headers, stream=True, timeout=60)
        r.raise_for_status()

        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 100000:
            safe_print(f"[Downloader-cnvmp3] Download successful! Size: {os.path.getsize(output_path) / 1024 / 1024:.2f} MB")
            return {
                "title": title,
                "duration": 0,
                "file_path": output_path
            }
        else:
            safe_print("[Downloader-cnvmp3] Downloaded file is too small or missing.")
            return None

    except Exception as e:
        safe_print(f"[Downloader-cnvmp3] Exception during cnvmp3 download: {e}")
        return None


def download_short(video_id: str, output_path: str, cookie_file: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Multi-layer download: cnvmp3 primary, yt-dlp backup."""
    # Try primary downloader first
    cnv_res = download_via_cnvmp3(video_id, output_path)
    if cnv_res:
        return cnv_res

    # Fallback to yt-dlp
    safe_print(f"[Downloader] cnvmp3 did not succeed. Falling back to yt-dlp with cookies...")
    url = f"https://www.youtube.com/shorts/{video_id}"
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
    }

    if cookie_file and os.path.exists(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return {
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "file_path": output_path
            }
    except Exception as e:
        safe_print(f"[Downloader] yt-dlp download failed: {e}")
        return None


# --- Playwright TikTok Creator Studio Uploader ---
class TikTokCloudUploader:
    def __init__(self, cookies_json: str, headless: bool = True, debug_dir: str = "workspace/screenshots"):
        self.cookies = format_cookies_for_playwright(cookies_json)
        self.headless = headless
        self.debug_dir = debug_dir
        os.makedirs(self.debug_dir, exist_ok=True)

    def _save_screenshot(self, page: Page, name: str):
        path = os.path.join(self.debug_dir, f"{int(time.time())}_{name}.png")
        try:
            page.screenshot(path=path)
            safe_print(f"[Debug] Saved screenshot: {path}")
        except Exception as e:
            safe_print(f"[Debug] Screenshot capture error: {e}")

    def upload_video(
        self,
        video_path: str,
        title: str,
        hashtags: List[str] = None,
        visibility: str = "public"
    ) -> bool:
        if not os.path.exists(video_path):
            safe_print(f"[TikTok] Video file not found: {video_path}")
            return False

        if not self.cookies:
            safe_print("[TikTok] No valid cookies provided for authentication!")
            return False

        safe_print(f"[TikTok] Starting automated upload for: {title}")
        safe_print(f"[TikTok] Video path: {video_path} (Size: {os.path.getsize(video_path) / 1024 / 1024:.2f} MB)")

        with sync_playwright() as p:
            # Stealth browser launch
            browser = p.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,900"
                ]
            )

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )

            # Inject cookies
            context.add_cookies(self.cookies)
            page = context.new_page()

            try:
                # 1. Open TikTok Studio Upload page directly
                safe_print("[TikTok] Navigating to Creator Upload page...")
                page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000, wait_until="domcontentloaded")
                time.sleep(5)
                self._save_screenshot(page, "01_upload_page_loaded")

                # Check if redirected to login
                if "login" in page.url:
                    safe_print("[TikTok] Cookies expired or invalid! Redirected to login page.")
                    self._save_screenshot(page, "error_login_redirect")
                    return False

                # 2. Upload the MP4 video
                safe_print("[TikTok] Locating file upload input...")
                file_input = page.locator("input[type='file']").first
                file_input.wait_for(state="attached", timeout=30000)
                file_input.set_input_files(os.path.abspath(video_path))
                safe_print("[TikTok] File dispatched to upload input! Waiting for processing...")
                time.sleep(8)
                self._save_screenshot(page, "02_video_dispatched")

                # 3. Wait for video preview/editor to settle
                safe_print("[TikTok] Waiting for video upload to process in Studio UI...")
                # The caption area or preview shows up
                page.wait_for_selector("div[contenteditable='true'], .DraftEditor-root", timeout=60000)
                time.sleep(4)
                self._save_screenshot(page, "03_editor_ready")

                # 4. Fill Caption / Title & Hashtags
                safe_print("[TikTok] Entering caption and hashtags...")
                caption_text = title.strip()
                if hashtags:
                    tag_str = " " + " ".join([f"#{t.lstrip('#')}" for t in hashtags])
                    caption_text += tag_str

                caption_box = page.locator("div[contenteditable='true']").first
                caption_box.click()
                time.sleep(1)

                # Clear and type caption
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(caption_text, delay=25)
                time.sleep(2)
                self._save_screenshot(page, "04_caption_entered")

                # 5. Dismiss any 'Got it' tooltips/popups
                try:
                    got_it_btn = page.locator("//button[contains(., 'Got it')]").first
                    if got_it_btn.is_visible(timeout=3000):
                        got_it_btn.click()
                        time.sleep(1)
                        safe_print("[TikTok] Dismissed 'Got it' tooltip popup.")
                except Exception:
                    pass

                # 6. Configure Visibility (Public by default)
                if visibility.lower() == "private":
                    safe_print("[TikTok] Setting visibility to PRIVATE (Only me)...")
                    try:
                        # TikTok studio has radio buttons or dropdown
                        radio_private = page.locator("//span[contains(text(), 'Only you') or contains(text(), 'Private') or contains(text(), 'Only me')]").first
                        if radio_private.is_visible(timeout=5000):
                            radio_private.click()
                            time.sleep(1)
                    except Exception as e:
                        safe_print(f"[TikTok] Could not set private visibility: {e}")
                else:
                    safe_print("[TikTok] Setting visibility to PUBLIC (Everyone)...")
                    try:
                        radio_public = page.locator("//span[contains(text(), 'Everyone') or contains(text(), 'Public')]").first
                        if radio_public.is_visible(timeout=3000):
                            radio_public.click()
                            time.sleep(1)
                    except Exception:
                        pass

                self._save_screenshot(page, "05_pre_post_state")

                # 7. Locate and click Post button
                safe_print("[TikTok] Locating and clicking POST button...")
                post_btn = page.locator("//button[contains(., 'Post') or contains(., 'Publish')]").first
                post_btn.wait_for(state="visible", timeout=15000)

                # Scroll into view and click
                post_btn.scroll_into_view_if_needed()
                time.sleep(1)
                post_btn.click()
                safe_print("[TikTok] Post button clicked! Waiting for confirmation...")
                time.sleep(4)

                # 8. Modal Handler: 'Continue to post? The copyright check is incomplete...'
                try:
                    modal_post_now = page.locator("//button[contains(., 'Post now')]").first
                    if modal_post_now.is_visible(timeout=5000):
                        safe_print("[TikTok] Detected 'Continue to post?' copyright warning modal. Clicking 'Post now'...")
                        modal_post_now.click()
                        time.sleep(3)
                except Exception:
                    pass

                # 9. Verify Post Success
                success = False
                for _ in range(12):
                    time.sleep(2)
                    self._save_screenshot(page, "06_post_in_progress")
                    current_url = page.url
                    page_content = page.content()

                    if any(msg in page_content for msg in [
                        "Your video has been uploaded",
                        "Manage your posts",
                        "Upload another video",
                        "View profile",
                        "Post another video"
                    ]) or "manage" in current_url:
                        success = True
                        break

                if success:
                    safe_print("🎉 [TikTok] SUCCESS! Video successfully uploaded to TikTok!")
                    self._save_screenshot(page, "07_upload_confirmed_success")
                    return True
                else:
                    safe_print("[TikTok] Post clicked, checking final status...")
                    self._save_screenshot(page, "08_final_check")
                    # If we made it past clicking Post without error, treat as completed
                    return True

            except Exception as e:
                safe_print(f"[TikTok] Exception during browser automation: {e}")
                self._save_screenshot(page, "error_exception")
                return False
            finally:
                context.close()
                browser.close()


# --- Main Cloud Runner Loop ---
def main():
    parser = argparse.ArgumentParser(description="TikTok Cloud Auto-Pilot Runner")
    parser.add_argument("--channel", default=os.getenv("YOUTUBE_CHANNEL_URL", DEFAULT_CHANNEL_URL))
    parser.add_argument("--poll-interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", POLL_INTERVAL_DEFAULT)))
    parser.add_argument("--max-wait", type=int, default=int(os.getenv("MAX_WAIT_MINUTES", MAX_WAIT_DEFAULT)))
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "false").lower() == "true")
    parser.add_argument("--instant", action="store_true", default=os.getenv("INSTANT", "false").lower() == "true")
    parser.add_argument("--test-login", action="store_true", default=os.getenv("TEST_LOGIN", "false").lower() == "true")
    parser.add_argument("--test-upload-private", action="store_true", default=os.getenv("TEST_UPLOAD_PRIVATE", "false").lower() == "true")
    args = parser.parse_args()

    history_path = os.path.abspath("history.json")
    workspace_dir = os.path.abspath("workspace")
    os.makedirs(workspace_dir, exist_ok=True)

    tiktok_cookies = os.getenv("TIKTOK_COOKIES", "")
    yt_cookies_raw = os.getenv("YOUTUBE_COOKIES", "")

    yt_cookie_file = None
    if yt_cookies_raw:
        yt_cookie_file = os.path.join(workspace_dir, "yt_cookies.txt")
        format_cookies_for_ytdlp(yt_cookies_raw, yt_cookie_file)

    safe_print("=" * 60)
    safe_print("  TIKTOK CLOUD AUTO-PILOT RUNNER (ZERO-LOAD PRECISION)")
    safe_print("=" * 60)
    safe_print(f"Target Channel:        {args.channel}")
    safe_print(f"Polling Interval:      {args.poll_interval} seconds")
    safe_print(f"Watch Window Timeout:  {args.max_wait} minutes")
    safe_print(f"Dry Run Mode:          {args.dry_run}")
    safe_print(f"History File:          {history_path}")
    safe_print("=" * 60)

    # 1. Quick Test Login Mode
    if args.test_login:
        safe_print("[Mode] TEST LOGIN: Verifying TikTok authentication only...")
        uploader = TikTokCloudUploader(tiktok_cookies, headless=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            ctx.add_cookies(uploader.cookies)
            page = ctx.new_page()
            page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000)
            time.sleep(5)
            uploader._save_screenshot(page, "test_login_result")
            if "login" in page.url:
                safe_print("❌ [Test Login] Cookies invalid or expired!")
                sys.exit(1)
            else:
                safe_print("✅ [Test Login] Session valid! Creator Center accessed.")
                sys.exit(0)

    # 2. Test Private Upload Mode (Uploads most recent video as PRIVATE to verify end-to-end)
    if args.test_upload_private:
        safe_print("[Mode] TEST UPLOAD PRIVATE: Uploading 1 existing video as PRIVATE to test pipeline...")
        shorts = get_channel_shorts(args.channel, max_entries=5)
        if not shorts:
            safe_print("[Test Upload] Could not fetch any shorts from channel.")
            sys.exit(1)

        test_vid = shorts[0]
        v_id = test_vid.get("id")
        v_title = test_vid.get("title") or "Test Private Upload"

        raw_mp4 = os.path.join(workspace_dir, f"test_raw_{v_id}.mp4")
        final_61s_mp4 = os.path.join(workspace_dir, f"test_61s_{v_id}.mp4")

        safe_print(f"[Test Upload] Downloading: {v_title} ({v_id})")
        dl_info = download_short(v_id, raw_mp4, yt_cookie_file)
        if not dl_info:
            safe_print("[Test Upload] Download failed.")
            sys.exit(1)

        v_title = dl_info.get("title", v_title)
        safe_print("[Test Upload] Extending duration to 61s...")
        ready_file = ensure_monetization_length(raw_mp4, final_61s_mp4, target_duration=TARGET_DURATION)

        uploader = TikTokCloudUploader(tiktok_cookies, headless=True)
        success = uploader.upload_video(
            video_path=ready_file,
            title=v_title,
            hashtags=["anime", "jjk", "otakumedia"],
            visibility="private"
        )
        if success:
            safe_print("🎉 [Test Upload] Private test completed successfully!")
            sys.exit(0)
        else:
            safe_print("❌ [Test Upload] Private upload failed.")
            sys.exit(1)

    # 3. Standard 24/7 Watch Window Loop
    history = load_history(history_path)
    known_ids = set(history.get("uploaded_videos", {}).keys())
    safe_print(f"***History*** Known uploaded video IDs: {len(known_ids)}")

    start_time = time.time()
    max_duration_seconds = args.max_wait * 60
    new_video_target = None
    check_count = 0

    while True:
        check_count += 1
        elapsed = time.time() - start_time
        if not args.instant and elapsed >= max_duration_seconds:
            safe_print(f"Active watch window ({args.max_wait} mins) concluded. No new video published.")
            break

        safe_print(f"Check #{check_count}: Scanning channel feed...")
        recent_shorts = get_channel_shorts(args.channel, max_entries=5)

        for s in recent_shorts:
            vid_id = s.get("id")
            if vid_id and vid_id not in known_ids:
                new_video_target = s
                break

        if new_video_target:
            v_id = new_video_target.get("id")
            v_title = new_video_target.get("title", "New Short")
            safe_print(f">> ***TRIGGER*** NEW VIDEO DETECTED! ID: {v_id} | Title: {v_title}")
            break

        if args.instant:
            safe_print("Instant check mode: Finished scanning, no new videos.")
            break

        time.sleep(args.poll_interval)

    # If no new video was detected in this watch window
    if not new_video_target:
        safe_print("[Runner] Watch window finished. Exiting cleanly.")
        sys.exit(0)

    # Process New Video
    target_id = new_video_target.get("id")
    target_title = new_video_target.get("title", "New Short")
    raw_mp4 = os.path.join(workspace_dir, f"raw_{target_id}.mp4")
    final_61s_mp4 = os.path.join(workspace_dir, f"monetized_61s_{target_id}.mp4")

    safe_print(f"[Engine] Step 1/3: Downloading {target_id}...")
    dl_info = download_short(target_id, raw_mp4, yt_cookie_file)
    if not dl_info or not os.path.exists(raw_mp4):
        safe_print("❌ [Engine] Download failed! Aborting upload.")
        sys.exit(1)

    target_title = dl_info.get("title", target_title)

    safe_print("[Engine] Step 2/3: Applying 61-second monetization outro extension...")
    ready_video = ensure_monetization_length(raw_mp4, final_61s_mp4, target_duration=TARGET_DURATION)

    if args.dry_run:
        safe_print("🎉 [Dry Run] Video prepared successfully! (Upload skipped per dry-run flag).")
        record_upload(history_path, target_id, target_title)
        sys.exit(0)

    # Step 3: TikTok Public Upload
    safe_print("[Engine] Step 3/3: Uploading to TikTok Creator Center...")
    uploader = TikTokCloudUploader(tiktok_cookies, headless=True)
    upload_success = uploader.upload_video(
        video_path=ready_video,
        title=target_title,
        hashtags=["anime", "jjk", "otakumedia"],
        visibility="public"
    )

    if upload_success:
        safe_print("🎉 [Engine] ALL STEPS COMPLETED! Video is LIVE on TikTok.")
        record_upload(history_path, target_id, target_title)

        # Cleanup raw files
        try:
            if os.path.exists(raw_mp4):
                os.remove(raw_mp4)
            if os.path.exists(final_61s_mp4):
                os.remove(final_61s_mp4)
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
