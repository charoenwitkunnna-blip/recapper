from seleniumbase import SB
from PIL import Image
from gtts import gTTS
import pytesseract
import io
import base64
import requests
import os
import time
import subprocess
import re

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

def split_into_panels(img, min_gap=30):
    gray = img.convert("L")
    width, height = gray.size
    pixels = gray.load()
    split_y_positions =[]
    current_gap_start = None

    for y in range(height):
        row_samples = [pixels[x, y] for x in range(0, width, 10)]
        if not row_samples: continue
        min_val, max_val = min(row_samples), max(row_samples)
        is_solid_bg = (max_val - min_val < 15) and (min_val > 240 or max_val < 15)

        if is_solid_bg:
            if current_gap_start is None: current_gap_start = y
        else:
            if current_gap_start is not None:
                gap_height = y - current_gap_start
                if gap_height >= min_gap:
                    split_y_positions.append(current_gap_start + (gap_height // 2))
                current_gap_start = None

    split_y_positions.append(height)
    panels =[]
    last_y = 0
    for y in split_y_positions:
        if y - last_y > 150: 
            panel = img.crop((0, last_y, width, y))
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
        last_y = y
    return panels if panels else [img]

print(f"Loading: {CHAPTER_URL}")
image_urls =[]
site_cookies = {}

with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    print("Checking for Cloudflare protection...")
    try: sb.uc_gui_click_captcha()
    except Exception: pass 
        
    print("Waiting for Cloudflare redirect to finish (up to 30s)...")
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

video_files =[]
story_context =[] # THIS HOLDS THE MEMORY OF THE STORY

for idx, img_url in enumerate(image_urls):
    if idx > 0: 
        print("Stopping early: Only processing the first strip.")
        break 
        
    print(f"\n--- Downloading Strip {idx+1} ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image, min_gap=30)
    print(f"Strip sliced into {len(panels)} panels.")
    
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

        # --- EXTRACT TEXT WITH TESSERACT OCR ---
        extracted_text = pytesseract.image_to_string(panel).strip()
        # Clean up weird Tesseract artifacts
        extracted_text = re.sub(r'[^A-Za-z0-9 .,!?\'"]+', '', extracted_text)
        
        text_hint = f'The characters are saying or thinking: "{extracted_text}".' if len(extracted_text) > 3 else "No text visible."
        recent_story = " ".join(story_context[-2:]) if story_context else "The story begins here."

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # --- THE NEW "PERSONA" PROMPT WITH STORY CONTEXT ---
        prompt = f"""You are a dramatic voiceover narrator for an epic manhwa recap video.
        Recent story events: {recent_story}
        {text_hint}
        
        Task: Based on the image and text, write exactly ONE engaging sentence narrating what the characters are doing or saying. 
        Rule 1: DO NOT say "In this panel", "The image shows", "This drawing", or describe logos/watermarks.
        Rule 2: Focus ONLY on narrating the action and emotion of the story.
        Rule 3: If the image is just a logo, a title, or blank background, reply EXACTLY with the word: SKIP
        """

        payload = {
            "model": "moondream",
            "prompt": prompt,
            "images": [encoded_string],
            "stream": False
        }
        
        ai_response = requests.post("http://localhost:11434/api/generate", json=payload, timeout=120)
        description = ai_response.json().get('response', 'SKIP').strip()
        
        # --- FILTER GARBAGE OUTPUTS ---
        if "SKIP" in description or len(description) < 10 or description.startswith("?"):
            print(f"     [!] AI discarded panel (Not story relevant): {description}")
            continue # SKIP MAKING A VIDEO FOR THIS PANEL!
            
        # Clean text for TTS
        description = description.replace("*", "").replace("#", "").replace('"', '')
        print(f"     Narrator: {description}")
        
        # Add to rolling memory
        story_context.append(description)

        # Generate TTS Audio
        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        tts = gTTS(text=description, lang='en', slow=False)
        tts.save(audio_path)

        # Build Video
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
