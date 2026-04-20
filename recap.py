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
    gray = img.convert("L")
    width, height = gray.size
    pixels = gray.load()
    
    is_bg_row =[]
    # Identify solid background rows (white or black)
    for y in range(height):
        row_samples = [pixels[x, y] for x in range(0, width, 10)]
        if not row_samples: 
            is_bg_row.append(True)
            continue
        min_val, max_val = min(row_samples), max(row_samples)
        is_solid_bg = (max_val - min_val < 15) and (min_val > 240 or max_val < 15)
        is_bg_row.append(is_solid_bg)
            
    panels =[]
    start_y = 0
    
    # Extract only the content, skipping the whitespace gaps completely
    while start_y < height:
        if is_bg_row[start_y]:
            start_y += 1
            continue
            
        end_y = start_y + 1
        current_gap = 0
        
        while end_y < height:
            if is_bg_row[end_y]:
                current_gap += 1
            else:
                current_gap = 0 
                
            # If we hit exactly 30px of whitespace, panel ends!
            if current_gap >= min_gap:
                break
            end_y += 1
            
        panel_end = end_y - current_gap
        
        if panel_end - start_y > 50: # Ignore tiny noise artifacts
            panel = img.crop((0, start_y, width, panel_end))
            
            # If the panel is still extremely tall, split it in half
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
            
        start_y = end_y
        
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

video_files =[]
story_context =[]

for idx, img_url in enumerate(image_urls):
    if idx > 0: 
        print("Stopping early: Only processing the first strip.")
        break 
        
    print(f"\n--- Downloading Strip {idx+1} ---")
    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image, min_gap=30)
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
        # AI STEP 1: OBJECTIVE IMAGE DESCRIPTION (Moondream)
        # ==========================================
        print("     [Step 1] Analyzing image visuals...")
        payload_1 = {
            "model": "moondream",
            "prompt": "Analyze this comic panel. Describe exactly what the characters look like, their facial expressions, and the action they are performing. Be highly detailed. Do not tell a story.",
            "images":[encoded_string],
            "stream": False
        }
        
        res_1 = requests.post("http://localhost:11434/api/generate", json=payload_1, timeout=120)
        raw_visuals = res_1.json().get('response', '').strip()

        if "[" in raw_visuals or "]" in raw_visuals or len(raw_visuals) < 10:
            print("[!] AI failed visual analysis. Skipping panel.")
            continue

        # ==========================================
        # AI STEP 2: STORY NARRATOR (Llama 3.2 - Text LLM)
        # ==========================================
        print("     [Step 2] Writing narrator script...")
        recent_story = " ".join(story_context[-2:]) if story_context else "The story begins here."
        
        script_prompt = f"""You are a dramatic YouTube Shorts narrator for an epic manhwa.
Previous story events: {recent_story}

Visual observation of the next scene: "{raw_visuals}"

Write exactly ONE punchy, engaging sentence narrating what happens next in the story based ONLY on the visual observation.
CRITICAL RULES:
1. Do NOT use phrases like "The image shows", "In this panel", "The visual observation", or "The character".
2. Describe the action directly (e.g., "A fiery blast erupts as he draws his blade!").
3. If the observation just describes a logo, a title, or a blank page, reply EXACTLY with the word: SKIP
"""

        payload_2 = {
            "model": "llama3.2", # Swapped to Llama 3.2 for the text logic!
            "prompt": script_prompt,
            "stream": False
        }
        
        res_2 = requests.post("http://localhost:11434/api/generate", json=payload_2, timeout=120)
        narrator_script = res_2.json().get('response', '').strip()

        # Clean text and filter skips
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
