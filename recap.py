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

# Process the uncut strips directly
for idx, img_url in enumerate(image_urls): 
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')

    # --- 1. Prepare Base64 for Gemini AI ---
    # We keep the strip uncut, but scale width to max 768px to prevent huge API payload sizes
    gemini_img = image.copy()
    if gemini_img.width > 768:
        gemini_img = gemini_img.resize((768, int(gemini_img.height * (768 / gemini_img.width))), Image.Resampling.LANCZOS)
    
    buffered = io.BytesIO()
    gemini_img.save(buffered, format="JPEG", quality=75)
    base64_panels.append(base64.b64encode(buffered.getvalue()).decode('utf-8'))

    # --- 2. Prepare Image for FFMPEG Video ---
    # ffmpeg 'concat' requires all images to be the exact same resolution.
    # We place the long strip onto a standard vertical video canvas (1080x1920)
    TARGET_W, TARGET_H = 1080, 1920
    bg = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0)) # Black background
    
    # Scale image to fit within the 1080x1920 canvas without stretching
    scale = min(TARGET_W / image.width, TARGET_H / image.height)
    new_w = int(image.width * scale)
    new_h = int(image.height * scale)
    
    video_img = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # Paste the strip perfectly in the center of the canvas
    x_offset = (TARGET_W - new_w) // 2
    y_offset = (TARGET_H - new_h) // 2
    bg.paste(video_img, (x_offset, y_offset))
    
    img_path = f"videos/temp/strip_{idx}.jpg"
    bg.save(img_path, format="JPEG", quality=90)
    panel_files.append(img_path)

print(f"[3] Sending {len(base64_panels)} Full Strips to Gemini Flash Latest...")

# --- THE ADVANCED YOUTUBE RECAP PROMPT ---
prompt_text = """You are a professional scriptwriter for a highly successful YouTube Manhwa/Manga recap channel. Your task is to transform the provided chapter images into a highly detailed, engaging, and chronological recap script.

STRICT RULES:
1. NO MARKDOWN: You are strictly forbidden from using Markdown formatting. Output ONLY plain text with normal paragraph breaks. No asterisks, bolding, italics, hash symbols, or bullet points.
2. BE EXTREMELY DETAILED: Walk through the chapter chronologically. Do not gloss over the middle. Capture every major plot beat, fight sequence, magic spell, inner thought, and lore reveal step-by-step.
3. YOUTUBE RECAP VOCABULARY: Use high-energy, dynamic, and modern recap language. Inject action-packed verbs and slang like "blitzes", "tanks the hit", "flexes his aura", "drops a bombshell", "absolute menace", or "OP". Tell the story as if you are passionately explaining an awesome manhwa.
4. BAN ON REPETITIVE NAMING ("OUR MC"): You are STRICTLY FORBIDDEN from using the phrase "our MC" more than ONCE in the entire script. You must constantly rotate how you address the main character. Use their actual name, pronouns, or creative aliases (e.g., "the protagonist", "our guy", "the ruthless assassin", "the magic student", "the boy").
5. ADVANCED TRANSITIONS: Do not start sentences with basic words like "Then", "Suddenly", "After that", or "But". Use fluid, engaging transitions (e.g., "Without hesitation," "Refusing to back down," "Cutting through the tension," "Moments later," "Against all odds,").
6. PARAPHRASE DIALOGUE & THOUGHTS: Do not use standard dialogue formatting or quote marks. Weave spoken words and inner monologues directly into the narrative.
7. NO FOURTH WALL BREAKS: Never use words like "panel", "image", "reader", "drawn", or "comic". Treat the events as happening in a living, breathing world.
8. DESCRIPTIVE IDENTIFIERS: If a character's name is not explicitly mentioned, give them a memorable title based on their look or vibe."""

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
parts = [{"text": prompt_text}]
for b64 in base64_panels:
    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

payload = {"contents": [{"parts": parts}]}

try:
    gemini_res = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"})
    gemini_data = gemini_res.json()
    script = gemini_data['candidates'][0]['content']['parts'][0]['text'].strip()
    print("\n=== AI GENERATED SCRIPT ===")
    print(script)
    print("===========================\n")
except Exception as e:
    print("Gemini API Request Failed:", e)
    exit(1)

print("[4] Generating Fast-Paced Audio via Kokoro TTS...")
if not os.path.exists("kokoro-v0_19.onnx"):
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx", "kokoro-v0_19.onnx")
    urllib.request.urlretrieve("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.json", "voices.json")

kokoro = Kokoro("kokoro-v0_19.onnx", "voices.json")

sentences = [s.strip() for s in re.split(r'(?<=[.!?]) +|\n+', script) if s.strip()]
audio_pieces =[]
sample_rate = 24000

for sentence in sentences:
    try:
        samples, sr = kokoro.create(sentence, voice=VOICE_MODEL, speed=AUDIO_SPEED, lang="en-us")
        audio_pieces.append(samples)
    except Exception as e:
        print(f"Skipped TTS for chunk: {sentence[:20]}... Error: {e}")

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
        f.write(f"file '{os.path.basename(pf)}'\n")
        f.write(f"duration {time_per_panel:.2f}\n")
    f.write(f"file '{os.path.basename(panel_files[-1])}'\n")

final_video_path = "videos/final_recap.mp4"
ffmpeg_cmd =[
    "ffmpeg", "-y", 
    "-f", "concat", 
    "-safe", "0", 
    "-i", concat_file_path, 
    "-i", audio_path, 
    "-c:v", "libx264", 
    "-pix_fmt", "yuv420p", 
    "-c:a", "aac", 
    "-b:a", "192k", 
    "-shortest", final_video_path
]

subprocess.run(ffmpeg_cmd, cwd="videos/temp", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.rename(f"videos/temp/final_recap.mp4", final_video_path)

print(f"[+] Success! Video completely generated at {final_video_path}")
