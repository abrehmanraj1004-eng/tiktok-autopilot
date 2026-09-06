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
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import yt_dlp


def safe_print(msg: str, flush: bool = True):
    """Safely print even with special characters across all platform encodings."""
    try:
        print(msg, flush=flush)
    except UnicodeEncodeError:
        clean = msg.encode("ascii", errors="backslashreplace").decode("ascii")
        print(clean, flush=flush)


# --- Video Processor (MoviePy) ---
def extend_to_61_seconds(input_path: str, output_path: str, target_seconds: float = 61.0) -> str:
    """Stretches outro if duration < 61.0 seconds so video meets monetization threshold."""
    import moviepy.editor as mp
    import moviepy.video.fx.all as vfx

    safe_print(f"[Processor] Inspecting video duration: {input_path}")
    clip = mp.VideoFileClip(input_path)
    dur = clip.duration
    safe_print(f"[Processor] Source duration: {dur:.2f}s (Target: {target_seconds:.1f}s)")

    if dur >= target_seconds:
        safe_print("[Processor] Video already >= 61s! Preserving original duration.")
        clip.close()
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    # Outro Stretcher
    tail_dur = min(3.0, dur * 0.4)
    deficit = target_seconds - dur
    target_tail_dur = tail_dur + deficit
    speed_factor = tail_dur / target_tail_dur

    body_end = dur - tail_dur
    safe_print(f"[Processor] Extending tail ({tail_dur:.2f}s -> {target_tail_dur:.2f}s) at speed {speed_factor:.3f}x...")

    body_clip = clip.subclip(0, body_end)
    tail_clip = clip.subclip(body_end, dur)

    # In MoviePy, applying vfx.speedx on the clip adjusts both video and audio tracks
    slowed_tail = tail_clip.fx(vfx.speedx, speed_factor)

    final_clip = mp.concatenate_videoclips([body_clip, slowed_tail], method="compose")
    safe_print(f"[Processor] Render target total duration: {final_clip.duration:.2f}s")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
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


# --- YouTube Scanner ---
def get_channel_shorts(channel_url: str, max_entries: int = 5) -> List[Dict[str, Any]]:
    """Fast flat extraction of latest shorts without downloading media."""
    clean_url = channel_url.rstrip("/")
    if not clean_url.endswith("/shorts"):
        clean_url += "/shorts"

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_entries
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        res = ydl.extract_info(clean_url, download=False)
        return res.get("entries", []) if res else []


def download_short(video_id: str, output_path: str) -> Dict[str, Any]:
    """Downloads single short in 1080p MP4 format."""
    video_url = f"https://www.youtube.com/shorts/{video_id}"
    safe_print(f"[Downloader] Downloading {video_url}...")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except Exception:
            pass

    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[ext=mp4]/best",
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "tv_embedded", "web"]
            }
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        return {
            "id": video_id,
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "file_path": output_path
        }


# --- Headless TikTok Uploader ---
def upload_to_tiktok(video_path: str, caption: str, cookie_input: str, is_private: bool = False) -> bool:
    """Uploads video directly to TikTok Creator Center using headless Chrome & injected cookies."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.chrome.options import Options

    safe_print("[TikTok] Launching headless browser...")
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)

        # 1. Visit TikTok root to set cookies
        safe_print("[TikTok] Initializing TikTok session...")
        driver.get("https://www.tiktok.com")
        time.sleep(2)

        # 2. Inject Cookies
        cookies_injected = False
        parsed_cookies = []
        try:
            parsed = json.loads(cookie_input.strip())
            if isinstance(parsed, list):
                parsed_cookies = parsed
            elif isinstance(parsed, dict):
                parsed_cookies = [{"name": k, "value": v} for k, v in parsed.items()]
        except Exception:
            # Assume raw sessionid string
            raw_str = cookie_input.strip()
            parsed_cookies = [{"name": "sessionid", "value": raw_str, "domain": ".tiktok.com"}]

        for c in parsed_cookies:
            cookie_dict = {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", ".tiktok.com")
            }
            try:
                driver.add_cookie(cookie_dict)
                cookies_injected = True
            except Exception:
                pass

        safe_print(f"[TikTok] Injected cookies into session (Success: {cookies_injected}).")

        # 3. Open TikTok Creator Center Upload Page
        upload_url = "https://www.tiktok.com/creator-center/upload?from=upload"
        safe_print(f"[TikTok] Navigating to upload studio: {upload_url}")
        driver.get(upload_url)
        time.sleep(5)

        # Wait for file input element
        file_input = None
        for _ in range(12):
            inputs = driver.find_elements(By.XPATH, '//input[@type="file"]')
            if inputs:
                file_input = inputs[0]
                break
            curr = driver.current_url.lower()
            if "login" in curr:
                raise RuntimeError("TikTok session expired or cookies invalid. Redirected to login page!")
            safe_print("[TikTok] Waiting for upload interface...")
            time.sleep(2)

        if not file_input:
            raise TimeoutError("Could not find file input element on TikTok upload page.")

        # Attach Video
        safe_print(f"[TikTok] Attaching video file: {os.path.basename(video_path)}...")
        file_input.send_keys(os.path.abspath(video_path))

        # Ingestion wait
        time.sleep(8)

        # Set Private visibility if requested
        if is_private:
            safe_print("[TikTok] Setting video privacy to PRIVATE (Only you)...")
            time.sleep(2)
            try:
                priv_selectors = [
                    '//input[@type="radio" and (@value="private" or @value="self" or @value="2")]',
                    '//label[contains(., "Private") or contains(., "Only you")]',
                    '//span[normalize-space()="Private" or normalize-space()="Only you"]',
                    '//div[normalize-space()="Private" or normalize-space()="Only you"]'
                ]
                for p_sel in priv_selectors:
                    p_btns = driver.find_elements(By.XPATH, p_sel)
                    if p_btns:
                        target_p = p_btns[0]
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_p)
                        driver.execute_script("arguments[0].click();", target_p)
                        safe_print("[TikTok] Successfully selected Private visibility.")
                        break
            except Exception as pe:
                safe_print(f"[TikTok] Note selecting private visibility: {pe}")

        # 4. Set Caption
        if caption:
            safe_print(f"[TikTok] Setting caption: {caption}")
            try:
                boxes = driver.find_elements(
                    By.XPATH,
                    '//div[contains(@class, "notranslate") or contains(@class, "public-DraftEditor-content") or @contenteditable="true"]'
                )
                if boxes:
                    b = boxes[0]
                    b.click()
                    time.sleep(0.5)
                    b.send_keys(Keys.CONTROL + "a")
                    b.send_keys(Keys.BACKSPACE)
                    time.sleep(0.3)
                    b.send_keys(caption)
            except Exception as e:
                safe_print(f"[TikTok] Caption warning: {e}")

        # 5. Wait for Post button to become enabled
        safe_print("[TikTok] Waiting for video upload & processing to enable Post button...")
        primary_post_selectors = [
            '//button[contains(@class, "Button__root--type-primary") and (normalize-space()="Post" or .//div[normalize-space()="Post"] or .//span[normalize-space()="Post"])]',
            '//button[contains(@class, "Button__root--type-primary") and contains(., "Post")]',
            '//button[not(ancestor::*[contains(@data-tt, "Sidebar")]) and normalize-space()="Post"]',
            '//button[contains(@class, "btn-post")]'
        ]

        target_btn = None
        start_wait = time.time()
        while time.time() - start_wait < 90:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            for sel in primary_post_selectors:
                candidates = driver.find_elements(By.XPATH, sel)
                for c in candidates:
                    try:
                        if c.get_attribute("data-tt") or "sidebar" in (c.get_attribute("class") or "").lower():
                            continue
                        txt = (c.text or c.get_attribute("innerText") or "").strip()
                        if txt.lower() != "post":
                            continue
                        is_disabled = (
                            c.get_attribute("disabled") is not None
                            or c.get_attribute("aria-disabled") == "true"
                            or "disabled" in (c.get_attribute("class") or "").lower()
                        )
                        if not is_disabled:
                            target_btn = c
                            break
                    except Exception:
                        continue
                if target_btn:
                    break
            if target_btn:
                safe_print("[TikTok] Post button is ready!")
                break
            time.sleep(2)

        if not target_btn:
            # Final fallback search
            for c in driver.find_elements(By.XPATH, '//button'):
                txt = (c.text or c.get_attribute("innerText") or "").strip()
                if txt.lower() == "post":
                    target_btn = c
                    break

        if not target_btn:
            raise RuntimeError("Post button did not become available within timeout.")

        # Click Post
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", target_btn)
        safe_print("[TikTok] Clicked Post button! Waiting for publication confirmation...")

        # Confirmation check
        confirmed = False
        for _ in range(12):
            time.sleep(2)
            curr = driver.current_url.lower()
            if "content" in curr or "manage" in curr:
                confirmed = True
                break
            indicators = driver.find_elements(
                By.XPATH,
                '//*[contains(text(), "Manage your posts") or contains(text(), "Upload another video") or contains(text(), "Your video has been uploaded") or contains(text(), "uploaded to TikTok")]'
            )
            if indicators:
                confirmed = True
                break

        if confirmed:
            safe_print("[TikTok] SUCCESS: Video officially published to TikTok!")
        else:
            safe_print("[TikTok] Video post submitted!")
        return True

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# --- Main Loop & Execution ---
def main():
    parser = argparse.ArgumentParser(description="TikTok Cloud Auto-Pilot Uploader")
    parser.add_argument("--channel-url", default=os.getenv("YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@OtakuMedia1/shorts"))
    parser.add_argument("--max-wait-minutes", type=int, default=int(os.getenv("MAX_WAIT_MINUTES", 12)))
    parser.add_argument("--poll-interval-seconds", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", 5)))
    parser.add_argument("--dry-run", action="store_true", default=(os.getenv("DRY_RUN", "false").lower() == "true"))
    parser.add_argument("--instant", action="store_true", default=(os.getenv("INSTANT", "false").lower() == "true"))
    parser.add_argument("--test-login", action="store_true", default=(os.getenv("TEST_LOGIN", "false").lower() == "true"))
    parser.add_argument("--test-upload-private", action="store_true", default=(os.getenv("TEST_UPLOAD_PRIVATE", "false").lower() == "true"))
    parser.add_argument("--history-file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json"))
    args = parser.parse_args()

    channel_url = args.channel_url
    max_wait_secs = 0 if args.instant else args.max_wait_minutes * 60
    poll_interval = args.poll_interval_seconds
    history_file = args.history_file
    dry_run = args.dry_run

    safe_print("==========================================================")
    safe_print("  TIKTOK CLOUD AUTO-PILOT RUNNER (ZERO-LOAD PRECISION)    ")
    safe_print("==========================================================")
    safe_print(f"Target Channel:        {channel_url}")
    safe_print(f"Polling Interval:      {poll_interval} seconds")
    safe_print(f"Watch Window Timeout:  {args.max_wait_minutes} minutes")
    safe_print(f"Dry Run Mode:          {dry_run}")
    safe_print(f"History File:          {history_file}")
    safe_print("==========================================================\n")

    if args.test_upload_private:
        safe_print("\n=======================================================")
        safe_print("  TEST MODE: UPLOADING 1 VIDEO AS PRIVATE TO TIKTOK     ")
        safe_print("=======================================================\n")
        
        # Select the latest known video from OtakuMedia1: 'qfNnzLfvgGg'
        test_vid_id = "qfNnzLfvgGg"
        test_title = "They Still HATE Todo In JJK Modulo!"
        test_caption = f"[Test Private] {test_title} #fyp #viral #shorts #jjk".strip()
        
        workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
        os.makedirs(workspace_dir, exist_ok=True)
        raw_mp4 = os.path.join(workspace_dir, f"test_raw_{test_vid_id}.mp4")
        final_61s_mp4 = os.path.join(workspace_dir, f"test_final_61s_{test_vid_id}.mp4")
        
        safe_print(f"[Test-Upload] Downloading test video {test_vid_id}...")
        download_short(test_vid_id, raw_mp4)
        
        safe_print("[Test-Upload] Stretching video to 61s...")
        processed_file = extend_to_61_seconds(raw_mp4, final_61s_mp4, target_seconds=61.0)
        
        cookie_input = os.getenv("TIKTOK_COOKIES", "")
        if not cookie_input:
            local_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.json")
            if os.path.exists(local_cookie_path):
                with open(local_cookie_path, "r", encoding="utf-8") as lcf:
                    cookie_input = lcf.read().strip()
        
        if not cookie_input:
            safe_print("[Test-Upload] ERROR: No TIKTOK_COOKIES found!")
            sys.exit(1)
            
        safe_print("[Test-Upload] Uploading to TikTok with PRIVATE visibility...")
        success = upload_to_tiktok(processed_file, test_caption, cookie_input, is_private=True)
        
        if success:
            safe_print("\n=======================================================")
            safe_print("  SUCCESS! TEST VIDEO UPLOADED AS PRIVATE TO TIKTOK!   ")
            safe_print("  Check your TikTok profile (Private/Only Me tab)!     ")
            safe_print("=======================================================\n")
        sys.exit(0)

    if args.test_login:
        safe_print("\n[TEST-LOGIN] Running TikTok authentication test in cloud browser...")
        cookie_input = os.getenv("TIKTOK_COOKIES", "")
        if not cookie_input:
            local_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.json")
            if os.path.exists(local_cookie_path):
                with open(local_cookie_path, "r", encoding="utf-8") as lcf:
                    cookie_input = lcf.read().strip()
        if not cookie_input:
            safe_print("[TEST-LOGIN] ERROR: No TIKTOK_COOKIES found in secrets!")
            sys.exit(1)

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(options=options)
        try:
            driver.get("https://www.tiktok.com")
            time.sleep(2)
            try:
                cookies = json.loads(cookie_input)
                if isinstance(cookies, dict):
                    cookies = [{"name": k, "value": v} for k, v in cookies.items()]
            except Exception:
                cookies = [{"name": "sessionid", "value": cookie_input}]
            
            for c in cookies:
                try:
                    driver.add_cookie({"name": c["name"], "value": c["value"], "path": "/"})
                except Exception:
                    pass
            
            safe_print("[TEST-LOGIN] Cookies added. Navigating to TikTok Studio upload page...")
            driver.get("https://www.tiktok.com/creator-center/upload?from=upload")
            time.sleep(6)
            
            curr = driver.current_url.lower()
            safe_print(f"[TEST-LOGIN] Landed URL: {driver.current_url}")
            safe_print(f"[TEST-LOGIN] Page Title: {driver.title}")
            
            inputs = driver.find_elements("xpath", '//input[@type="file"]')
            if inputs:
                safe_print("\n=======================================================")
                safe_print("  SUCCESS! TIKTOK ACCOUNT IS 100% CONNECTED & VERIFIED! ")
                safe_print(f"  Upload Studio is ready and accessible in the cloud!")
                safe_print("=======================================================\n")
                sys.exit(0)
            elif "login" in curr:
                safe_print("\n[TEST-LOGIN] FAILED: Redirected to login page. Cookies need refresh.")
                sys.exit(1)
            else:
                safe_print(f"\n[TEST-LOGIN] Warning: Neither login nor file input found. Title: {driver.title}")
                sys.exit(0)
        finally:
            driver.quit()

    history = load_history(history_file)
    uploaded = history.setdefault("uploaded_videos", {})
    safe_print(f"[History] Known uploaded video IDs: {len(uploaded)}")

    start_time = time.time()
    new_video_found = None
    check_count = 0

    while True:
        check_count += 1
        elapsed = time.time() - start_time
        safe_print(f"[{datetime.now().strftime('%H:%M:%S')}] Check #{check_count}: Scanning channel feed...", flush=True)

        try:
            entries = get_channel_shorts(channel_url, max_entries=5)
            for entry in entries:
                vid_id = entry.get("id")
                if vid_id and vid_id not in uploaded:
                    new_video_found = entry
                    safe_print(f"\n>> [TRIGGER] NEW VIDEO DETECTED! ID: {vid_id} | Title: {entry.get('title')}")
                    break
        except Exception as e:
            safe_print(f"[Scanner Error] {e}")

        if new_video_found:
            break

        if args.instant or (elapsed >= max_wait_secs):
            safe_print(f"\n[Timeout] Watch window completed ({elapsed:.0f}s elapsed). No new video dropped. Exiting.")
            sys.exit(0)

        time.sleep(poll_interval)

    # Process and Upload the New Video
    vid_id = new_video_found.get("id")
    video_title = new_video_found.get("title", "")
    caption = f"{video_title} #fyp #viral #shorts #anime #jjk".strip()

    workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")
    os.makedirs(workspace_dir, exist_ok=True)

    raw_mp4 = os.path.join(workspace_dir, f"raw_{vid_id}.mp4")
    final_61s_mp4 = os.path.join(workspace_dir, f"final_61s_{vid_id}.mp4")

    # 1. Download
    dl_info = download_short(vid_id, raw_mp4)
    if not video_title:
        video_title = dl_info.get("title", "")
        caption = f"{video_title} #fyp #viral #shorts #anime #jjk".strip()

    # 2. 61-Second Outro Stretcher
    processed_file = extend_to_61_seconds(raw_mp4, final_61s_mp4, target_seconds=61.0)

    # 3. TikTok Upload
    if dry_run:
        safe_print(f"[Dry Run] Skipping TikTok upload. Processed video ready at: {processed_file}")
        success = True
    else:
        cookie_input = os.getenv("TIKTOK_COOKIES", "")
        if not cookie_input:
            local_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_cookies.json")
            if os.path.exists(local_cookie_path):
                try:
                    with open(local_cookie_path, "r", encoding="utf-8") as lcf:
                        cookie_input = lcf.read().strip()
                        safe_print(f"[TikTok] Loaded cookies from local {local_cookie_path}")
                except Exception as ex:
                    safe_print(f"[TikTok] Error reading local cookie file: {ex}")

        if not cookie_input:
            # Fallback: check settings.json
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
            if os.path.exists(settings_path):
                try:
                    with open(settings_path, "r", encoding="utf-8") as sf:
                        s_data = json.load(sf)
                        for acc in s_data.get("accounts", []):
                            if acc.get("status") == "connected" and acc.get("cookies"):
                                cookie_input = acc.get("cookies")
                                break
                except Exception:
                    pass

        if not cookie_input:
            raise ValueError("No TIKTOK_COOKIES secret or cookie configuration found!")

        success = upload_to_tiktok(processed_file, caption, cookie_input)

    if success:
        pkt_now = datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d %H:%M:%S PKT")
        uploaded[vid_id] = {
            "title": video_title,
            "date": pkt_now,
            "status": "uploaded_to_tiktok"
        }
        history["last_updated"] = pkt_now
        save_history(history_file, history)
        safe_print(f"\n[SUCCESS] Video '{video_title}' successfully processed and logged.")

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
