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
os.makedirs("videos/raw_strips", exist_ok=True)
headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28) # Perfectly sized for the ruler

print(f"[2] Drawing Super Rulers on {len(image_urls)} Strips for the AI Editor...")

parts =[]
original_files = {} 
ai_heights = {} # We need to remember how tall the AI image was to scale the crop back up!

for idx, img_url in enumerate(image_urls): 
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
        if img_response.status_code != 200: continue

        image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
        
        raw_path = f"videos/raw_strips/strip_{idx}.jpg"
        image.save(raw_path, format="JPEG", quality=95)
        original_files[idx] = raw_path
        
        # Scale for AI
        ai_img = image.copy()
        ai_img.thumbnail((800, 40000), Image.Resampling.LANCZOS) 
        ai_w, ai_h = ai_img.size
        ai_heights[idx] = ai_h 
        
        draw = ImageDraw.Draw(ai_img, 'RGBA')
        
        # Draw a dark background bar so the ruler is highly readable
        draw.rectangle([(0, 0), (70, ai_h)], fill=(0, 0, 0, 220))
        
        # === THE SUPER RULER LOGIC ===
        # Every 100 pixels equals "10 units" on the ruler (0, 10, 20, 30...)
        step = 100 
        for y in range(0, ai_h, step):
            mark_value = y // 10 
            
            # Draw Major Tick & Number
            draw.line([(0, y), (35, y)], fill=(255, 255, 255, 255), width=4)
            draw.text((40, y - 15), str(mark_value), fill=(255, 255, 0, 255), font=font)
            
            # Draw Minor Ticks (Every 2 units / 20 pixels)
            for minor_y in range(y + 20, y + 100, 20):
                if minor_y < ai_h:
                    draw.line([(0, minor_y), (15, minor_y)], fill=(255, 255, 255, 150), width=2)

        buffered = io.BytesIO()
        ai_img.save(buffered, format="JPEG", quality=60)
        b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
        
        parts.append({"text": f"Image Index: {idx}"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
            
    except Exception as e:
        print(f"Error processing image {idx}: {e}")

print(f"[+] Successfully prepared Super Rulers.")
print("[3] Asking AI to Crop Panels & Script the Recap (JSON Response)...")

prompt_text = """You are a professional YouTube Shorts Manhwa recap Director.
I have provided you with chronological Manhwa strips. On the left side of EVERY image is a Super Ruler.
The large numbers mark units like 0, 10, 20, 30. There are smaller tick marks in between them.

YOUR TASK:
1. Select the most action-packed and story-relevant panels. Skip boring filler.
2. For each panel you select, look at the ruler to determine exactly where the panel starts and ends. 
3. Be incredibly precise! If a panel starts exactly on the 30 mark and ends midway between 60 and 70, you should write start_mark: 30, end_mark: 65.
4. Give a tiny bit of padding so you don't chop off heads or dialogue bubbles.
5. Write a fast-paced, high-energy narration for that exact panel.

OUTPUT FORMAT:
You MUST return a pure JSON array of objects.[
  {
    "image_index": 0,
    "start_mark": 12,
    "end_mark": 46,
    "narration": "The absolute menace drops from the sky, shattering the ground!"
  },
  {
    "image_index": 1,
    "start_mark": 100,
    "end_mark": 135,
    "narration": "Without hesitation, he flexes his aura and blitzes the enemy."
  }
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
        elif gemini_res.status_code in[503, 429]:
            time.sleep(retry_delay * (2 ** attempt))
            continue
        else:
            print(f"HTTP ERROR {gemini_res.status_code}: {gemini_res.text}")
            exit(1)
    except Exception as e:
        time.sleep(retry_delay * (2 ** attempt))

if not script_data:
    print("Failed to get valid JSON from Gemini.")
    exit(1)

print(f"[+] AI Editor precision-mapped {len(script_data)} panels!")
print("[4] Framing Premium Video Scenes & Generating Audio...")

if not os.path.exists("kokoro-v0_19.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx", "kokoro-v0_19.onnx")
if not os.path.exists("voices.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin", "voices.bin")

kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
sample_rate = 24000

concat_lines =[]
final_audio_pieces =[]
TARGET_W, TARGET_H = 1080, 1920

for i, block in enumerate(script_data):
    img_idx = block.get("image_index")
    start_mark = block.get("start_mark", 0)
    end_mark = block.get("end_mark", 10)
    narration = block.get("narration", "").strip()
    
    if img_idx not in original_files or not narration: continue
    if end_mark <= start_mark: end_mark = start_mark + 10 
    
    try:
        raw_img = Image.open(original_files[img_idx])
        
        # --- MATHEMATICAL CROPPING ---
        # 1 unit on the ruler = 10 pixels on the AI image. 
        ai_h = ai_heights[img_idx]
        scale_factor = raw_img.height / ai_h 
        
        # Convert the AI's chosen marks back to the original massive 4k image
        top_px = int((start_mark * 10) * scale_factor)
        bottom_px = int((end_mark * 10) * scale_factor)
        
        # Clamp bounds so it doesn't try to crop outside the image
        top_px = max(0, min(top_px, raw_img.height - 10))
        bottom_px = max(top_px + 10, min(bottom_px, raw_img.height))
        
        cropped_panel = raw_img.crop((0, top_px, raw_img.width, bottom_px))
        
        # --- PREMIUM CINEMATIC FRAMING ---
        # 1. Base Blurred Background
        bg = cropped_panel.resize((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(35)) # Smooth, heavy blur
        
        # 2. Fit the actual crisp panel in the center
        scale = min(TARGET_W / cropped_panel.width, TARGET_H / cropped_panel.height)
        new_w, new_h = int(cropped_panel.width * scale), int(cropped_panel.height * scale)
        panel_scaled = cropped_panel.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        x_offset = (TARGET_W - new_w) // 2
        y_offset = (TARGET_H - new_h) // 2
        bg.paste(panel_scaled, (x_offset, y_offset))
        
        frame_path = f"videos/temp/final_frame_{i:04d}.jpg"
        bg.save(frame_path, format="JPEG", quality=95)
        
        # --- GENERATE SYNCHRONIZED AUDIO ---
        samples, sr = kokoro.create(narration, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        final_audio_pieces.append(samples)
        
        exact_duration = len(samples) / sample_rate
        abs_frame_path = os.path.abspath(frame_path).replace('\\', '/')
        
        concat_lines.append(f"file '{abs_frame_path}'")
        concat_lines.append(f"duration {exact_duration:.4f}")
        
    except Exception as e:
        print(f"Skipped Scene {i} due to Error: {e}")

if not concat_lines:
    print("Error: No valid scenes generated.")
    exit(1)

concat_lines.append(concat_lines[-2])

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
print(f"[+] Success! Cinematic Synced Video generated at {final_video_path}")
