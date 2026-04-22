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
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
START_URL = "https://manhuaus.com/manga/infinite-mage/chapter-1/"
MAX_CHAPTERS_TO_PROCESS = 5 # Set this to 100 if you want it to run all night

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

# Font Setup
font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

def process_chapter(chapter_url):
    print(f"\n{'='*50}\n[STARTING] {chapter_url}\n{'='*50}")
    
    # 1. Parse URL for naming conventions
    url_parts =[p for p in chapter_url.split('/') if p]
    manga_name = url_parts[-2] # e.g., infinite-mage
    chapter_str = url_parts[-1] # e.g., chapter-122
    chap_num_match = re.search(r'\d+', chapter_str)
    chap_num = chap_num_match.group(0) if chap_num_match else chapter_str

    # 2. Setup Dynamic Directories
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

    # 3. Load existing characters to maintain lore accuracy
    existing_chars_text = ""
    if os.path.exists(char_file):
        with open(char_file, "r", encoding="utf-8") as f:
            existing_chars_text = f.read()

    # 4. Scrape Chapter and Next Link
    print(f"[{chap_num}] Loading Manhwa (Eager Mode)...")
    image_urls, site_cookies, next_url =[], {}, None
    with SB(uc=True, xvfb=True, locale_code="en", page_load_strategy="eager") as sb:
        sb.uc_open_with_reconnect(chapter_url, reconnect_time=4)
        try: sb.uc_gui_click_captcha()
        except: pass
        
        sb.wait_for_element(".wp-manga-chapter-img", timeout=15)
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5) 
        
        # Extract Images
        images = sb.find_elements("css selector", ".wp-manga-chapter-img")
        for img in images:
            src = img.get_attribute("data-src") or img.get_attribute("src")
            if src and "http" in src: image_urls.append(src.strip())
            
        # Extract Cookies
        for cookie in sb.driver.get_cookies():
            site_cookies[cookie['name']] = cookie['value']
            
        # Find Next Chapter Button dynamically
        try:
            next_btn = sb.find_element("css selector", "a.next_page")
            next_url = next_btn.get_attribute("href")
        except:
            print(f"[{chap_num}] No Next Chapter button found. This might be the latest chapter.")

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

    # 6. Generate AI Script + Character Extraction
    print(f"[{chap_num}] Generating AI Script & Extracting Lore...")
    prompt = (
        "Act as a professional Manhwa recap scriptwriter. Follow strict high-energy rules.\n\n"
        "CRITICAL INSTRUCTION: DO NOT output markdown. Return pure JSON format ONLY.\n"
        f"EXISTING CHARACTER LORE DATABASE:\n{existing_chars_text if existing_chars_text else 'None yet.'}\n\n"
        "Return pure JSON with 'new_characters' (only characters that are NOT in the database above) and 'panels'.\n"
        "Each item in 'new_characters' must have: 'name', 'appearance', 'role', 'personality'.\n"
        "Each item in 'panels' must have: image_index (int), start_mark (int), end_mark (int), narration (string), effect ('zoom_in', 'zoom_out', 'pan_down', 'pan_up').\n"
        "CAMERA RULES: 'start_mark' and 'end_mark' MUST tightly bound the specific action being narrated to prevent excessive panning."
    )
    
    payload = {"contents":[{"parts":[{"text": prompt}] + parts}], "generationConfig": {"responseMimeType": "application/json"}}
    script_data = None
    current_key = 0
    
    for _ in range(15):
        res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEYS[current_key]}", json=payload)
        if res.status_code == 200:
            try:
                raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                start_idx = raw_text.find('{')
                end_idx = raw_text.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    clean_json = raw_text[start_idx:end_idx+1]
                    script_data = json.loads(clean_json)
                    break 
            except: pass
        current_key = (current_key + 1) % len(GEMINI_API_KEYS)

    if not script_data:
        print(f"[{chap_num}] Error: AI Script generation failed. Skipping to next.")
        return next_url

    # Save new characters to our Lore Database
    if script_data.get('new_characters'):
        with open(char_file, "a", encoding="utf-8") as f:
            for c in script_data['new_characters']:
                f.write(f"Name: {c.get('name')}\nRole: {c.get('role')}\nAppearance: {c.get('appearance')}\nPersonality: {c.get('personality')}\n{'-'*20}\n")

    # 7. Rendering & Audio Sync
    print(f"[{chap_num}] Rendering Frames & Audio Syncing...")
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
        dur_str = f"{exact_dur:.3f}"
        frames = int(exact_dur * 30)
        
        f_path = os.path.join(temp_dir, f"p_{i:04d}.jpg")
        v_out = os.path.join(temp_dir, f"v_{i:04d}.mp4")
        
        aspect_ratio = crop.height / crop.width
        bg = crop.resize((1080, 1920)).filter(ImageFilter.GaussianBlur(35))
        s_w, s_h = int(crop.width * min(1080/crop.width, 1920/crop.height)), int(crop.height * min(1080/crop.width, 1920/crop.height))
        bg.paste(crop.resize((s_w, s_h)), ((1080-s_w)//2, (1920-s_h)//2))

        effect = block.get('effect', 'zoom_in').lower()

        if effect == 'pan_down' and aspect_ratio > 1.8:
            crop.resize((1080, int(1080 * aspect_ratio))).save(f_path)
            cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-t", dur_str, "-i", f_path, "-i", audio_path, "-vf", f"crop=1080:1920:0:min(in_h-1920\\,100*t),format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-ar", "44100", "-b:a", "192k", "-map", "0:v", "-map", "1:a", "-shortest", v_out]
        elif effect == 'pan_up' and aspect_ratio > 1.8:
            crop.resize((1080, int(1080 * aspect_ratio))).save(f_path)
            cmd =["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-t", dur_str, "-i", f_path, "-i", audio_path, "-vf", f"crop=1080:1920:0:max(0\\,(in_h-1920)-100*t),format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-ar", "44100", "-b:a", "192k", "-map", "0:v", "-map", "1:a", "-shortest", v_out]
        elif effect == 'zoom_out':
            bg.save(f_path)
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.25-(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-ar", "44100", "-b:a", "192k", "-map", "0:v", "-map", "1:a", "-shortest", "-t", dur_str, v_out]
        else: 
            bg.save(f_path)
            cmd =["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='1.00+(0.25/{frames})*on':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-ar", "44100", "-b:a", "192k", "-map", "0:v", "-map", "1:a", "-shortest", "-t", dur_str, v_out]
        
        ffmpeg_tasks.append((cmd, v_out))

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as ex:
        [subprocess.run(c[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in ffmpeg_tasks]

    # 8. Stitching Chapter
    print(f"[{chap_num}] Stitching Chapter Video...")
    vid_list_path = os.path.join(temp_dir, "vid_list.txt")
    with open(vid_list_path, "w") as f:
        f.write("\n".join([f"file '{os.path.abspath(c[1]).replace(chr(92), '/')}'" for c in ffmpeg_tasks]))

    # Export strictly to the numbered chapter folder
    final_output = os.path.join(current_chap_dir, "video.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", vid_list_path, "-c", "copy", final_output], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Track the highest chapter completed
    with open(highest_file, "w") as f:
        f.write(str(chap_num))

    # Clean out the temp directory so memory/storage doesn't balloon over 10 hours
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    print(f"[{chap_num}] COMPLETE! Saved to: {final_output}")
    return next_url

# ================= MAIN LOOP =================
# Verify Kokoro Models locally first
if not os.path.exists("kokoro-v1.0.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", "kokoro-v1.0.onnx")
if not os.path.exists("voices-v1.0.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", "voices-v1.0.bin")

current_target = START_URL
chapters_processed = 0

while current_target and chapters_processed < MAX_CHAPTERS_TO_PROCESS:
    try:
        next_target = process_chapter(current_target)
        current_target = next_target
        chapters_processed += 1
    except Exception as e:
        print(f"CRITICAL ERROR on {current_target}: {e}")
        break

print("\n\nAll Requested Chapters Finished!")
