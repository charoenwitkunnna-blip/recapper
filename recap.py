from seleniumbase import SB
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from urllib.parse import urljoin
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

# --- DYNAMIC API KEY EXTRACTION ---
GEMINI_API_KEYS =[]
temp_keys =[]
pattern = re.compile(r"GEMINI_API_KEY_(\d+)")

all_secrets_raw = os.environ.get("ALL_SECRETS")
if all_secrets_raw:
    try:
        secrets_dict = json.loads(all_secrets_raw)
        for key, value in secrets_dict.items():
            match = pattern.fullmatch(key)
            if match and value.strip() and "YOUR_API_KEY" not in value:
                temp_keys.append((int(match.group(1)), value.strip()))
    except json.JSONDecodeError: pass

if not temp_keys:
    for key, value in os.environ.items():
        match = pattern.fullmatch(key)
        if match and value.strip() and "YOUR_API_KEY" not in value:
            temp_keys.append((int(match.group(1)), value.strip()))

temp_keys.sort(key=lambda x: x[0])
for _, key_value in temp_keys:
    GEMINI_API_KEYS.append(key_value)
    
os.makedirs("videos", exist_ok=True)

with open(".gitignore", "w") as f:
    f.write("*/temp/\n*.onnx\n*.bin\n__pycache__/\nvideos/\n")

font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

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

    if os.path.exists(final_path):
        print(f"[{chap_num}] Video already exists! Skipping...")
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
        except: return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        results =[r for r in executor.map(lambda p: process_image(*p), enumerate(image_urls)) if r]
    results.sort(key=lambda x: x[0])
    for idx, path, h, t, i in results:
        original_files[idx], ai_heights[idx] = path, h
        parts.extend([t, i])

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
    
    script_data, current_key = None, 0
    for _ in range(15):
        try:
            res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEYS[current_key]}", json=payload)
            if res.status_code == 200:
                try:
                    raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                    start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}')
                    if start_idx != -1 and end_idx != -1:
                        script_data = json.loads(raw_text[start_idx:end_idx+1])
                        break 
                except: pass
                time.sleep(2) 
            elif res.status_code == 400:
                print(f"[{chap_num}] Error 400 (Bad Request). Prompt issue. Retrying with same key...")
                time.sleep(2)
            else:
                print(f"[{chap_num}] Error {res.status_code} (Rate Limit/Unavailable). Switching API key...")
                current_key = (current_key + 1) % len(GEMINI_API_KEYS)
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            print(f"[{chap_num}] Network Error: {e}. Switching API key...")
            current_key = (current_key + 1) % len(GEMINI_API_KEYS)
            time.sleep(2)

    if not script_data: return next_url, False

    # Save new characters
    if script_data.get('new_characters'):
        with open(char_file, "a", encoding="utf-8") as f:
            for c in script_data['new_characters']:
                f.write(f"Name: {c.get('name')}\nRole: {c.get('role')}\nAppearance: {c.get('appearance')}\nPersonality: {c.get('personality')}\n{'-'*20}\n")
                
    # Save the current chapter's narration to act as context for the next chapter
    if script_data.get('panels'):
        full_narration = " ".join([p.get('narration', '') for p in script_data['panels']])
        with open(context_file, "w", encoding="utf-8") as f:
            f.write(full_narration)

    print(f"[{chap_num}] Preparing Audio & Assets Concurrently...")
    kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    kokoro_lock = threading.Lock()
    
    def prepare_panel_assets(i, block):
        img_idx = block.get('image_index')
        if img_idx not in original_files: return None
        raw = Image.open(original_files[img_idx])
        scale = raw.height / ai_heights[img_idx]
        
        top_px = max(0, int(block.get('start_mark', 0)*10*scale)-20)
        bottom_px = min(raw.height, int(block.get('end_mark', 10)*10*scale)+20)
        
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
                except: samples, _ = kokoro.create("...", voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
                
        audio_path = os.path.join(temp_dir, f"audio_{i:04d}.wav")
        sf.write(audio_path, samples, 24000)
        
        exact_dur = max(0.5, len(samples) / 24000.0)
        frames = math.ceil(exact_dur * 30)
        video_dur = frames / 30.0
        
        f_path, v_out = os.path.join(temp_dir, f"p_{i:04d}.jpg"), os.path.join(temp_dir, f"v_{i:04d}.mov") 
        aspect_ratio = crop.height / crop.width
        effect = block.get('effect', 'zoom_in').lower()
        
        common_flags =["-c:v", "libx264", "-preset", "faster", "-crf", "28", "-c:a", "pcm_s16le", "-ar", "44100", "-af", "apad", "-t", str(video_dur)]
        
        target_w = 1080 
        target_h = int(target_w * aspect_ratio)

        if target_h > 2500:
            if effect not in['pan_up', 'pan_down']: effect = 'pan_down'
            bg = crop.resize((1920, target_h)).filter(ImageFilter.GaussianBlur(40))
            fg = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
            bg.paste(fg, ((1920 - target_w) // 2, 0)) 
            bg.save(f_path)
            
            if effect == 'pan_up':
                cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, 
                       "-vf", f"crop=1920:1080:0:max(0\\,(in_h-1080)-100*t),fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
            else:
                cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, 
                       "-vf", f"crop=1920:1080:0:min(in_h-1080\\,100*t),fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
        else:
            bg = crop.resize((1920, 1080)).filter(ImageFilter.GaussianBlur(40))
            scale_f = min(1920 / crop.width, 1080 / crop.height)
            s_w, s_h = int(crop.width * scale_f), int(crop.height * scale_f)
            bg.paste(crop.resize((s_w, s_h), Image.Resampling.LANCZOS), ((1920 - s_w) // 2, (1080 - s_h) // 2))
            bg.save(f_path)

            # Jitter Fix: Slightly expanded initial scale before zoompan + lower zoom intensity limits integer-rounding jitter
            if effect == 'zoom_out': 
                cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, 
                       "-vf", f"scale=4000x2250,zoompan=z='1.15-(0.15/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1920x1080:fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
            else:
                cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, 
                       "-vf", f"scale=4000x2250,zoompan=z='1.00+(0.15/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1920x1080:fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        
        return (cmd, v_out)

    ffmpeg_tasks =[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        results = ex.map(lambda p: prepare_panel_assets(p[0], p[1]), enumerate(script_data['panels']))
        for res in results:
            if res: ffmpeg_tasks.append(res)

    print(f"[{chap_num}] Rendering Synced Clips...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:[subprocess.run(c[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in ffmpeg_tasks]

    list_path = os.path.join(temp_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write("\n".join([f"file '{os.path.abspath(c[1]).replace(chr(92), '/')}'" for c in ffmpeg_tasks]))
    
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(highest_file, "w") as f: f.write(str(chap_num))
    shutil.rmtree(temp_dir, ignore_errors=True)
    return next_url, False

def stitch_all_chapters():
    print(f"\n{'='*50}\n[FINALIZING] Stitching Recap (Fast Copy)\n{'='*50}")
    chapters_dir = os.path.join(MANGA_NAME, "chapters")
    if not os.path.exists(chapters_dir): return
    valid_chaps =[]
    for d in os.listdir(chapters_dir):
        vp = os.path.join(chapters_dir, d, "video.mp4")
        if os.path.exists(vp):
            m = re.search(r'\d+', d)
            valid_chaps.append((int(m.group(0)) if m else 0, vp))
    valid_chaps.sort(key=lambda x: x[0])
    if not valid_chaps: return
    list_path = os.path.join(MANGA_NAME, "full_recap_list.txt")
    with open(list_path, "w") as f:
        for num, vp in valid_chaps: f.write(f"file '{os.path.abspath(vp).replace(chr(92), '/')}'\n")
    
    final_recap_path = os.path.join("videos", f"{MANGA_NAME}_full_recap.mp4")
    
    # Fast Stitching - Because clips are natively 1.1x speed via Kokoro, we can instantly copy them!
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "copy", "-c:a", "copy",
        final_recap_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if os.path.exists(list_path): os.remove(list_path)


# ================= 1.5x SPEED SHORTS GENERATOR =================
def create_shorts_teaser():
    print(f"\n{'='*50}\n[SHORT GENERATOR] Creating High-Speed YouTube Shorts Teaser\n{'='*50}")
    
    chapters_dir = os.path.join(MANGA_NAME, "chapters")
    if not os.path.exists(chapters_dir): 
        print("No chapters found to create shorts.")
        return
        
    valid_chaps =[]
    for d in os.listdir(chapters_dir):
        vp = os.path.join(chapters_dir, d, "video.mp4")
        if os.path.exists(vp):
            m = re.search(r'\d+', d)
            valid_chaps.append((int(m.group(0)) if m else 0, vp))
            
    valid_chaps.sort(key=lambda x: x[0])
    if not valid_chaps: return
    
    first_chap_video = valid_chaps[0][1] 

    shorts_dir = os.path.join(MANGA_NAME, "shorts_temp")
    os.makedirs(shorts_dir, exist_ok=True)

    print("Generating Call-to-Action Audio...")
    teaser_audio_text = "Want to see what happens next? Watch the full recap on my channel!"
    kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
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
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", cta_img_path, "-i", cta_audio_path,
        "-c:v", "libx264", "-preset", "faster", "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
        "-pix_fmt", "yuv420p", "-t", str(cta_dur), cta_video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("Applying Speed to Chapter 1 and formatting for Shorts...")
    hook_video_path = os.path.join(shorts_dir, "hook_video.mp4")
    
    max_hook_length = 59.0 - cta_dur 

    # Because base video is natively 1.1x speed, we apply a 1.25x speedup modifier to reach ~1.375x fast pacing.
    # setpts=0.8*PTS handles the video speed. atempo=1.25 handles audio speed. They stay perfectly synced!
    vf_string = "[0:v]setpts=0.8*PTS,scale=-1:1920,crop=1080:1920,boxblur=luma_radius=25:luma_power=1[bg];[0:v]setpts=0.8*PTS,scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
    af_string = "[0:a]atempo=1.25[aout]"
    
    subprocess.run([
        "ffmpeg", "-y", "-i", first_chap_video,
        "-filter_complex", f"{vf_string};{af_string}",
        "-map", "[vout]", "-map", "[aout]",
        "-t", str(max_hook_length), 
        "-c:v", "libx264", "-preset", "faster", "-crf", "28", 
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        hook_video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("Stitching Short together...")
    list_path = os.path.join(shorts_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write(f"file '{os.path.abspath(hook_video_path).replace(chr(92), '/')}'\n")
        f.write(f"file '{os.path.abspath(cta_video_path).replace(chr(92), '/')}'\n")

    final_short_path = os.path.join("videos", f"{MANGA_NAME}_shorts.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:v", "copy", "-c:a", "copy", final_short_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"✅ Fast-Paced Short Teaser generated successfully: {final_short_path}")
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
            print(f"\n[!] Error processing {current_target}: {e}")
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