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

# --- DYNAMIC API KEY EXTRACTION ---
GEMINI_API_KEYS = []
found_key_names =[]

pattern = re.compile(r"GEMINI_API_KEY_(\d+)")

# Collect (index, key_name, value)
temp_keys =[]

for key, value in os.environ.items():
    match = pattern.fullmatch(key)
    if match and value.strip() and "YOUR_API_KEY" not in value:
        index = int(match.group(1))
        temp_keys.append((index, key, value.strip()))

# Sort by number (1,2,3...)
temp_keys.sort(key=lambda x: x[0])

# Extract ordered lists
for _, key_name, key_value in temp_keys:
    GEMINI_API_KEYS.append(key_value)
    found_key_names.append(key_name)

VOICE_MODEL = "am_adam"
AUDIO_SPEED = 1.25

if not GEMINI_API_KEYS:
    print("ERROR: No GEMINI API KEYS provided in environment variables.")
    print("Please ensure your secrets are set (e.g., export GEMINI_API_KEY_1='key').")
    exit(1)
else:
    print(f"[i] Successfully loaded {len(GEMINI_API_KEYS)} API Key(s) from: {', '.join(found_key_names)}")
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

# Directories setup
os.makedirs("videos/temp", exist_ok=True)
os.makedirs("videos/raw_strips", exist_ok=True)
os.makedirs("characters", exist_ok=True)

headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

font_path = "Roboto-Black.ttf"
if not os.path.exists(font_path):
    urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Black.ttf", font_path)
font = ImageFont.truetype(font_path, 28)

print(f"[2] Drawing Super Rulers on {len(image_urls)} Strips for the AI Editor...")

parts =[]
original_files = {} 
ai_heights = {} 

for idx, img_url in enumerate(image_urls): 
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
        if img_response.status_code != 200: continue

        image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
        
        raw_path = f"videos/raw_strips/strip_{idx}.jpg"
        image.save(raw_path, format="JPEG", quality=95)
        original_files[idx] = raw_path
        
        ai_img = image.copy()
        ai_img.thumbnail((800, 40000), Image.Resampling.LANCZOS) 
        ai_w, ai_h = ai_img.size
        ai_heights[idx] = ai_h 
        
        draw = ImageDraw.Draw(ai_img, 'RGBA')
        draw.rectangle([(0, 0), (70, ai_h)], fill=(0, 0, 0, 220))
        
        step = 100 
        for y in range(0, ai_h, step):
            mark_value = y // 10 
            draw.line([(0, y), (35, y)], fill=(255, 255, 255, 255), width=4)
            draw.text((40, y - 15), str(mark_value), fill=(255, 255, 0, 255), font=font)
            
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
print("[3] Asking AI to Profile Characters, Crop Panels & Script the Recap...")

prompt_text = """You are a professional scriptwriter and highly successful YouTube Shorts Manhwa recap Director.
I have provided you with chronological Manhwa strips. On the left side of EVERY image is a Super Ruler.

STRICT NARRATION RULES:
1. NO MARKDOWN: You are strictly forbidden from using Markdown formatting in the narration. Output ONLY plain text. No asterisks, bolding, italics, hash symbols, or bullet points.
2. BE EXTREMELY DETAILED: Walk through the chapter chronologically. Do not gloss over the middle. Capture every major plot beat, fight sequence, magic spell, inner thought, and lore reveal step-by-step.
3. YOUTUBE RECAP VOCABULARY: Use high-energy, dynamic, and modern recap language. Inject action-packed verbs and slang like "blitzes", "speedblitzes", "tanks the hit", "flexes his aura", "drops a bombshell", "absolute menace", "absolute unit", or "OP". Tell the story as if you are passionately explaining an awesome manhwa.
4. BAN ON REPETITIVE NAMING ("OUR MC"): You are STRICTLY FORBIDDEN from using the phrase "our MC" more than ONCE in the entire script. You must constantly rotate how you address the main character. Use their actual name, pronouns, or creative aliases.
5. ADVANCED TRANSITIONS: Do not start sentences with basic words like "Then", "Suddenly", "After that", or "But". Use fluid, engaging transitions.
6. PARAPHRASE DIALOGUE & THOUGHTS: Do not use standard dialogue formatting or quote marks. Weave spoken words and inner monologues directly into the narrative.
7. NO FOURTH WALL BREAKS: Never use words like "panel", "image", "reader", "drawn", or "comic". Treat the events as happening in a living, breathing world.
8. DESCRIPTIVE IDENTIFIERS: If a character's name is not explicitly mentioned, give them a memorable title based on their look or vibe.

YOUR TASKS:
1. Identify Characters: Document the name (or descriptive title) and physical appearance of any significant character shown.
2. Select Panels: Choose the most action-packed and story-relevant panels. Skip filler. Look at the ruler to determine exactly where the panel starts and ends (with slight padding).
3. Script: Write the fast-paced narration for that exact panel following the STRICT NARRATION RULES.

OUTPUT FORMAT:
You MUST return a pure JSON object containing a "characters" array and a "panels" array. 
{
  "characters":[
    {"name": "Shirone / The Young Mage", "appearance": "Silver hair, wears ragged commoner clothes, determined eyes"}
  ],
  "panels":[
    {
      "image_index": 0,
      "start_mark": 12,
      "end_mark": 46,
      "narration": "The absolute menace drops from the sky, shattering the ground!"
    }
  ]
}
"""

parts.insert(0, {"text": prompt_text})
payload = {
    "contents": [{"parts": parts}],
    "generationConfig": {"responseMimeType": "application/json"}
}

max_retries = 15 
retry_delay = 5 
script_data = None
current_key_idx = 0

# --- ROBUST API KEY ROTATION ---
for attempt in range(max_retries):
    api_key = GEMINI_API_KEYS[current_key_idx]
    
    # --- UPDATED TO SPECIFICALLY USE gemini-flash-latest ---
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"
    
    print(f"Requesting Gemini API (Attempt {attempt+1}/{max_retries}) using Key Index {current_key_idx} ({found_key_names[current_key_idx]})...")
    try:
        gemini_res = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
        
        if gemini_res.status_code == 200:
            gemini_data = gemini_res.json()
            if 'candidates' in gemini_data and 'content' in gemini_data['candidates'][0]:
                raw_text = gemini_data['candidates'][0]['content']['parts'][0]['text'].strip()
                try:
                    script_data = json.loads(raw_text)
                    print("[+] Successfully generated script!")
                    break 
                except json.JSONDecodeError:
                    print("[-] Failed to parse AI JSON. Retrying...")
                    time.sleep(retry_delay)
                    
        elif gemini_res.status_code in [429, 503, 400]:
            print(f"[-] HTTP {gemini_res.status_code}: {gemini_res.text}")
            current_key_idx = (current_key_idx + 1) % len(GEMINI_API_KEYS)
            print(f"-> Switching to API Key {current_key_idx} ({found_key_names[current_key_idx]})...")
            time.sleep(retry_delay)
        else:
            print(f"[-] HTTP ERROR {gemini_res.status_code}: {gemini_res.text}")
            current_key_idx = (current_key_idx + 1) % len(GEMINI_API_KEYS)
            print(f"-> Switching to API Key {current_key_idx} ({found_key_names[current_key_idx]})...")
            time.sleep(retry_delay)
            
    except Exception as e:
        print(f"[-] Exception during request: {e}")
        current_key_idx = (current_key_idx + 1) % len(GEMINI_API_KEYS)
        print(f"-> Switching to API Key {current_key_idx} ({found_key_names[current_key_idx]})...")
        time.sleep(retry_delay)

if not script_data or "panels" not in script_data:
    print("FATAL ERROR: Failed to get valid JSON from Gemini after all retries and key rotations.")
    exit(1)

# --- EXTRACT & SAVE CHARACTER DATA ---
characters = script_data.get("characters",[])
panels = script_data.get("panels",[])

if characters:
    char_file_path = "characters/chapter_1_characters.txt"
    with open(char_file_path, "w", encoding="utf-8") as cf:
        cf.write("=== DETECTED CHARACTERS ===\n\n")
        for char in characters:
            cf.write(f"Name/Alias: {char.get('name', 'Unknown')}\n")
            cf.write(f"Appearance: {char.get('appearance', 'No description provided')}\n")
            cf.write("-" * 30 + "\n")
    print(f"[+] Saved {len(characters)} character profiles to {char_file_path}")

print(f"[+] AI Editor precision-mapped {len(panels)} panels!")
print("[4] Framing Premium Video Scenes & Generating Audio...")

if not os.path.exists("kokoro-v0_19.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx", "kokoro-v0_19.onnx")
if not os.path.exists("voices.bin"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin", "voices.bin")

kokoro = Kokoro("kokoro-v0_19.onnx", "voices.bin")
sample_rate = 24000

concat_lines =[]
TARGET_W, TARGET_H = 1080, 1920

for i, block in enumerate(panels):
    img_idx = block.get("image_index")
    start_mark = block.get("start_mark", 0)
    end_mark = block.get("end_mark", 10)
    narration = block.get("narration", "").strip()
    
    if img_idx not in original_files or not narration: continue
    if end_mark <= start_mark: end_mark = start_mark + 10 
    
    try:
        raw_img = Image.open(original_files[img_idx])
        
        ai_h = ai_heights[img_idx]
        scale_factor = raw_img.height / ai_h 
        
        top_px = int((start_mark * 10) * scale_factor)
        bottom_px = int((end_mark * 10) * scale_factor)
        
        top_px = max(0, min(top_px, raw_img.height - 10))
        bottom_px = max(top_px + 10, min(bottom_px, raw_img.height))
        
        cropped_panel = raw_img.crop((0, top_px, raw_img.width, bottom_px))
        
        # Audio generation for this specific scene
        samples, sr = kokoro.create(narration, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        audio_path = f"videos/temp/audio_{i:04d}.wav"
        sf.write(audio_path, samples, sample_rate)
        
        exact_duration = max(0.5, len(samples) / sample_rate) # Prevent duration from being too short
        frames = max(1, int(exact_duration * 30))
        
        aspect_ratio = cropped_panel.height / cropped_panel.width
        frame_path = f"videos/temp/panel_{i:04d}.jpg"
        scene_video_path = f"videos/temp/scene_{i:04d}.mp4"
        
        # --- 1. LONG PANEL DETECTED: PAN DOWN (SLIDE) EFFECT ---
        if aspect_ratio > 1.8:
            print(f"   -> Scene {i:02d} | Long Panel Detected | Effect: Cinematic Pan Down")
            
            # Format the input image to exactly 1080 width and whatever natural height scales out
            new_w = TARGET_W
            new_h = max(TARGET_H, int(TARGET_W * aspect_ratio))
            scaled_panel = cropped_panel.resize((new_w, new_h), Image.Resampling.LANCZOS)
            scaled_panel.save(frame_path, format="JPEG", quality=95)
            
            # Use strict evaluation for FFmpeg crop sliding (No single quotes to break subprocess parsing)
            ffmpeg_cmd =[
                "ffmpeg", "-y", 
                "-loop", "1", "-t", f"{exact_duration:.4f}",
                "-i", frame_path, "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", f"crop=1080:1920:0:(in_h-1920)*(t/{exact_duration:.4f}),format=yuv420p",
                "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-ar", "24000", 
                "-pix_fmt", "yuv420p", "-r", "30", "-shortest",
                scene_video_path
            ]
            
        # --- 2. NORMAL PANEL DETECTED: ZOOM IN EFFECT ---
        else:
            print(f"   -> Scene {i:02d} | Normal Panel Detected | Effect: Smooth Zoom In")
            
            # Create a 1080x1920 padded blur background 
            bg = cropped_panel.resize((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(35)) 
            
            scale = min(TARGET_W / cropped_panel.width, TARGET_H / cropped_panel.height)
            new_w, new_h = int(cropped_panel.width * scale), int(cropped_panel.height * scale)
            panel_scaled = cropped_panel.resize((new_w, new_h), Image.Resampling.LANCZOS)
            
            x_offset = (TARGET_W - new_w) // 2
            y_offset = (TARGET_H - new_h) // 2
            bg.paste(panel_scaled, (x_offset, y_offset))
            bg.save(frame_path, format="JPEG", quality=95)
            
            # Use `zoompan` for center zooming without sub-shell quoting conflicts
            ffmpeg_cmd =[
                "ffmpeg", "-y", 
                "-i", frame_path, "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", f"zoompan=z=zoom+0.0015:x=iw/2-(iw/zoom)/2:y=ih/2-(ih/zoom)/2:d={frames}:s=1080x1920:fps=30,format=yuv420p",
                "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k", "-ar", "24000", 
                "-pix_fmt", "yuv420p", "-shortest",
                scene_video_path
            ]
            
        # Execute Render and grab errors if any occur
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"   [!] FFmpeg Error on Scene {i}:\n{result.stderr}")
        elif os.path.exists(scene_video_path):
            abs_scene_path = os.path.abspath(scene_video_path).replace('\\', '/')
            concat_lines.append(f"file '{abs_scene_path}'")
            
    except Exception as e:
        print(f"Skipped Scene {i} due to Python Error: {e}")

if not concat_lines:
    print("Error: No valid scenes generated.")
    exit(1)

concat_file_path = "videos/temp/vid_list.txt"
with open(concat_file_path, "w") as f:
    f.write("\n".join(concat_lines) + "\n")

print("[5] Stitching the Final Edited Masterpiece...")
final_video_path = "videos/final_recap.mp4"

# Lossless concat demuxing
ffmpeg_cmd =[
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file_path, 
    "-c", "copy", final_video_path
]

subprocess.run(ffmpeg_cmd)
print(f"[+] Success! Cinematic Synced Video with Dynamic Camera Movements generated at {final_video_path}")
