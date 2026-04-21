from seleniumbase import SB
from PIL import Image
from gtts import gTTS
import io
import base64
import requests
import os
import time
import subprocess
import pytesseract # <-- NEW: For reading speech bubbles!

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

def split_into_panels(img, min_gap=30):
    """
    Scans the image from top to bottom. If it finds 30 consecutive pixels 
    of solid whitespace/blackspace, it cuts the image into a new panel.
    """
    gray = img.convert("L")
    width, height = gray.size
    pixels = gray.load()
    
    split_y_positions = [0]
    consecutive_empty_rows = 0
    gap_start_y = 0

    for y in range(height):
        row_samples = [pixels[x, y] for x in range(0, width, 10)]
        if not row_samples: continue
        min_val, max_val = min(row_samples), max(row_samples)
        
        is_empty_row = (max_val - min_val < 15)

        if is_empty_row:
            if consecutive_empty_rows == 0:
                gap_start_y = y
            consecutive_empty_rows += 1
        else:
            if consecutive_empty_rows >= min_gap:
                split_point = gap_start_y + (consecutive_empty_rows // 2)
                split_y_positions.append(split_point)
            consecutive_empty_rows = 0

    split_y_positions.append(height)
    
    panels = []
    for i in range(len(split_y_positions) - 1):
        top = split_y_positions[i]
        bottom = split_y_positions[i+1]
        
        if bottom - top > 100: 
            panel = img.crop((0, top, width, bottom))
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
                
    return panels if panels else [img]


# ==========================================
# MAIN EXECUTION
# ==========================================

print(f"Loading: {CHAPTER_URL}")
image_urls = []
site_cookies = {}

with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    print("Checking for Cloudflare protection...")
    try: sb.uc_gui_click_captcha()
    except Exception: pass

    print("Waiting for Cloudflare redirect to finish...")
    try: sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
    except Exception: 
        print("Failed to bypass Cloudflare.")
        exit(1)

    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/4);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    images = sb.find_elements("css selector", ".wp-manga-chapter-img")
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src and "http" in src: 
            image_urls.append(src.strip())
            
    for cookie in sb.driver.get_cookies():
        site_cookies[cookie['name']] = cookie['value']

if not image_urls: exit(1)

os.makedirs("videos/temp", exist_ok=True)
headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}
video_files = []
story_context = []

for idx, img_url in enumerate(image_urls):
    if idx > 0:
        print("Stopping early: Only processing the first strip for testing.")
        break 

    print(f"\n--- Downloading Strip {idx+1} ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image, min_gap=30)
    print(f"Strip sliced into {len(panels)} panels.")

    for p_idx, panel in enumerate(panels):
        print(f"\n  -> Processing Panel {p_idx+1}/{len(panels)}...")
        
        # Resize and save panel
        max_width = 800
        if panel.width > max_width:
            ratio = max_width / panel.width
            panel = panel.resize((max_width, int(panel.height * ratio)), Image.Resampling.LANCZOS)
        
        w, h = panel.size
        panel = panel.crop((0, 0, w - (w%2), h - (h%2)))
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # ==========================================
        # STEP 1: EXTRACT TEXT (OCR)
        # ==========================================
        print("     [Step 1] Reading speech bubbles (OCR)...")
        ocr_text = pytesseract.image_to_string(panel).strip()
        # Clean up stray characters
        ocr_text = " ".join([word for word in ocr_text.split() if len(word) > 1 or word.lower() in ['a', 'i']])
        if ocr_text:
            print(f"     -> Found text: '{ocr_text}'")

        # ==========================================
        # STEP 2: EXTRACT VISUALS (Moondream)
        # ==========================================
        print("     [Step 2] AI analyzing character appearances & action...")
        vision_prompt = "Describe the characters in this image (hair color, clothing, facial expression) and exactly what action they are doing right now. Be objective."
        try:
            res_vision = requests.post("http://localhost:11434/api/generate", json={
                "model": "moondream",
                "prompt": vision_prompt,
                "images": [encoded_string],
                "stream": False
            }, timeout=60)
            raw_visuals = res_vision.json().get('response', '').strip()
        except Exception:
            raw_visuals = "A character doing something."

        # ==========================================
        # STEP 3: WRITER AI (Llama 3.2 - 1B)
        # ==========================================
        print("     [Step 3] AI Storyteller drafting the script...")
        recent_story = " ".join(story_context[-2:]) if story_context else "The story just started."
        
        writer_prompt = f"""You are a dramatic YouTube narrator recapping a Manhwa. Write exactly ONE engaging sentence narrating what happens next.

        Previous events: {recent_story}

        CURRENT SCENE DATA:
        - Visuals: {raw_visuals}
        - Character Dialogue/Text: {ocr_text if ocr_text else "No dialogue."}

        RULES:
        1. If the dialogue reveals a character's name, use it! 
        2. If you don't know their name, describe them in the third person based on the visuals (e.g., "The blonde warrior", "The angry mage").
        3. Do NOT say "The image shows" or "In this panel". Tell it like a story!
        4. If the visual is just an inanimate object or blank page, reply with the exact word: SKIP
        """

        try:
            res_writer = requests.post("http://localhost:11434/api/generate", json={
                "model": "llama3.2:1b",
                "prompt": writer_prompt,
                "stream": False
            }, timeout=60)
            narrator_script = res_writer.json().get('response', '').strip()
        except Exception:
            print("     [!] Writer AI failed. Skipping.")
            continue

        narrator_script = narrator_script.replace("*", "").replace('"', '').strip()
        
        if "SKIP" in narrator_script.upper() or len(narrator_script) < 10:
            print(f"     [!] Panel skipped.")
            continue
            
        print(f"     Narrator: {narrator_script}")
        story_context.append(narrator_script)

        # ==========================================
        # STEP 4: BUILD THE VIDEO
        # ==========================================
        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        tts = gTTS(text=narrator_script, lang='en', slow=False)
        tts.save(audio_path)

        video_path = f"videos/temp/video_{p_idx}.mp4"
        subprocess.run([
            "ffmpeg", "-loop", "1", "-y", "-i", img_path, "-i", audio_path, 
            "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k", 
            "-pix_fmt", "yuv420p", "-shortest", video_path
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
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "videos/temp/vid_list.txt", "-c", "copy", final_video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print(f"Success! Narrated video created at {final_video_path}")
