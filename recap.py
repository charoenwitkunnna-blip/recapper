import os
import io
import time
import subprocess
import asyncio
import cv2  
import numpy as np  
from PIL import Image
import requests
from seleniumbase import SB

import google.generativeai as genai
import edge_tts

# Configuration
CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("FATAL ERROR: GEMINI_API_KEY environment variable not found.")
    print("Get a free key at: https://aistudio.google.com/app/apikey")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)

# --- NEW AUTO-DETECT MODEL LOGIC ---
# Google deprecates old models constantly. This finds the newest "flash" model automatically.
available_models =[m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
if 'models/gemini-2.5-flash' in available_models:
    MODEL_NAME = 'gemini-2.5-flash'
elif 'models/gemini-2.0-flash' in available_models:
    MODEL_NAME = 'gemini-2.0-flash'
else:
    # Fallback to whatever 'flash' model is active in your region
    flash_models = [m for m in available_models if 'flash' in m]
    MODEL_NAME = flash_models[0].replace('models/', '') if flash_models else 'gemini-2.5-flash'

print(f"Using Google AI Model: {MODEL_NAME}")
model = genai.GenerativeModel(MODEL_NAME)
# -----------------------------------

def split_into_panels(img):
    """OpenCV Panel Extractor (Optimized for Webtoons)"""
    open_cv_image = np.array(img)
    img_bgr = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    
    _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
    kernel = np.ones((30, 30), np.uint8)
    dilated = cv2.dilate(thresh, kernel, iterations=5)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    bounding_boxes =[]
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w > img.width * 0.10 and h > 150: 
            bounding_boxes.append((x, y, w, h))
            
    bounding_boxes = sorted(bounding_boxes, key=lambda b: b[1])
    
    panels =[]
    for bbox in bounding_boxes:
        x, y, w, h = bbox
        x1, y1 = max(0, x - 20), max(0, y - 20)
        x2, y2 = min(img.width, x + w + 20), min(img.height, y + h + 20)
        
        panel = img.crop((x1, y1, x2, y2))
        if panel.height > 2500:
            panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
            panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
        else:
            panels.append(panel)
        
    return panels if panels else [img]

async def generate_audio(text, output_path):
    """Uses Edge-TTS for high-quality TikTok/Shorts style voices."""
    communicate = edge_tts.Communicate(text, "en-US-ChristopherNeural")
    await communicate.save(output_path)

def main():
    print(f"Loading: {CHAPTER_URL}")
    image_urls =[]
    site_cookies = {}

    with SB(uc=True, xvfb=True, locale_code="en") as sb:
        sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
        print("Checking for Cloudflare protection...")
        try: sb.uc_gui_click_captcha()
        except Exception: pass 
            
        print("Waiting for Cloudflare redirect...")
        try: sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
        except Exception: exit(1)

        sb.execute_script("window.scrollTo(0, document.body.scrollHeight/4);")
        time.sleep(2)
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        images = sb.find_elements("css selector", ".wp-manga-chapter-img")
        for img in images:
            src = img.get_attribute("data-src") or img.get_attribute("src")
            if src and "http" in src: image_urls.append(src.strip())
                
        for cookie in sb.driver.get_cookies():
            site_cookies[cookie['name']] = cookie['value']

    if not image_urls: exit(1)
    os.makedirs("videos/temp", exist_ok=True)
    headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}

    # For testing, we only process the first image strip
    img_url = image_urls[0]
    print(f"\n--- Downloading Strip 1 ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    
    if img_response.status_code != 200: exit(1)

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image)
    print(f"Strip sliced into {len(panels)} clean panels.")

    story_context =[]
    video_files =[]

    for p_idx, panel in enumerate(panels):
        print(f"\n  -> Processing Panel {p_idx+1}/{len(panels)}...")
        
        # Resize image
        max_width = 800 
        if panel.width > max_width:
            ratio = max_width / panel.width
            panel = panel.resize((max_width, int(panel.height * ratio)), Image.Resampling.LANCZOS)
        
        w, h = panel.size
        w, h = w - (w % 2), h - (h % 2)
        panel = panel.crop((0, 0, w, h))
        
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        # ==========================================
        # AI STEP: Vision + Script Generation via Gemini
        # ==========================================
        print("[Step 1] Asking Gemini to read panel and write script...")
        recent_story = " ".join(story_context[-3:]) if story_context else "The story begins here."
        
        prompt = f"""You are a dramatic YouTube Shorts narrator for an epic manhwa.
Story context so far: {recent_story}

Look at the provided image panel. Read any dialogue/text bubbles, and observe the action and characters.
Continue the story in a compelling, storytelling tone based ONLY on what you see in the panel. 

CRITICAL RULES:
1. Write EXACTLY ONE punchy, engaging sentence.
2. If there is dialogue in the image, sprinkle in direct quotes! (e.g. A fiery blast erupts as he shouts, 'Die!')
3. Do NOT say "In this panel", "The image shows", or "I see". Just narrate the action.
4. If the image is just a blank page, a title, or a logo, reply EXACTLY with the word: SKIP
"""
        
        try:
            response = model.generate_content([prompt, panel])
            narrator_script = response.text.replace("*", "").replace('"', '').strip()
        except Exception as e:
            print(f"     [!] Gemini API Error: {e}. Skipping.")
            continue

        if "SKIP" in narrator_script.upper() or len(narrator_script) < 15:
            print("     [!] Deemed non-story relevant. Skipped.")
            continue
            
        print(f"     Narrator: {narrator_script}")
        story_context.append(narrator_script)

        # ==========================================
        # AUDIO & VIDEO STEP
        # ==========================================
        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        asyncio.run(generate_audio(narrator_script, audio_path))

        video_path = f"videos/temp/video_{p_idx}.mp4"
        subprocess.run([
            "ffmpeg", "-loop", "1", "-y", 
            "-i", img_path, 
            "-i", audio_path, 
            "-c:v", "libx264", "-tune", "stillimage", 
            "-c:a", "aac", "-b:a", "192k", 
            "-pix_fmt", "yuv420p", 
            "-shortest", video_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        video_files.append(video_path)
        
        # Sleep for 4 seconds to respect Gemini's free tier limits (15 requests per minute)
        time.sleep(4) 

    if not video_files:
        print("No valid story panels found to make a video.")
        exit(1)

    print("\nStitching final recap video together...")
    with open("videos/temp/vid_list.txt", "w") as f:
        for vf in video_files:
            f.write(f"file '{os.path.basename(vf)}'\n")

    final_video_path = "videos/final_recap.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", 
        "-i", "videos/temp/vid_list.txt", 
        "-c", "copy", final_video_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"Success! Narrated video created at {final_video_path}")

if __name__ == "__main__":
    main()
