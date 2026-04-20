from seleniumbase import SB
from PIL import Image
from gtts import gTTS
import io
import base64
import requests
import os
import time
import subprocess
import cv2  
import numpy as np  

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

def split_into_panels(img):
    """
    OpenCV Panel Extractor (Upgraded for Webtoons).
    """
    open_cv_image = np.array(img)
    img_bgr = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    
    # Threshold: turn artwork white, background black
    _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
    
    # UPGRADE: We use a massive 30x30 kernel and 5 iterations!
    # Manhwa has floating speech bubbles. This ensures floating text 
    # connects to the character's bounding box and doesn't get chopped in half.
    kernel = np.ones((30, 30), np.uint8)
    dilated = cv2.dilate(thresh, kernel, iterations=5)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    bounding_boxes =[]
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w > img.width * 0.10 and h > 150: # Filter out noise
            bounding_boxes.append((x, y, w, h))
            
    bounding_boxes = sorted(bounding_boxes, key=lambda b: b[1])
    
    panels =[]
    for bbox in bounding_boxes:
        x, y, w, h = bbox
        
        # Add a 20-pixel safety margin so we don't clip text bubble edges
        x1 = max(0, x - 20)
        y1 = max(0, y - 20)
        x2 = min(img.width, x + w + 20)
        y2 = min(img.height, y + h + 20)
        
        panel = img.crop((x1, y1, x2, y2))
        
        if panel.height > 2500:
            panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
            panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
        else:
            panels.append(panel)
        
    return panels if panels else [img]

print(f"Loading: {CHAPTER_URL}")
image_urls =[]
site_cookies = {}

with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    print("Checking for Cloudflare protection...")
    try: sb.uc_gui_click_captcha()
    except Exception: pass 
        
    print("Waiting for Cloudflare redirect to finish...")
    try: sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
    except Exception: exit(1)

    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/4);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
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
headers = {
    "Referer": "https://manhuaus.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

video_files = []
story_context =[]

for idx, img_url in enumerate(image_urls):
    if idx > 0: 
        print("Stopping early: Only processing the first strip.")
        break 
        
    print(f"\n--- Downloading Strip {idx+1} ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    
    panels = split_into_panels(image)
    print(f"Strip sliced into {len(panels)} clean panels.")
    
    for p_idx, panel in enumerate(panels):
        print(f"\n  -> Processing Panel {p_idx+1}/{len(panels)}...")
        
        max_width = 800
        if panel.width > max_width:
            ratio = max_width / panel.width
            new_height = int(panel.height * ratio)
            panel = panel.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        w, h = panel.size
        w = w - (w % 2)
        h = h - (h % 2)
        panel = panel.crop((0, 0, w, h))
        
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # ==========================================
        # AI STEP 1: VISION & OCR (Using Qwen2.5-VL!)
        # ==========================================
        print("[Step 1] Reading image with Qwen2.5-VL...")
        
        vision_prompt = """You are an expert manga and webtoon translator. Carefully analyze this image panel. 
1. Read and transcribe EVERY word of text, dialogue, or sound effect you see. 
2. Describe the characters, their facial expressions, and exact actions. 
3. Describe any magical effects, weapons, or backgrounds. 
Be highly detailed and objective. Do not invent a story, just describe what is visually present."""

        payload_1 = {
            "model": "qwen2.5vl:3b", # Massive upgrade from Moondream!
            "prompt": vision_prompt,
            "images":[encoded_string],
            "stream": False
        }
        
        try:
            res_1 = requests.post("http://localhost:11434/api/generate", json=payload_1, timeout=120)
            raw_visuals = res_1.json().get('response', '').strip()
        except Exception as e:
            print(f"     [!] AI Timeout or Error. Skipping panel.")
            continue

        if len(raw_visuals) < 10:
            print("[!] AI failed visual analysis. Skipping panel.")
            continue

        # ==========================================
        # AI STEP 2: NARRATOR WRITER (Using Llama 3.2)
        # ==========================================
        print("     [Step 2] Writing narrator script...")
        recent_story = " ".join(story_context[-3:]) if story_context else "The story begins here."
        
        script_prompt = f"""You are a dramatic YouTube Shorts narrator for an epic manhwa.
Pasted below is a summary of the story up to this point:
{recent_story}

Visual observation and OCR text of the next panel: "{raw_visuals}"

Your job is to continue the story where it left off in a compelling, storytelling tone using the new visual observation.
If the characters are speaking in the OCR text, sprinkle in direct quotes from them during intense parts to enhance your storytelling.

CRITICAL RULES:
1. Keep it SHORT and CONCISE. Write EXACTLY ONE punchy sentence!
2. Do NOT use phrases like "The image shows", "In this panel", "The visual observation", or "The character".
3. Describe the action directly (e.g., "A fiery blast erupts as he shouts, 'Die!'").
4. If the observation just describes a logo, a title, or a blank page, reply EXACTLY with the word: SKIP
"""

        payload_2 = {
            "model": "llama3.2",
            "prompt": script_prompt,
            "stream": False
        }
        
        try:
            res_2 = requests.post("http://localhost:11434/api/generate", json=payload_2, timeout=120)
            narrator_script = res_2.json().get('response', '').strip()
        except Exception as e:
            print(f"     [!] AI Timeout or Error. Skipping panel.")
            continue

        narrator_script = narrator_script.replace("*", "").replace('"', '').strip()
        
        if "SKIP" in narrator_script.upper() or len(narrator_script) < 15:
            print(f"     [!] Panel skipped (Deemed non-story relevant).")
            continue
            
        print(f"     Narrator: {narrator_script}")
        story_context.append(narrator_script)

        # ==========================================
        # BUILD THE VIDEO
        # ==========================================
        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        tts = gTTS(text=narrator_script, lang='en', slow=False)
        tts.save(audio_path)

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
