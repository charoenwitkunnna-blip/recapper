from seleniumbase import SB
from PIL import Image
from gtts import gTTS
import io
import base64
import requests
import os
import time
import subprocess

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
        # Sample pixels across the row to check for color variation
        row_samples = [pixels[x, y] for x in range(0, width, 10)]
        if not row_samples: continue
        min_val, max_val = min(row_samples), max(row_samples)
        
        # If the difference between the darkest and lightest pixel is tiny, it's a solid line
        is_empty_row = (max_val - min_val < 15)

        if is_empty_row:
            if consecutive_empty_rows == 0:
                gap_start_y = y
            consecutive_empty_rows += 1
        else:
            # If we hit art, check if the previous gap was bigger than our 30px threshold
            if consecutive_empty_rows >= min_gap:
                split_point = gap_start_y + (consecutive_empty_rows // 2)
                split_y_positions.append(split_point)
            consecutive_empty_rows = 0 # Reset counter

    split_y_positions.append(height) # Add the bottom of the image
    
    panels = []
    for i in range(len(split_y_positions) - 1):
        top = split_y_positions[i]
        bottom = split_y_positions[i+1]
        
        # Ignore tiny artifacts smaller than 100 pixels tall
        if bottom - top > 100: 
            panel = img.crop((0, top, width, bottom))
            
            # If a panel is ridiculously tall (over 2500px), split it in half
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
    try: 
        sb.uc_gui_click_captcha()
    except Exception: 
        pass

    print("Waiting for Cloudflare redirect to finish...")
    try: 
        sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
    except Exception: 
        print("Failed to bypass Cloudflare or find images.")
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

if not image_urls: 
    print("No images found.")
    exit(1)

os.makedirs("videos/temp", exist_ok=True)
headers = {
    "Referer": "https://manhuaus.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

video_files = []
story_context = []

for idx, img_url in enumerate(image_urls):
    if idx > 0:
        print("Stopping early: Only processing the first strip for testing.")
        break # REMOVE THIS BREAK if you want to do the whole chapter

    print(f"\n--- Downloading Strip {idx+1} ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: 
        continue

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

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # ==========================================
        # AI STEP: NARRATION (Combined into 1 step)
        # ==========================================
        print("     [AI] Analyzing image and writing script...")
        
        script_prompt = """You are narrating an intense manhwa story. Look at this panel and describe the action and character emotions in ONE punchy, dramatic sentence. 
        DO NOT say 'The image shows', 'In this panel', or 'The character'. Speak as if you are a storyteller telling a tale."""
        
        payload = {
            "model": "moondream",
            "prompt": script_prompt,
            "images": [encoded_string],
            "stream": False
        }
        
        try:
            res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=120)
            narrator_script = res.json().get('response', '').strip()
        except:
            print("     [!] AI failed. Skipping panel.")
            continue

        # Clean text and filter out "The image shows" if the AI disobeys
        narrator_script = narrator_script.replace("*", "").replace('"', '').strip()
        bad_starts = ["The image shows", "In this panel", "This image", "The picture"]
        for bad in bad_starts:
            if narrator_script.lower().startswith(bad.lower()):
                narrator_script = narrator_script[len(bad):].strip()
                if narrator_script.startswith("that") or narrator_script.startswith("a "):
                    narrator_script = narrator_script[4:].strip()

        if len(narrator_script) < 10:
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
