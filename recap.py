from seleniumbase import SB
from PIL import Image
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
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
VOICE_MODEL = "am_adam" # Kokoro Voice (am_adam = American Male, af_bella = American Female)
AUDIO_SPEED = 1.25 # Fast-paced YouTube style

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY environment variable not set.")
    exit(1)
# =================================================

print(f"[1] Loading Manhwa URL: {CHAPTER_URL}")
image_urls, site_cookies =[], {}

# Scrape Image URLs
with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    try: sb.uc_gui_click_captcha()
    except: pass
    try: sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
    except: exit(1)

    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(3)
    images = sb.find_elements("css selector", ".wp-manga-chapter-img")
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src and "http" in src: image_urls.append(src.strip())
    for cookie in sb.driver.get_cookies():
        site_cookies[cookie['name']] = cookie['value']

if not image_urls: 
    print("Failed to find images.")
    exit(1)

os.makedirs("videos/temp", exist_ok=True)
headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}
base64_panels = []
panel_files =[]

print(f"[2] Downloading and Processing {len(image_urls)} Full Strips...")

for idx, img_url in enumerate(image_urls): 
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')

    # Prepare Base64 for Gemini AI (Full Size)
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG", quality=85)
    base64_panels.append(base64.b64encode(buffered.getvalue()).decode('utf-8'))

    # Prepare Image for FFMPEG Video
    TARGET_W, TARGET_H = 1080, 1920
    bg = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0)) 
    
    scale = min(TARGET_W / image.width, TARGET_H / image.height)
    new_w = int(image.width * scale)
    new_h = int(image.height * scale)
    video_img = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    x_offset = (TARGET_W - new_w) // 2
    y_offset = (TARGET_H - new_h) // 2
    bg.paste(video_img, (x_offset, y_offset))
    
    img_path = f"videos/temp/strip_{idx}.jpg"
    bg.save(img_path, format="JPEG", quality=90)
    panel_files.append(img_path)

print(f"[3] Sending {len(base64_panels)} Full Strips to Gemini Flash Latest...")

prompt_text = """You are a professional scriptwriter for a highly successful YouTube Manhwa/Manga recap channel. Output ONLY plain text recap script. NO MARKDOWN. Be extremely detailed. Use high-energy language. Do not say 'our MC' more than once. Use fluid transitions. No fourth wall breaks."""

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
parts = [{"text": prompt_text}]
for b64 in base64_panels:
    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
payload = {"contents": [{"parts": parts}]}

# --- RETRY LOGIC WITH EXPONENTIAL BACKOFF ---
max_retries = 3
retry_delay = 5 # base seconds
script = None

for attempt in range(max_retries):
    try:
        gemini_res = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
        
        # Handle Successful Response
        if gemini_res.status_code == 200:
            gemini_data = gemini_res.json()
            if 'candidates' in gemini_data and 'content' in gemini_data['candidates'][0]:
                script = gemini_data['candidates'][0]['content']['parts'][0]['text'].strip()
                break # Success! Exit loop.
            else:
                print(f"Error in JSON structure: {gemini_data}")
                exit(1)
        
        # Handle Transient Errors (503 Service Unavailable or 429 Too Many Requests)
        elif gemini_res.status_code in [503, 429]:
            wait_time = retry_delay * (2 ** attempt)
            print(f"API Busy/Overloaded ({gemini_res.status_code}). Retrying in {wait_time}s... (Attempt {attempt+1}/{max_retries})")
            time.sleep(wait_time)
            continue
        
        # Handle Fatal Errors
        else:
            print(f"\n=== HTTP ERROR {gemini_res.status_code} ===")
            print(gemini_res.text)
            exit(1)

    except Exception as e:
        wait_time = retry_delay * (2 ** attempt)
        print(f"Connection Error: {e}. Retrying in {wait_time}s...")
        time.sleep(wait_time)

if not script:
    print("Failed to get a response from Gemini after 3 retries. The server is likely under heavy load. Try again in a few minutes.")
    exit(1)

print("\n=== AI GENERATED SCRIPT ===")
print(script)
print("===========================\n")

print("[4] Generating Fast-Paced Audio via Kokoro TTS...")
if not os.path.exists("kokoro-v0_19.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx", "kokoro-v0_19.onnx")
if not os.path.exists("voices.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin", "voices.bin")

kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
sentences =[s.strip() for s in re.split(r'(?<=[.!?]) +|\n+', script) if s.strip()]
audio_pieces =[]
sample_rate = 24000

for sentence in sentences:
    try:
        samples, sr = kokoro.create(sentence, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        audio_pieces.append(samples)
    except Exception as e:
        print(f"Skipped TTS chunk: {e}")

final_audio = np.concatenate(audio_pieces)
audio_path = "videos/temp/final_narration.wav"
sf.write(audio_path, final_audio, sample_rate)

print("[5] Stitching Fast-Paced Slideshow Video...")
with sf.SoundFile(audio_path) as f:
    total_audio_time = len(f) / f.samplerate

time_per_panel = total_audio_time / len(panel_files)
concat_file_path = "videos/temp/vid_list.txt"

with open(concat_file_path, "w") as f:
    for pf in panel_files:
        abs_path = os.path.abspath(pf).replace('\\', '/')
        f.write(f"file '{abs_path}'\n")
        f.write(f"duration {time_per_panel:.2f}\n")
    f.write(f"file '{os.path.abspath(panel_files[-1]).replace('\\', '/')}'\n")

final_video_path = "videos/final_recap.mp4"
ffmpeg_cmd =[
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file_path, 
    "-i", audio_path, "-c:v", "libx264", "-pix_fmt", "yuv420p", 
    "-c:a", "aac", "-b:a", "192k", "-shortest", final_video_path
]

subprocess.run(ffmpeg_cmd)
print(f"[+] Success! Video generated at {final_video_path}")
