from seleniumbase import SB
from PIL import Image, ImageDraw, ImageFont, ImageFilter
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
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
START_URL = "https://manhuaus.com/manga/infinite-mage/chapter-1/"
VOICE_MODEL = "am_adam"  
AUDIO_SPEED = 1.0    
MAX_CHAPTERS_TO_PROCESS = 30

# Extract Manga Name globally so it can be used for final stitching
url_parts =[p for p in START_URL.split('/') if p]
MANGA_NAME = url_parts[-2] if len(url_parts) >= 2 else "manga"

# --- DYNAMIC API KEY EXTRACTION ---
GEMINI_API_KEYS =[]
temp_keys =[]
pattern = re.compile(r"GEMINI_API_KEY_(\d+)")

# 1. Try to load from GitHub Actions JSON blob (ALL_SECRETS)
all_secrets_raw = os.environ.get("ALL_SECRETS")
if all_secrets_raw:
    try:
        secrets_dict = json.loads(all_secrets_raw)
        for key, value in secrets_dict.items():
            match = pattern.fullmatch(key)
            if match and value.strip() and "YOUR_API_KEY" not in value:
                temp_keys.append((int(match.group(1)), value.strip()))
    except json.JSONDecodeError:
        print("Warning: ALL_SECRETS was found but could not be parsed as JSON.")

# 2. Fallback for Local Development (Standard Environment Variables)
if not temp_keys:
    for key, value in os.environ.items():
        match = pattern.fullmatch(key)
        if match and value.strip() and "YOUR_API_KEY" not in value:
            temp_keys.append((int(match.group(1)), value.strip()))

# Sort keys to maintain strict rotation order (1, 2, 3...)
temp_keys.sort(key=lambda x: x[0])
for _, key_value in temp_keys:
    GEMINI_API_KEYS.append(key_value)
# Ensure essential global folders exist
os.makedirs("videos", exist_ok=True)

# VERY IMPORTANT: Protect Git repo from the 100MB crash limit!
# We tell Git to ALWAYS IGNORE the massive final stitched videos folder.
with open(".gitignore", "w") as f:
    f.write("*/temp/\n*.onnx\n*.bin\n__pycache__/\nvideos/\n")

# Font Setup
font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

def process_chapter(chapter_url):
    print(f"\n{'='*50}\n[STARTING] {chapter_url}\n{'='*50}")
    
    # 1. Parse URL for naming
    url_parts =[p for p in chapter_url.split('/') if p]
    chapter_str = url_parts[-1] 
    chap_num_match = re.search(r'\d+', chapter_str)
    chap_num = chap_num_match.group(0) if chap_num_match else chapter_str

    # 2. Setup Dynamic Directories (Create if they don't exist)
    base_dir = MANGA_NAME
    cast_dir = os.path.join(base_dir, "cast")
    chapters_dir = os.path.join(base_dir, "chapters")
    current_chap_dir = os.path.join(chapters_dir, chap_num)
    temp_dir = os.path.join(base_dir, "temp")

    for d in[cast_dir, chapters_dir, current_chap_dir, temp_dir]:
        os.makedirs(d, exist_ok=True)

    char_file = os.path.join(cast_dir, "characters.txt")
    highest_file = os.path.join(chapters_dir, "highest.txt")
    
    # Video Output Path (Only saved inside the chapter folder now)
    final_path = os.path.join(current_chap_dir, "video.mp4")

    # ================= RESUME CHECK =================
    if os.path.exists(final_path):
        print(f"[{chap_num}] Video already exists in chapter folder! Skipping AI & Rendering to continue recap...")
        next_url = None
        with SB(uc=True, xvfb=True, locale_code="en", page_load_strategy="eager") as sb:
            sb.uc_open_with_reconnect(chapter_url, reconnect_time=4)
            try: sb.uc_gui_click_captcha()
            except: pass
            try:
                next_btn = sb.find_element("css selector", "a.next_page")
                next_url = next_btn.get_attribute("href")
            except: 
                print(f"[{chap_num}] 🛑 'Manga Info' button detected. All caught up!")
                next_url = None
        return next_url, True  # True means it was skipped
    # ================================================

    # 3. Load existing lore
    existing_chars_text = ""
    if os.path.exists(char_file):
        with open(char_file, "r", encoding="utf-8") as f:
            existing_chars_text = f.read()

    # 4. Scrape Chapter and Next Link
    print(f"[{chap_num}] Scraping Images & Next Link...")
    image_urls, site_cookies, next_url =[], {}, None
    with SB(uc=True, xvfb=True, locale_code="en", page_load_strategy="eager") as sb:
        sb.uc_open_with_reconnect(chapter_url, reconnect_time=4)
        try: sb.uc_gui_click_captcha()
        except: pass
        
        sb.wait_for_element(".wp-manga-chapter-img", timeout=15)
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5) 
        
        images = sb.find_elements("css selector", ".wp-manga-chapter-img")
        for img in images:
            src = img.get_attribute("data-src") or img.get_attribute("src")
            if src and "http" in src: image_urls.append(src.strip())
            
        for cookie in sb.driver.get_cookies():
            site_cookies[cookie['name']] = cookie['value']
            
        try:
            next_btn = sb.find_element("css selector", "a.next_page")
            next_url = next_btn.get_attribute("href")
        except:
            print(f"[{chap_num}] 🛑 'Manga Info' button detected. All caught up! No more new panels.")
            next_url = None

    # 5. Process Strips
    print(f"[{chap_num}] Processing {len(image_urls)} Strips...")
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results =[r for r in executor.map(lambda p: process_image(*p), enumerate(image_urls)) if r]
    results.sort(key=lambda x: x[0])

    for idx, path, h, t, i in results:
        original_files[idx], ai_heights[idx] = path, h
        parts.extend([t, i])

    # 6. AI Scripting with Lore
    print(f"[{chap_num}] AI Writing Script...")
    prompt = (
        "Act as a professional Manhwa recap scriptwriter. Return pure JSON format ONLY.\n"
        f"EXISTING CHARACTER LORE:\n{existing_chars_text if existing_chars_text else 'None.'}\n\n"
        "Return JSON with 'new_characters' (list of name, appearance, role, personality) and 'panels'.\n"
        "Each 'panels' item: image_index, start_mark, end_mark, narration, effect (zoom_in, zoom_out, pan_down, pan_up)."
    )
    
    payload = {"contents":[{"parts":[{"text": prompt}] + parts}], "generationConfig": {"responseMimeType": "application/json"}}
    script_data, current_key = None, 0
    
    for _ in range(15):
        res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEYS[current_key]}", json=payload)
        if res.status_code == 200:
            try:
                raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                start_idx, end_idx = raw_text.find('{'), raw_text.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    script_data = json.loads(raw_text[start_idx:end_idx+1])
                    break 
            except: pass
        current_key = (current_key + 1) % len(GEMINI_API_KEYS)

    if not script_data: return next_url, False

    # Update Lore File
    if script_data.get('new_characters'):
        with open(char_file, "a", encoding="utf-8") as f:
            for c in script_data['new_characters']:
                f.write(f"Name: {c.get('name')}\nRole: {c.get('role')}\nAppearance: {c.get('appearance')}\nPersonality: {c.get('personality')}\n{'-'*20}\n")

    # 7. Rendering
    print(f"[{chap_num}] Rendering Synced Clips...")
    kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    ffmpeg_tasks = []

    for i, block in enumerate(script_data['panels']):
        img_idx = block.get('image_index')
        if img_idx not in original_files: continue
            
        raw = Image.open(original_files[img_idx])
        scale = raw.height / ai_heights[img_idx]
        top_px = max(0, int(block.get('start_mark', 0)*10*scale)-20)
        bottom_px = min(raw.height, int(block.get('end_mark', 10)*10*scale)+20)
        crop = raw.crop((0, top_px, raw.width, bottom_px))
        
        samples, _ = kokoro.create(block['narration'], voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        audio_path = os.path.join(temp_dir, f"audio_{i:04d}.wav")
        sf.write(audio_path, samples, 24000)
        
        exact_dur = max(0.5, len(samples) / 24000.0)
        frames = math.ceil(exact_dur * 30)
        video_dur = frames / 30.0
        
        f_path = os.path.join(temp_dir, f"p_{i:04d}.jpg")
        v_out = os.path.join(temp_dir, f"v_{i:04d}.mov") 
        
        aspect_ratio = crop.height / crop.width
        bg = crop.resize((1080, 1920)).filter(ImageFilter.GaussianBlur(35))
        s_w, s_h = int(crop.width * min(1080/crop.width, 1920/crop.height)), int(crop.height * min(1080/crop.width, 1920/crop.height))
        bg.paste(crop.resize((s_w, s_h)), ((1080-s_w)//2, (1920-s_h)//2))

        effect = block.get('effect', 'zoom_in').lower()

        common_flags =["-c:v", "libx264", "-preset", "superfast", "-c:a", "pcm_s16le", "-ar", "44100", "-af", "apad", "-t", str(video_dur)]
        
        if effect == 'pan_down' and aspect_ratio > 1.8:
            crop.resize((1080, int(1080 * aspect_ratio))).save(f_path)
            cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, "-vf", f"crop=1080:1920:0:min(in_h-1920\\,100*t),fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        elif effect == 'pan_up' and aspect_ratio > 1.8:
            crop.resize((1080, int(1080 * aspect_ratio))).save(f_path)
            cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", f_path, "-i", audio_path, "-vf", f"crop=1080:1920:0:max(0\\,(in_h-1920)-100*t),fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        elif effect == 'zoom_out':
            bg.save(f_path)
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.25-(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
        else: 
            bg.save(f_path)
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.00+(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,setsar=1,format=yuv420p"] + common_flags +[v_out]
        
        ffmpeg_tasks.append((cmd, v_out))

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as ex:
        [subprocess.run(c[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in ffmpeg_tasks]

    # 8. Stitching Chapter
    print(f"[{chap_num}] Stitching Chapter...")
    list_path = os.path.join(temp_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write("\n".join([f"file '{os.path.abspath(c[1]).replace(chr(92), '/')}'" for c in ffmpeg_tasks]))
    
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    with open(highest_file, "w") as f: f.write(str(chap_num))
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    return next_url, False # False means it was NOT skipped (it processed normally)


def stitch_all_chapters():
    """
    Looks through the manga's chapters directory, finds all individual video.mp4 files,
    sorts them chronologically, and stitches them into ONE massive recap video in the /videos/ folder.
    """
    print(f"\n{'='*50}\n[FINALIZING] Stitching Full Series Recap\n{'='*50}")
    chapters_dir = os.path.join(MANGA_NAME, "chapters")
    if not os.path.exists(chapters_dir):
        print("No chapters directory found.")
        return
        
    chap_dirs =[d for d in os.listdir(chapters_dir) if os.path.isdir(os.path.join(chapters_dir, d))]
    valid_chaps =[]
    
    for d in chap_dirs:
        vid_path = os.path.join(chapters_dir, d, "video.mp4")
        if os.path.exists(vid_path):
            # Extract number so chapter 2 comes before chapter 10
            num_match = re.search(r'\d+', d)
            num = int(num_match.group(0)) if num_match else 0
            valid_chaps.append((num, vid_path))
            
    # Sort by chapter number
    valid_chaps.sort(key=lambda x: x[0])
    
    if not valid_chaps:
        print("No chapter videos found to stitch.")
        return
        
    list_path = os.path.join(MANGA_NAME, "full_recap_list.txt")
    with open(list_path, "w") as f:
        for num, vp in valid_chaps:
            # Absolute path with safe slashes for ffmpeg concat
            f.write(f"file '{os.path.abspath(vp).replace(chr(92), '/')}'\n")
            
    final_recap_name = f"{MANGA_NAME}_full_recap.mp4"
    final_recap_path = os.path.join("videos", final_recap_name)
    
    # Fast Concat without re-encoding (-c copy)
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", final_recap_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print(f"SUCCESS: Stitched {len(valid_chaps)} chapters into {final_recap_path}")
    if os.path.exists(list_path):
        os.remove(list_path)

# ================= MAIN LOOP =================
if not os.path.exists("kokoro-v1.0.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", "kokoro-v1.0.onnx")
if not os.path.exists("voices-v1.0.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", "voices-v1.0.bin")

current_target = START_URL
processed = 0

while current_target and processed < MAX_CHAPTERS_TO_PROCESS:
    try:
        current_target, skipped = process_chapter(current_target)
        
        if not skipped:
            processed += 1
            
    except Exception as e:
        print(f"Error: {e}")
        break

# Once the loop breaks (hit Max Chapters or Manga Info caught up)
# Gather all chapters ever generated and stitch them into the main /videos/ folder
stitch_all_chapters()

print("\nRecap process finished.")
