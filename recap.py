from seleniumbase import SB
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from urllib.parse import urljoin, urlparse
import io
import base64
import requests
import os
import time
import subprocess
import urllib.request
import re
import numpy as np
import soundfile as sf
import json
import concurrent.futures
import shutil
import math
import threading
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
# Pull from environment variables passed by GitHub Actions
START_URL = os.environ.get("START_URL", "https://vortexscans.org/series/the-saint-levels-up-through-necromancy/chapter-1").strip()
VOICE_MODEL = "am_adam"  
AUDIO_SPEED = 1.1    # Natively render audio/video at 1.1x so we don't have to re-encode during the final stitch!

# Handle "all" or specific number of chapters
max_chap_env = os.environ.get("MAX_CHAPTERS", "1").strip().lower()
if max_chap_env == "all":
    MAX_CHAPTERS_TO_PROCESS = float('inf')
else:
    try:
        MAX_CHAPTERS_TO_PROCESS = int(max_chap_env)
    except ValueError:
        MAX_CHAPTERS_TO_PROCESS = 1 

url_parts =[p for p in START_URL.split('/') if p]
MANGA_NAME = url_parts[-2] if len(url_parts) >= 2 else "manga"

# --- API KEY EXTRACTION ---
# Keys are read once, kept in memory only, and stripped from the environment before
# any render/subprocess runs. They never appear in a URL or in log output.
GEMINI_API_KEYS = []
_collected = []
_pattern = re.compile(r"GEMINI_API_KEY_(\d+)")

def _valid_key(value):
    value = (value or "").strip()
    return value if value and "YOUR_API_KEY" not in value else None

# Preferred: one secret holding a JSON array, e.g. GEMINI_API_KEYS=["k1","k2"]
_raw_list = os.environ.get("GEMINI_API_KEYS", "")
if _raw_list.strip():
    try:
        _parsed = json.loads(_raw_list)
        if isinstance(_parsed, list):
            for _i, _value in enumerate(_parsed):
                _key = _valid_key(_value)
                if _key:
                    _collected.append((_i, _key))
        else:
            print("[!] GEMINI_API_KEYS is JSON but not an array, ignoring it.")
    except json.JSONDecodeError:
        print("[!] GEMINI_API_KEYS is not valid JSON, ignoring it.")

# Individually named environment variables: GEMINI_API_KEY_1, GEMINI_API_KEY_2, ...
for _name, _value in os.environ.items():
    _match = _pattern.fullmatch(_name)
    if _match:
        _key = _valid_key(_value)
        if _key:
            _collected.append((int(_match.group(1)), _key))

# Deprecated: every repository secret handed over as one JSON blob. Read, then drop.
_all_secrets_raw = os.environ.pop("ALL_SECRETS", "")
if _all_secrets_raw:
    try:
        for _name, _value in json.loads(_all_secrets_raw).items():
            _match = _pattern.fullmatch(_name)
            if _match:
                _key = _valid_key(_value)
                if _key:
                    _collected.append((int(_match.group(1)), _key))
    except json.JSONDecodeError:
        pass

_seen = set()
for _order, _key in sorted(_collected, key=lambda x: x[0]):
    if _key not in _seen:
        _seen.add(_key)
        GEMINI_API_KEYS.append(_key)

if not GEMINI_API_KEYS:
    raise SystemExit("[!] No Gemini API keys found. Set GEMINI_API_KEYS (JSON array) or GEMINI_API_KEY_1..N.")

# Scrub secrets so third-party imports, Selenium and ffmpeg subprocesses cannot see them.
for _name in [n for n in os.environ if n.startswith("GEMINI_API_KEY") or n == "ALL_SECRETS"]:
    os.environ.pop(_name, None)
CHILD_ENV = dict(os.environ)

GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-flash-latest").strip()
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_TIMEOUT = 180
print(f"[i] Loaded {len(GEMINI_API_KEYS)} Gemini API key(s); model={GEMINI_MODEL}")

def describe_error(exc):
    """Type-only error description: requests embeds the request URL (and API key) in str(exc)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__

os.makedirs("videos", exist_ok=True)

font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

# Referer must match the site being scraped; a stale hardcoded one gets image
# requests blocked and strips silently dropped.
_start = urlparse(START_URL)
headers = {"Referer": f"{_start.scheme}://{_start.netloc}/", "User-Agent": "Mozilla/5.0"}


def concat_path(path):
    """ffmpeg concat demuxer entry: single-quoted, literal quotes escaped.
    Series names with apostrophes break the list otherwise."""
    return "'" + os.path.abspath(path).replace(chr(92), "/").replace("'", "'\\''") + "'"


# ================= RENDER VALIDATION =================
# A chapter only counts as finished when the MP4 actually probes as playable.
# Without this, a crashed ffmpeg leaves a 0-byte video.mp4 that every later run
# trusts and skips, silently poisoning the stitched recap.
FFPROBE = shutil.which("ffprobe") or "ffprobe"
MIN_CHAPTER_SECONDS = 5.0
MARKER_NAME = "done.json"
RENDER_VERSION = 1
FFMPEG_WORKERS = max(1, min(2, os.cpu_count() or 2))
FFMPEG_THREADS = max(1, min(2, (os.cpu_count() or 2) // FFMPEG_WORKERS))
_kokoro = None
kokoro_lock = threading.Lock()


def get_kokoro():
    global _kokoro
    if _kokoro is None:
        _kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    return _kokoro


class ChapterError(RuntimeError):
    """Raised for a chapter that must be retried: render failed or produced an invalid file."""


def _run(cmd, **kwargs):
    """subprocess.run with secrets-free env, captured output, and a hard timeout."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 7200)
    return subprocess.run(cmd, env=CHILD_ENV, **kwargs)


def probe_duration(path):
    """Duration in seconds, or 0.0 if the file is missing, empty, or unplayable."""
    if not path or not os.path.exists(path) or os.path.getsize(path) < 1024:
        return 0.0
    try:
        proc = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", path], timeout=120)
        return float((proc.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def marker_path(chap_dir):
    return os.path.join(chap_dir, MARKER_NAME)


def write_marker(chap_dir, chapter_url, chap_num, video_path):
    """Record a validated render. Also makes git the source of truth for progress,
    since the MP4s themselves are no longer committed."""
    duration = probe_duration(video_path)
    record = {
        "chapter": str(chap_num),
        "url": chapter_url,
        "duration": round(duration, 2),
        "bytes": os.path.getsize(video_path),
        "render_version": RENDER_VERSION,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(marker_path(chap_dir), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    return duration


def is_chapter_done(chap_dir, video_path, chap_num, chapter_url):
    """True only if the MP4 is present and playable. Backfills the marker for
    pre-marker renders so existing work is not thrown away."""
    if probe_duration(video_path) < MIN_CHAPTER_SECONDS:
        return False
    marker = marker_path(chap_dir)
    if os.path.exists(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                record = json.load(f)
            return int(record.get("render_version", 0)) == RENDER_VERSION
        except (OSError, ValueError):
            return False
    write_marker(chap_dir, chapter_url, chap_num, video_path)
    return True


def extract_next_url(sb, retries=3):
    """Bulletproof Python-side extraction with retries for late-rendering React DOMs."""
    current_url = sb.get_current_url()
    
    for attempt in range(retries):
        sb.sleep(2) # Allow framework rendering
        try:
            links = sb.find_elements("css selector", "a")
            for link in links:
                try:
                    text = (link.text or "").strip().lower()
                    aria = (link.get_attribute("aria-label") or "").strip().lower()
                    cls = (link.get_attribute("class") or "").lower()
                    classes = cls.split()
                    
                    if text == "next" or aria == "next" or "next_page" in classes or "next chapter" in text:
                        
                        # Strictly check for disabled buttons or "pointer-events-none" indicating end-of-series
                        if "pointer-events-none" in classes or link.get_attribute("disabled"):
                            continue
                            
                        href = link.get_attribute("href")
                        if href and len(href) > 2 and "javascript" not in href:
                            return urljoin(current_url, href)
                except:
                    continue
        except:
            pass
            
    return None

def process_chapter(chapter_url):
    print(f"\n{'='*50}\n[STARTING] {chapter_url}\n{'='*50}")
    url_parts =[p for p in chapter_url.split('/') if p]
    chapter_str = url_parts[-1] 
    chap_num_match = re.search(r'\d+', chapter_str)
    chap_num = chap_num_match.group(0) if chap_num_match else chapter_str

    base_dir = MANGA_NAME
    cast_dir = os.path.join(base_dir, "cast")
    chapters_dir = os.path.join(base_dir, "chapters")
    current_chap_dir = os.path.join(chapters_dir, chap_num)
    temp_dir = os.path.join(base_dir, "temp")

    for d in[cast_dir, chapters_dir, current_chap_dir, temp_dir]:
        os.makedirs(d, exist_ok=True)

    char_file = os.path.join(cast_dir, "characters.txt")
    context_file = os.path.join(cast_dir, "last_chapter_context.txt")
    highest_file = os.path.join(chapters_dir, "highest.txt")
    final_path = os.path.join(current_chap_dir, "video.mp4")

    if is_chapter_done(current_chap_dir, final_path, chap_num, chapter_url):
        print(f"[{chap_num}] Video already rendered and playable ({probe_duration(final_path):.1f}s). Skipping...")
        next_url = None
        with SB(uc=True, xvfb=True, locale_code="en", page_load_strategy="eager") as sb:
            sb.uc_open_with_reconnect(chapter_url, reconnect_time=4)
            try: sb.uc_gui_click_captcha()
            except: pass
            
            # Robust check for paywalls
            try:
                page_text = sb.get_text("body").lower()
                if "locked chapter" in page_text or "login to unlock" in page_text:
                    print(f"[{chap_num}] Chapter is locked (Paywall detected). Stopping here.")
                    return None, True
            except: pass
            
            next_url = extract_next_url(sb)
            
            if next_url: print(f"[{chap_num}] Next Chapter Found: {next_url}")
            else: print(f"[{chap_num}] Reached end of series or paywall.")
                
        return next_url, True  

    # Load Existing Character Lore
    existing_chars_text = ""
    if os.path.exists(char_file):
        with open(char_file, "r", encoding="utf-8") as f:
            existing_chars_text = f.read()
            
    # Load Previous Chapter Context
    previous_context = ""
    if os.path.exists(context_file):
        with open(context_file, "r", encoding="utf-8") as f:
            previous_context = f.read()

    print(f"[{chap_num}] Scraping Images...")
    image_urls, site_cookies, next_url =[], {}, None
    with SB(uc=True, xvfb=True, locale_code="en", page_load_strategy="eager") as sb:
        sb.uc_open_with_reconnect(chapter_url, reconnect_time=4)
        try: sb.uc_gui_click_captcha()
        except: pass
        
        # Robust check for paywalls
        try:
            page_text = sb.get_text("body").lower()
            if "locked chapter" in page_text or "login to unlock" in page_text:
                print(f"[{chap_num}] Chapter is locked (Paywall detected). Stopping here.")
                return None, True
        except: pass
            
        # Support for both sites' image selectors
        if sb.is_element_present(".wp-manga-chapter-img"):
            sb.wait_for_element(".wp-manga-chapter-img", timeout=15)
            images = sb.find_elements("css selector", ".wp-manga-chapter-img")
        elif sb.is_element_present("img[data-reader-page-image]"):
            sb.wait_for_element("img[data-reader-page-image]", timeout=15)
            images = sb.find_elements("css selector", "img[data-reader-page-image]")
        else:
            images =[]
            
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5) 
        
        for img in images:
            src = img.get_attribute("data-src") or img.get_attribute("src")
            if src and "http" in src: image_urls.append(src.strip())
        for cookie in sb.driver.get_cookies():
            site_cookies[cookie['name']] = cookie['value']
            
        next_url = extract_next_url(sb)
        if next_url: print(f"[{chap_num}] Next Chapter Found: {next_url}")
        else: print(f"[{chap_num}] Reached end of series or paywall.")

    print(f"[{chap_num}] Processing Strips...")
    parts, original_files, ai_heights =[], {}, {}

    def process_image(idx, img_url):
        try:
            img_response = requests.get(img_url, headers=headers, cookies=site_cookies, timeout=15)
            image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
            raw_path = os.path.join(temp_dir, f"strip_{idx}.jpg")
            image.save(raw_path, format="JPEG", quality=95)
            ai_img = image.copy()
            ai_img.thumbnail((800, 40000), Image.Resampling.BILINEAR) 
            ai_w, ai_h = ai_img.size
            draw = ImageDraw.Draw(ai_img, 'RGBA')
            draw.rectangle([(0, 0), (70, ai_h)], fill=(0, 0, 0, 220))
            for y in range(0, ai_h, 100):
                draw.line([(0, y), (35, y)], fill=(255, 255, 255, 255), width=4)
                draw.text((40, y - 15), str(y // 10), fill=(255, 255, 0, 255), font=font)
            buffered = io.BytesIO()
            ai_img.save(buffered, format="JPEG", quality=60)
            b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            return (idx, raw_path, ai_h, {"text": f"Image Index: {idx}"}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        except Exception as e:
            # A dropped strip is a page the narration will silently skip over.
            print(f"[{chap_num}] Strip {idx} failed ({describe_error(e)}): {img_url}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        results =[r for r in executor.map(lambda p: process_image(*p), enumerate(image_urls)) if r]
    results.sort(key=lambda x: x[0])
    for idx, path, h, t, i in results:
        original_files[idx], ai_heights[idx] = path, h
        parts.extend([t, i])

    if not image_urls:
        raise ChapterError("found no chapter images on the page (selector or layout problem)")
    dropped = len(image_urls) - len(results)
    if dropped:
        print(f"[{chap_num}] WARNING: {dropped}/{len(image_urls)} strip(s) failed to download; those pages are missing from the recap.")
    if dropped > len(image_urls) * 0.2:
        raise ChapterError(f"{dropped}/{len(image_urls)} strips failed to download; refusing to narrate a broken chapter")

    print(f"[{chap_num}] AI Writing Script...")
    
    prompt = (
        "Act as a professional Manhwa recap scriptwriter. Follow strict high-energy rules for YouTube/TikTok.\n\n"
        "CRITICAL INSTRUCTION: DO NOT output markdown blocks (like ```json). Return pure JSON format ONLY.\n\n"
        f"EXISTING CHARACTER LORE DATABASE:\n{existing_chars_text if existing_chars_text else 'None yet.'}\n\n"
        f"PREVIOUS CHAPTER NARRATION (For story continuity):\n{previous_context if previous_context else 'None (This is the first chapter).'}\n\n"
        "NARRATION RULES:\n"
        "- Hook the viewer immediately.\n"
        "- Use dramatic pacing, active voice, and high-energy storytelling.\n"
        "- Describe character actions, emotions, and plot twists dynamically.\n"
        "- DO NOT skip any details try not to leave out any details be percise.\n\n"
        "TTS AUDIO COMPATIBILITY RULES (CRITICAL):\n"
        "- The 'narration' text MUST be a single continuous string. DO NOT use line breaks (\\n).\n"
        "- DO NOT use ellipses (...). Use a single period instead.\n"
        "- DO NOT use multiple punctuation marks together (no !!, ??, or ?!). Use only one.\n"
        "- DO NOT use asterisks, brackets, or parentheses for actions (no *gasps* or [sighs]).\n"
        "- DO NOT use quotation marks (\", \') or em-dashes (—). Keep it entirely plain text.\n\n"
        "CAMERA & MARKER RULES:\n"
        "- Look at the images provided. There are yellow numbers acting as markers along the left edge.\n"
        "- 'start_mark' and 'end_mark' MUST correspond to these yellow numbers and tightly bound the specific action being narrated to prevent excessive/awkward panning.\n"
        "- If an action spans a short vertical space, use 'zoom_in' or 'zoom_out'.\n"
        "- If an action spans a large vertical space, use 'pan_down' or 'pan_up'.\n\n"
        "JSON SCHEMA REQUIREMENT:\n"
        "Return pure JSON with 'new_characters' (only characters that are NOT in the database above) and 'panels'.\n"
        "Each item in 'new_characters' must have: 'name', 'appearance', 'role', 'personality'.\n"
        "Each item in 'panels' must have: image_index (int), start_mark (number), end_mark (number), narration (string), effect ('zoom_in', 'zoom_out', 'pan_down', 'pan_up')."
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "new_characters": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}, "appearance": {"type": "STRING"}, "role": {"type": "STRING"}, "personality": {"type": "STRING"}}, "required":["name", "appearance", "role", "personality"]}},
            "panels": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"image_index": {"type": "INTEGER"}, "start_mark": {"type": "NUMBER"}, "end_mark": {"type": "NUMBER"}, "narration": {"type": "STRING"}, "effect": {"type": "STRING", "enum":["zoom_in", "zoom_out", "pan_down", "pan_up"]}}, "required":["image_index", "start_mark", "end_mark", "narration", "effect"]}}
        }, "required":["new_characters", "panels"]
    }
    payload = {"contents":[{"parts":[{"text": prompt}] + parts}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}
    
    script_data, current_key, rejected_keys = None, 0, set()
    for attempt in range(15):
        if len(rejected_keys) >= len(GEMINI_API_KEYS):
            print(f"[{chap_num}] All {len(GEMINI_API_KEYS)} API key(s) were rejected (401/403). Giving up on this chapter.")
            break
        try:
            # Key goes in a header, never in the URL: requests embeds the URL in
            # exception messages, and these logs are public.
            res = requests.post(
                GEMINI_ENDPOINT,
                json=payload,
                timeout=GEMINI_TIMEOUT,
                headers={"x-goog-api-key": GEMINI_API_KEYS[current_key]},
            )
        except requests.exceptions.RequestException as e:
            print(f"[{chap_num}] Network error ({describe_error(e)}). Rotating key.")
            current_key = (current_key + 1) % len(GEMINI_API_KEYS)
            time.sleep(min(60, 2 * (attempt + 1)))
            continue

        if res.status_code == 200:
            try:
                candidate = res.json()['candidates'][0]
                finish = candidate.get('finishReason', '')
                raw_text = candidate['content']['parts'][0]['text']
                start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    script_data = json.loads(raw_text[start_idx:end_idx+1])
                    break
                print(f"[{chap_num}] Response contained no JSON object (finishReason={finish or 'unknown'}). Retrying.")
            except (KeyError, IndexError, TypeError, ValueError) as e:
                print(f"[{chap_num}] Unusable response ({describe_error(e)}). Retrying.")
            time.sleep(2)
        elif res.status_code == 400:
            print(f"[{chap_num}] Error 400 (Bad Request). Prompt issue. Retrying with same key...")
            time.sleep(2)
        elif res.status_code in (401, 403):
            print(f"[{chap_num}] Error {res.status_code} (key rejected). Rotating key...")
            rejected_keys.add(current_key)
            current_key = (current_key + 1) % len(GEMINI_API_KEYS)
            time.sleep(2)
        else:
            retry_after = res.headers.get("retry-after", "")
            delay = min(60, max(2, int(retry_after))) if retry_after.isdigit() else 2
            print(f"[{chap_num}] Error {res.status_code} (Rate Limit/Unavailable). Rotating key, waiting {delay}s...")
            current_key = (current_key + 1) % len(GEMINI_API_KEYS)
            time.sleep(delay)

    if not script_data:
        raise ChapterError("Gemini produced no usable script after 15 attempts")

    # Save new characters (names already on file are skipped, so a chapter retry
    # does not duplicate lore)
    if script_data.get('new_characters'):
        known_names = set(re.findall(r"^Name:\s*(.+)$", existing_chars_text, re.MULTILINE))
        with open(char_file, "a", encoding="utf-8") as f:
            for c in script_data['new_characters']:
                name = (c.get('name') or '').strip()
                if not name or name in known_names:
                    continue
                known_names.add(name)
                f.write(f"Name: {name}\nRole: {c.get('role')}\nAppearance: {c.get('appearance')}\nPersonality: {c.get('personality')}\n{'-'*20}\n")
                
    # Save the current chapter's narration to act as context for the next chapter
    if script_data.get('panels'):
        full_narration = " ".join([p.get('narration', '') for p in script_data['panels']])
        with open(context_file, "w", encoding="utf-8") as f:
            f.write(full_narration)

    print(f"[{chap_num}] Preparing Audio & Assets Concurrently...")
    kokoro = get_kokoro()
    
    def prepare_panel_assets(i, block):
        # Gemini sometimes returns indices or marks as strings; a mismatch here
        # would silently drop a panel and desync narration from artwork.
        try:
            img_idx = int(block.get('image_index'))
        except (TypeError, ValueError):
            print(f"[{chap_num}] Panel {i}: bad image_index {block.get('image_index')!r}, skipping panel.")
            return None
        img_idx = max(0, min(img_idx, max(original_files) if original_files else 0))
        if img_idx not in original_files: return None
        
        def _mark(name, default):
            try:
                return float(block.get(name, default))
            except (TypeError, ValueError):
                return float(default)
        raw = Image.open(original_files[img_idx])
        scale = raw.height / ai_heights[img_idx]
        
        top_px = max(0, int(_mark('start_mark', 0)*10*scale)-20)
        bottom_px = min(raw.height, int(_mark('end_mark', 10)*10*scale)+20)
        
        # --- SAFEGUARD FOR AI HALLUCINATIONS ---
        if top_px >= bottom_px:
            # Swap them if the AI inverted the numbers
            top_px, bottom_px = bottom_px, top_px 
            if top_px == bottom_px:
                # If they are identical, give it a tiny valid height to prevent crash
                bottom_px += 50 

        crop = raw.crop((0, top_px, raw.width, bottom_px))
        
        clean_narration = block.get('narration', '').replace('\n', ' ').replace('\r', ' ').strip()
        clean_narration = re.sub(r'["\”\“\‘\’\*_]', '', clean_narration) 
        clean_narration = re.sub(r'\s+', ' ', clean_narration).strip()
        if not clean_narration: clean_narration = "..." 
            
        with kokoro_lock: 
            try:
                samples, _ = kokoro.create(clean_narration, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
            except Exception as e:
                safe_text = re.sub(r'[^a-zA-Z0-9\s.,?!]', '', clean_narration)
                if not safe_text.strip(): safe_text = "..."
                try: samples, _ = kokoro.create(safe_text, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
                except Exception:
                    try: samples, _ = kokoro.create("...", voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
                    except Exception as e2:
                        raise ChapterError(f"TTS failed on panel {i} ({describe_error(e2)})")
        if len(samples) == 0:
            print(f"[{chap_num}] Panel {i}: TTS returned no audio; substituting silence.")
            samples = [0.0] * int(0.5 * 24000)
                
        audio_path = os.path.join(temp_dir, f"audio_{i:04d}.wav")
        sf.write(audio_path, samples, 24000)
        
        exact_dur = max(0.5, len(samples) / 24000.0)
        frames = math.ceil(exact_dur * 30)
        video_dur = frames / 30.0
        
        f_path, v_out = os.path.join(temp_dir, f"p_{i:04d}.jpg"), os.path.join(temp_dir, f"v_{i:04d}.mov") 
        aspect_ratio = crop.height / crop.width
        effect = block.get('effect', 'zoom_in').lower()
        
        common_flags =["-c:v", "libx264", "-threads", str(FFMPEG_THREADS), "-preset", "faster", "-crf", "28", "-c:a", "pcm_s16le", "-ar", "44100", "-af", "apad", "-t", str(video_dur)]
        
        target_w = 1080 
        target_h = int(target_w * aspect_ratio)

        if target_h > 1350:
            if effect not in['pan_up', 'pan_down']: effect = 'pan_down'
            bg = crop.resize((1920, target_h)).filter(ImageFilter.GaussianBlur(40))
            fg = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
            bg.paste(fg, ((1920 - target_w) // 2, 0)) 
            bg.save(f_path, quality=95, subsampling=0)
            
            if effect == 'pan_up':
                cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, 
                       "-vf", f"crop=1920:1080:0:(in_h-1080)*(1-min(1\\,t/{max(video_dur - 1/30, 1/30):.6f})),fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
            else:
                cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, 
                       "-vf", f"crop=1920:1080:0:(in_h-1080)*min(1\\,t/{max(video_dur - 1/30, 1/30):.6f}),fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
        else:
            bg = crop.resize((1920, 1080)).filter(ImageFilter.GaussianBlur(40))
            scale_f = min(1920 / crop.width, 1080 / crop.height)
            s_w, s_h = int(crop.width * scale_f), int(crop.height * scale_f)
            bg.paste(crop.resize((s_w, s_h), Image.Resampling.LANCZOS), ((1920 - s_w) // 2, (1080 - s_h) // 2))
            bg.save(f_path, quality=95, subsampling=0)

            # Jitter Fix: Slightly expanded initial scale before zoompan + lower zoom intensity limits integer-rounding jitter
            if effect == 'zoom_out': 
                cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, 
                       "-vf", f"scale=2560x1440,zoompan=z='1.15-(0.15/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1920x1080:fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
            else:
                cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, 
                       "-vf", f"scale=2560x1440,zoompan=z='1.00+(0.15/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1920x1080:fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        
        return (cmd, v_out)

    ffmpeg_tasks =[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=FFMPEG_WORKERS) as ex:
        results = ex.map(lambda p: prepare_panel_assets(p[0], p[1]), enumerate(script_data['panels']))
        for res in results:
            if res: ffmpeg_tasks.append(res)

    if not ffmpeg_tasks:
        raise ChapterError(f"no renderable panels (panels={len(script_data.get('panels') or [])}, images={len(image_urls)})")

    print(f"[{chap_num}] Rendering {len(ffmpeg_tasks)} Synced Clips...")

    def render_clip(cmd):
        proc = _run(cmd, timeout=3600)
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        return os.path.basename(cmd[-1]), proc.returncode, tail

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=FFMPEG_WORKERS) as ex:
        for out_name, rc, tail in ex.map(render_clip, [c[0] for c in ffmpeg_tasks]):
            if rc != 0:
                failures.append(f"{out_name} rc={rc} {tail}")
    if failures:
        raise ChapterError(f"{len(failures)} clip(s) failed: " + "; ".join(failures[:3]))

    list_path = os.path.join(temp_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write("\n".join([concat_path(c[1]) for c in ffmpeg_tasks]))
    
    concat = _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", final_path], timeout=10800)
    if concat.returncode != 0:
        tail = " | ".join((concat.stderr or "").strip().splitlines()[-5:])
        raise ChapterError(f"final concat failed (rc={concat.returncode}): {tail}")

    duration = write_marker(current_chap_dir, chapter_url, chap_num, final_path)
    if duration < MIN_CHAPTER_SECONDS:
        # Never leave an invalid render behind: existence alone must not mark a chapter done.
        print(f"[{chap_num}] Rendered video failed validation ({duration:.1f}s). Discarding so it retries.")
        for path in (final_path, marker_path(current_chap_dir)):
            if os.path.exists(path):
                os.remove(path)
        raise ChapterError(f"invalid render: {duration:.1f}s")

    print(f"[{chap_num}] Done: {duration:.1f}s, {os.path.getsize(final_path)//1024} KB")
    with open(highest_file, "w") as f: f.write(str(chap_num))
    shutil.rmtree(temp_dir, ignore_errors=True)
    return next_url, False

def collect_rendered_chapters(chapters_dir):
    """Chapter videos that actually play, sorted by chapter number."""
    found = []
    if not os.path.exists(chapters_dir):
        return found
    for d in os.listdir(chapters_dir):
        chap_dir = os.path.join(chapters_dir, d)
        vp = os.path.join(chap_dir, "video.mp4")
        if not os.path.isdir(chap_dir) or not os.path.exists(vp):
            continue
        if probe_duration(vp) < MIN_CHAPTER_SECONDS:
            print(f"[!] Skipping unplayable render: {vp}")
            continue
        m = re.search(r'\d+', d)
        found.append((int(m.group(0)) if m else 0, vp))
    found.sort(key=lambda x: x[0])
    return found


def stitch_all_chapters():
    print(f"\n{'='*50}\n[FINALIZING] Stitching Recap (Fast Copy)\n{'='*50}")
    chapters_dir = os.path.join(MANGA_NAME, "chapters")
    valid_chaps = collect_rendered_chapters(chapters_dir)
    if not valid_chaps: return
    list_path = os.path.join(MANGA_NAME, "full_recap_list.txt")
    with open(list_path, "w") as f:
        for num, vp in valid_chaps: f.write(concat_path(vp) + "\n")
    
    final_recap_path = os.path.join("videos", f"{MANGA_NAME}_full_recap.mp4")
    
    # Fast Stitching - Because clips are natively 1.1x speed via Kokoro, we can instantly copy them!
    concat = _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "copy", "-c:a", "copy",
        final_recap_path
    ], timeout=10800)
    if concat.returncode != 0:
        tail = " | ".join((concat.stderr or "").strip().splitlines()[-5:])
        print(f"[!] Recap stitch failed (rc={concat.returncode}): {tail}")
    else:
        print(f"[i] Recap stitched: {final_recap_path} ({probe_duration(final_recap_path):.1f}s)")
    
    if os.path.exists(list_path): os.remove(list_path)


# ================= 1.5x SPEED SHORTS GENERATOR =================
def create_shorts_teaser():
    print(f"\n{'='*50}\n[SHORT GENERATOR] Creating High-Speed YouTube Shorts Teaser\n{'='*50}")
    
    chapters_dir = os.path.join(MANGA_NAME, "chapters")
    valid_chaps = collect_rendered_chapters(chapters_dir)
    if not valid_chaps: 
        print("No chapters found to create shorts.")
        return
            
    first_chap_video = valid_chaps[0][1] 

    shorts_dir = os.path.join(MANGA_NAME, "shorts_temp")
    os.makedirs(shorts_dir, exist_ok=True)

    print("Generating Call-to-Action Audio...")
    teaser_audio_text = "Want to see what happens next? Watch the full recap on my channel!"
    kokoro = get_kokoro()
    try:
        samples, _ = kokoro.create(teaser_audio_text, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
    except:
        samples, _ = kokoro.create("Watch the full recap on my channel!", voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")

    cta_audio_path = os.path.join(shorts_dir, "cta_audio.wav")
    sf.write(cta_audio_path, samples, 24000)
    cta_dur = len(samples) / 24000.0

    print("Generating Call-to-Action Visuals...")
    cta_img_path = os.path.join(shorts_dir, "cta_img.jpg")
    img = Image.new('RGB', (1080, 1920), color=(15, 15, 15)) 
    draw = ImageDraw.Draw(img)
    large_font = ImageFont.truetype(font_path, 85)
    
    text = "Watch the Full Recap\nOn My Channel!"
    bbox = draw.textbbox((0, 0), text, font=large_font, align="center")
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((1080-w)/2, (1920-h)/2), text, font=large_font, fill=(255, 255, 255), align="center")
    img.save(cta_img_path)

    cta_video_path = os.path.join(shorts_dir, "cta_video.mp4")
    cta = _run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", cta_img_path, "-i", cta_audio_path,
        "-c:v", "libx264", "-preset", "faster", "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-t", str(cta_dur), cta_video_path
    ], timeout=1800)
    if cta.returncode != 0:
        print(f"[!] CTA render failed (rc={cta.returncode}): {' | '.join((cta.stderr or '').strip().splitlines()[-3:])}")
        shutil.rmtree(shorts_dir, ignore_errors=True)
        return

    print("Applying Speed to Chapter 1 and formatting for Shorts...")
    hook_video_path = os.path.join(shorts_dir, "hook_video.mp4")
    
    max_hook_length = 59.0 - cta_dur 
    if max_hook_length < 5.0:
        print(f"[!] CTA audio is {cta_dur:.1f}s, leaving no room for a hook. Skipping shorts.")
        shutil.rmtree(shorts_dir, ignore_errors=True)
        return

    # Because base video is natively 1.1x speed, we apply a 1.25x speedup modifier to reach ~1.375x fast pacing.
    # setpts=0.8*PTS handles the video speed. atempo=1.25 handles audio speed. They stay perfectly synced!
    # Crop away the landscape render's sidebars before placing the central comic
    # on a vertical canvas. Existing landscape renders cannot recover artwork
    # beyond this central square; direct portrait rendering would require sources.
    vf_string = "[0:v]setpts=0.8*PTS,crop=1080:1080:(iw-1080)/2:0,split=2[bgsrc][fg];[bgsrc]scale=1080:1920,boxblur=luma_radius=25:luma_power=1[bg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
    af_string = "[0:a]atempo=1.25[aout]"
    
    hook = _run([
        "ffmpeg", "-y", "-i", first_chap_video,
        "-filter_complex", f"{vf_string};{af_string}",
        "-map", "[vout]", "-map", "[aout]",
        "-t", str(max_hook_length), 
        "-c:v", "libx264", "-preset", "faster", "-crf", "28", 
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        hook_video_path
    ], timeout=3600)
    if hook.returncode != 0:
        print(f"[!] Hook render failed (rc={hook.returncode}): {' | '.join((hook.stderr or '').strip().splitlines()[-3:])}")
        shutil.rmtree(shorts_dir, ignore_errors=True)
        return

    print("Stitching Short together...")
    list_path = os.path.join(shorts_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write(concat_path(hook_video_path) + "\n")
        f.write(concat_path(cta_video_path) + "\n")

    final_short_path = os.path.join("videos", f"{MANGA_NAME}_shorts.mp4")
    short = _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "copy", "-c:a", "copy", final_short_path
    ], timeout=1800)
    if short.returncode == 0:
        print(f"[i] Short teaser generated: {final_short_path} ({probe_duration(final_short_path):.1f}s)")
    else:
        print(f"[!] Short stitch failed (rc={short.returncode}): {' | '.join((short.stderr or '').strip().splitlines()[-3:])}")
    shutil.rmtree(shorts_dir, ignore_errors=True) 


# ================= EXECUTION =================
if not os.path.exists("kokoro-v1.0.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", "kokoro-v1.0.onnx")
if not os.path.exists("voices-v1.0.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", "voices-v1.0.bin")

current_target = START_URL
processed = 0
MAX_RETRIES = 3

while current_target and processed < MAX_CHAPTERS_TO_PROCESS:
    chapter_retries = 0
    success = False
    next_target = None
    
    while chapter_retries < MAX_RETRIES:
        try:
            next_target, skipped = process_chapter(current_target)
            if not skipped: 
                processed += 1
                print(f"Processed: {processed}/{MAX_CHAPTERS_TO_PROCESS} Chapters.")
            
            # If we reached here, the chapter was successful
            success = True
            break  
            
        except Exception as e:
            chapter_retries += 1
            # ChapterError messages are ours. Anything else is sanitised to type/status,
            # because third-party exception text can carry the full request URL.
            reason = str(e) if isinstance(e, ChapterError) else describe_error(e)
            print(f"\n[!] Error processing {current_target}: {reason}")
            if chapter_retries < MAX_RETRIES:
                print(f"[!] Retrying chapter... (Attempt {chapter_retries}/{MAX_RETRIES}) in 5 seconds...")
                time.sleep(5)
            else:
                print(f"[!] Failed to process chapter after {MAX_RETRIES} attempts.")
                print(f"[!] Stopping script to save and stitch the current progress.")
                
    if not success:
        # If we exhausted all retries and still failed, break the main loop entirely
        break
        
    current_target = next_target

# Run Finalizations (This will stitch whatever was successfully downloaded before the permanent failure)
stitch_all_chapters()
create_shorts_teaser()
