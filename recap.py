from seleniumbase import SB
from PIL import Image, ImageDraw, ImageFont
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
from kokoro_onnx import Kokoro

# ================= CONFIGURATION =================
CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-1/"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
VOICE_MODEL = "am_adam"
AUDIO_SPEED = 1.25

if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY environment variable not set.")
    exit(1)
# =================================================

print(f"[1] Loading Manhwa URL: {CHAPTER_URL}")
image_urls, site_cookies =[], {}

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

# Download a Bold Font for the AI Overlay
font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 120)

print(f"[2] Slicing Video Frames and Generating AI Overlay Strips...")
TARGET_W, TARGET_H = 1080, 1920

panel_files = {}  # Map scene_id -> high res video frame
parts =[]        # Payload array for Gemini
scene_counter = 0

for idx, img_url in enumerate(image_urls): 
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
        if img_response.status_code != 200: continue

        image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
        
        # Scale strip width to perfectly fit video frame
        scale = TARGET_W / image.width
        new_h = int(image.height * scale)
        video_img = image.resize((TARGET_W, new_h), Image.Resampling.LANCZOS)
        
        # Create a copy to draw the AI overlay onto
        ai_img = video_img.copy()
        draw = ImageDraw.Draw(ai_img, 'RGBA')
        
        current_y = 0
        step = TARGET_H - 150 # 150px overlap for seamless transition
        
        while current_y < new_h:
            scene_id = f"{scene_counter:04d}"
            
            # --- 1. Extract Clean Video Frame ---
            box = (0, current_y, TARGET_W, min(current_y + TARGET_H, new_h))
            slice_img = video_img.crop(box)
            
            if slice_img.height < TARGET_H:
                bg = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0))
                bg.paste(slice_img, (0, 0))
                slice_img = bg
                
            img_path = f"videos/temp/scene_{scene_id}.jpg"
            slice_img.save(img_path, format="JPEG", quality=90)
            panel_files[scene_id] = os.path.abspath(img_path).replace('\\', '/')
            
            # --- 2. Draw ID Overlay onto the AI Strip ---
            # Red division line
            draw.line([(0, current_y), (TARGET_W, current_y)], fill=(255, 0, 0, 255), width=12)
            # Semi-transparent background box for readability
            draw.rectangle([(0, current_y), (500, current_y + 150)], fill=(0, 0, 0, 220))
            # Bright Yellow Text
            draw.text((20, current_y + 15), f"ID: {scene_id}", fill=(255, 255, 0, 255), font=font)
            
            scene_counter += 1
            current_y += step
            
        # --- 3. Compress and send the single overlayed strip to the AI ---
        ai_img.thumbnail((720, 50000), Image.Resampling.LANCZOS) # Scale down width, keep height unlimited
        buffered = io.BytesIO()
        ai_img.save(buffered, format="JPEG", quality=65)
        b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
            
    except Exception as e:
        print(f"Error processing image {idx}: {e}")

if not panel_files:
    print("No images were successfully processed.")
    exit(1)

print(f"[+] Processed {scene_counter} scenes across {len(parts)} AI Contact Strips.")
print("[3] Asking AI to Edit & Script the Recap (JSON Response)...")

prompt_text = """You are a professional YouTube Manhwa recap Director and Scriptwriter.
I have provided you with the full Manhwa strips. I have drawn red lines to divide the strips into scenes, and stamped each scene with a highly visible ID (e.g., ID: 0000).

YOUR TASK:
1. Be the Editor: Select ONLY the IDs of the most visually exciting and story-relevant scenes.
2. Cut the fat: COMPLETELY IGNORE (cut) IDs that show boring transitions, blank backgrounds, or filler. We will only show the images you select in the video.
3. Be the Writer: For every ID you select, write an action-packed, high-energy narration script that perfectly matches the action inside that specific red boundary. 
4. Don't use the phrase "our MC" more than once.

OUTPUT FORMAT:
You MUST return a pure JSON array of objects.[
  {"id": "0000", "narration": "The absolute menace drops from the sky, shattering the ground!"},
  {"id": "0003", "narration": "Without hesitation, he flexes his aura and blitzes the enemy."}
]
"""

parts.insert(0, {"text": prompt_text})

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"

payload = {
    "contents": [{"parts": parts}],
    "generationConfig": {"responseMimeType": "application/json"}
}

max_retries = 3
retry_delay = 5 
script_data = None

for attempt in range(max_retries):
    try:
        gemini_res = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
        
        if gemini_res.status_code == 200:
            gemini_data = gemini_res.json()
            if 'candidates' in gemini_data and 'content' in gemini_data['candidates'][0]:
                raw_text = gemini_data['candidates'][0]['content']['parts'][0]['text'].strip()
                try:
                    script_data = json.loads(raw_text)
                    break 
                except json.JSONDecodeError:
                    print(f"Failed to parse AI JSON. Retrying...")
            else:
                print(f"Error in JSON structure: {gemini_data}")
                exit(1)
        elif gemini_res.status_code in [503, 429]:
            wait_time = retry_delay * (2 ** attempt)
            print(f"API Busy ({gemini_res.status_code}). Retrying in {wait_time}s...")
            time.sleep(wait_time)
            continue
        else:
            print(f"\n=== HTTP ERROR {gemini_res.status_code} ===")
            print(gemini_res.text)
            exit(1)

    except Exception as e:
        wait_time = retry_delay * (2 ** attempt)
        print(f"Connection Error: {e}. Retrying in {wait_time}s...")
        time.sleep(wait_time)

if not script_data:
    print("Failed to get valid JSON from Gemini after 3 retries.")
    exit(1)

print(f"[+] AI Editor chose {len(script_data)} crucial action scenes out of {scene_counter}.")
print("[4] Generating Tightly-Packed Audio & Syncing Timing...")

if not os.path.exists("kokoro-v0_19.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx", "kokoro-v0_19.onnx")
if not os.path.exists("voices.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin", "voices.bin")

kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
sample_rate = 24000

concat_lines = []
final_audio_pieces =[]

for block in script_data:
    # Safely extract the ID in case the AI writes "ID: 0001" or "1" instead of "0001"
    raw_id = str(block.get("id", ""))
    match = re.search(r'\d+', raw_id)
    if not match: continue
    
    clean_id = f"{int(match.group()):04d}"
    narration = block.get("narration", "").strip()
    
    if not narration or clean_id not in panel_files:
        continue 
        
    try:
        # Generate the Audio 
        samples, sr = kokoro.create(narration, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        
        # PER REQUEST: No artificial pauses added here! Audio is tightly packed.
        final_audio_pieces.append(samples)
        
        exact_duration = len(samples) / sample_rate
        
        # Build the exact scene duration for the selected image
        concat_lines.append(f"file '{panel_files[clean_id]}'")
        concat_lines.append(f"duration {exact_duration:.4f}")
        
    except Exception as e:
        print(f"Skipped TTS chunk: {e}")

if not concat_lines:
    print("Error: No valid scenes were stitched. The AI may have hallucinated IDs.")
    exit(1)

# FFmpeg concat requires the last file to be stated again without a duration
last_file_line = concat_lines[-2]
concat_lines.append(last_file_line)

audio_path = "videos/temp/final_narration.wav"
sf.write(audio_path, np.concatenate(final_audio_pieces), sample_rate)

concat_file_path = "videos/temp/vid_list.txt"
with open(concat_file_path, "w") as f:
    f.write("\n".join(concat_lines) + "\n")

print("[5] Rendering the Final Edited Masterpiece...")
final_video_path = "videos/final_recap.mp4"
ffmpeg_cmd =[
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file_path, 
    "-i", audio_path, "-c:v", "libx264", "-pix_fmt", "yuv420p", 
    "-c:a", "aac", "-b:a", "192k", "-shortest", final_video_path
]

subprocess.run(ffmpeg_cmd)
print(f"[+] Success! Edited & Synced Video generated at {final_video_path}")
