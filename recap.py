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
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-1/"

# --- DYNAMIC API KEY EXTRACTION ---
GEMINI_API_KEYS = []
found_key_names =[]

pattern = re.compile(r"GEMINI_API_KEY_(\d+)")

temp_keys =[]
for key, value in os.environ.items():
    match = pattern.fullmatch(key)
    if match and value.strip() and "YOUR_API_KEY" not in value:
        index = int(match.group(1))
        temp_keys.append((index, key, value.strip()))

temp_keys.sort(key=lambda x: x[0])
for _, key_name, key_value in temp_keys:
    GEMINI_API_KEYS.append(key_value)
    found_key_names.append(key_name)

VOICE_MODEL = "am_adam"
AUDIO_SPEED = 1.25

if not GEMINI_API_KEYS:
    print("ERROR: No GEMINI API KEYS provided.")
    exit(1)

# Directories setup
os.makedirs("videos/temp", exist_ok=True)
os.makedirs("videos/raw_strips", exist_ok=True)
os.makedirs("characters", exist_ok=True)

headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}
font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

print(f"[1] Loading Manhwa...")
image_urls, site_cookies =[], {}
with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    try: sb.uc_gui_click_captcha()
    except: pass
    sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(3)
    images = sb.find_elements("css selector", ".wp-manga-chapter-img")
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src and "http" in src: image_urls.append(src.strip())
    for cookie in sb.driver.get_cookies():
        site_cookies[cookie['name']] = cookie['value']

print(f"[2] Processing {len(image_urls)} Strips...")
parts, original_files, ai_heights = [], {}, {}

def process_image(idx, img_url):
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies, timeout=15)
        image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
        raw_path = f"videos/raw_strips/strip_{idx}.jpg"
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
    results = [r for r in executor.map(lambda p: process_image(*p), enumerate(image_urls)) if r]
results.sort(key=lambda x: x[0])

for idx, path, h, t, i in results:
    original_files[idx], ai_heights[idx] = path, h
    parts.extend([t, i])

# --- AI SCRIPTING ---
prompt = "Act as a professional Manhwa recap scriptwriter. Follow strict NO MARKDOWN, highly detailed, high-energy, no-repetitive-naming rules. Return JSON with 'characters' and 'panels' (image_index, start_mark, end_mark, narration)."
payload = {"contents": [{"parts": [{"text": prompt}] + parts}], "generationConfig": {"responseMimeType": "application/json"}}

script_data = None
current_key = 0
for _ in range(15):
    res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEYS[current_key]}", json=payload)
    if res.status_code == 200:
        script_data = json.loads(res.json()['candidates'][0]['content']['parts'][0]['text'])
        break
    current_key = (current_key + 1) % len(GEMINI_API_KEYS)

# --- RENDERING ---
kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
concat_lines, ffmpeg_tasks = [], []

for i, block in enumerate(script_data['panels']):
    img_idx, s, e = block['image_index'], block['start_mark'], block['end_mark']
    raw = Image.open(original_files[img_idx])
    scale = raw.height / ai_heights[img_idx]
    crop = raw.crop((0, int(s*10*scale)-20, raw.width, int(e*10*scale)+20))
    
    # Audio
    samples, _ = kokoro.create(block['narration'], voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
    audio_path = f"videos/temp/audio_{i:04d}.wav"
    sf.write(audio_path, samples, 24000)
    dur = max(0.5, len(samples)/24000)
    frames = int(dur * 30)
    
    f_path, v_out = f"videos/temp/p_{i:04d}.jpg", f"videos/temp/v_{i:04d}.mp4"
    
    if crop.height/crop.width > 2.2: # Pan 0.3
        crop.resize((1080, int(1080*(crop.height/crop.width)))).save(f_path)
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(dur), "-i", f_path, "-i", audio_path, "-vf", f"crop=1080:1920:0:((in_h-1920)*0.3)*(t/{dur}),format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-shortest", v_out]
    else: # Zoom 1.30
        bg = crop.resize((1080, 1920)).filter(ImageFilter.GaussianBlur(35))
        s_w, s_h = int(crop.width * min(1080/crop.width, 1920/crop.height)), int(crop.height * min(1080/crop.width, 1920/crop.height))
        bg.paste(crop.resize((s_w, s_h)), ((1080-s_w)//2, (1920-s_h)//2))
        bg.save(f_path)
        zoom_inc = min(0.002, 0.3 / frames)
        cmd = ["ffmpeg", "-y", "-i", f_path, "-i", audio_path, "-vf", f"scale=2160x3840,zoompan=z='min(1.3, zoom+{zoom_inc:.6f})':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':s=1080x1920:fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-c:a", "aac", "-shortest", v_out]
    
    ffmpeg_tasks.append((cmd, v_out))

with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()-1) as ex:
    [subprocess.run(c[0], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in ffmpeg_tasks]

with open("videos/temp/vid_list.txt", "w") as f:
    f.write("\n".join([f"file '{os.path.abspath(c[1])}'" for c in ffmpeg_tasks]))

subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "videos/temp/vid_list.txt", "-c", "copy", "videos/final_recap.mp4"])
print("Done!")
