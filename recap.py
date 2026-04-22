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
MAX_CHAPTERS_TO_PROCESS = 3

# --- DYNAMIC API KEY EXTRACTION ---
GEMINI_API_KEYS = []
temp_keys =[]
pattern = re.compile(r"GEMINI_API_KEY_(\d+)")
for key, value in os.environ.items():
    match = pattern.fullmatch(key)
    if match and value.strip() and "YOUR_API_KEY" not in value:
        temp_keys.append((int(match.group(1)), key, value.strip()))

temp_keys.sort(key=lambda x: x[0])
for _, _, key_value in temp_keys:
    GEMINI_API_KEYS.append(key_value)

VOICE_MODEL = "am_adam"
AUDIO_SPEED = 1.25

if not GEMINI_API_KEYS:
    print("ERROR: No GEMINI API KEYS provided.")
    exit(1)

# Ensure essential global folders exist to prevent Git errors
os.makedirs("videos", exist_ok=True)

# VERY IMPORTANT: Protect Git repo from bloat if the script crashes midway 
if not os.path.exists(".gitignore"):
    with open(".gitignore", "w") as f:
        f.write("*/temp/\n*.onnx\n*.bin\n__pycache__/\n")

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
    manga_name = url_parts[-2] 
    chapter_str = url_parts[-1] 
    chap_num_match = re.search(r'\d+', chapter_str)
    chap_num = chap_num_match.group(0) if chap_num_match else chapter_str

    # 2. Setup Dynamic Directories (Create if they don't exist)
    base_dir = manga_name
    cast_dir = os.path.join(base_dir, "cast")
    chapters_dir = os.path.join(base_dir, "chapters")
    current_chap_dir = os.path.join(chapters_dir, chap_num)
    video_out_dir = os.path.join(base_dir, "video")
    temp_dir = os.path.join(base_dir, "temp")

    for d in[cast_dir, chapters_dir, current_chap_dir, video_out_dir, temp_dir]:
        os.makedirs(d, exist_ok=True)

    char_file = os.path.join(cast_dir, "characters.txt")
    highest_file = os.path.join(chapters_dir, "highest.txt")
    
    # Video Output Paths
    final_name = f"{manga_name}_ch{chap_num}.mp4"
    final_path = os.path.join(current_chap_dir, "video.mp4")
    global_video_path = os.path.join("videos", final_name)

    # ================= RESUME CHECK =================
    # If the video already exists, just find the next chapter link and skip processing
    if os.path.exists(final_path) or os.path.exists(global_video_path):
        print(f"[{chap_num}] Video already exists! Skipping AI & Rendering to continue recap...")
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
    ffmpeg_tasks =[]

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
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.25-(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        else: 
            bg.save(f_path)
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.00+(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,setsar=1,format=yuv420p"] + common_flags + [v_out]
        
        ffmpeg_tasks.append((cmd, v_out))

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as ex:[subprocess.run(c[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in ffmpeg_tasks]

    # 8. Stitching
    print(f"[{chap_num}] Stitching...")
    list_path = os.path.join(temp_dir, "list.txt")
    with open(list_path, "w") as f:
        f.write("\n".join([f"file '{os.path.abspath(c[1]).replace(chr(92), '/')}'" for c in ffmpeg_tasks]))
    
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Copy to "videos/" global folder
    shutil.copy(final_path, global_video_path)
    
    with open(highest_file, "w") as f: f.write(str(chap_num))
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    return next_url, False # False means it was NOT skipped (it processed normally)

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
        
        # Only increment the 'processed' count if a NEW chapter was actually generated.
        if not skipped:
            processed += 1
            
    except Exception as e:
        print(f"Error: {e}")
        break

print("\nRecap process finished.")