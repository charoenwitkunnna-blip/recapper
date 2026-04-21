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
MEMORY_FILE = "videos/memory/characters.txt"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return f.read().strip()
    return "No characters logged yet."

def save_memory(memory_text):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        f.write(memory_text)

def split_into_panels(img, min_gap=30):
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
            if consecutive_empty_rows == 0: gap_start_y = y
            consecutive_empty_rows += 1
        else:
            if consecutive_empty_rows >= min_gap:
                split_y_positions.append(gap_start_y + (consecutive_empty_rows // 2))
            consecutive_empty_rows = 0

    split_y_positions.append(height)
    panels =[]
    for i in range(len(split_y_positions) - 1):
        top, bottom = split_y_positions[i], split_y_positions[i+1]
        if bottom - top > 150: 
            panel = img.crop((0, top, width, bottom))
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
    return panels if panels else [img]

def call_vision_ai(prompt, image_b64):
    try:
        res = requests.post("http://localhost:11434/api/generate", json={
            "model": "minicpm-v", 
            "prompt": prompt, 
            "images": [image_b64], 
            "stream": False
        }, timeout=300)
        return res.json().get('response', '').strip()
    except Exception as e:
        return ""

print(f"Loading: {CHAPTER_URL}")
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

if not image_urls: exit(1)

os.makedirs("videos/temp", exist_ok=True)
os.makedirs("videos/memory", exist_ok=True)
headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}
video_files, story_context = [],[]

save_memory("No characters logged yet.")

for idx, img_url in enumerate(image_urls):
    if idx > 0: break # Testing: only first strip

    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image, min_gap=30)

    for p_idx, panel in enumerate(panels):
        print(f"\n  -> Processing Panel {p_idx+1}/{len(panels)}...")
        
        if panel.width > 800:
            panel = panel.resize((800, int(panel.height * (800 / panel.width))), Image.Resampling.LANCZOS)
        
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # ==========================================
        # PASS 1: The Director (Categorize Faces & OCR)
        # ==========================================
        print("     [Director] Identifying characters and reading text...")
        current_memory = load_memory()
        
        director_prompt = f"""You are an anime character tracker and OCR reader. 
        Current Character Database: {current_memory}
        
        Look at the image and do TWO things:
        1. Identify the characters. If they are in the database, use their name. If they are new, invent a descriptive name for them (e.g., "Blue-Haired Boy") and add them to the database.
        2. Read all text inside speech bubbles or boxes.

        Format your reply EXACTLY like this:
        [PRESENT]
        (list the characters in the image here)
        [TEXT]
        (write the spoken dialogue here, or 'None')
        [DATABASE]
        (write the updated character database here)
        """
        
        director_output = call_vision_ai(director_prompt, encoded_string)
        
        # Safely parse the director's output
        present_chars, speech_text, new_db = "Unknown", "None", current_memory
        if "[PRESENT]" in director_output and "[TEXT]" in director_output:
            try:
                present_chars = director_output.split("[TEXT]")[0].replace("[PRESENT]", "").strip()
                remainder = director_output.split("[TEXT]")[1]
                if "[DATABASE]" in remainder:
                    speech_text = remainder.split("[DATABASE]")[0].strip()
                    new_db = remainder.split("[DATABASE]")[1].strip()
                    save_memory(new_db)
            except: pass

        print(f"     -> Characters: {present_chars}")
        print(f"     -> Dialogue: {speech_text}")

        # ==========================================
        # PASS 2: The Writer (Narrative Generation)
        # ==========================================
        print("     [Writer] Looking at the image and drafting the script...")
        recent_story = " ".join(story_context[-2:]) if story_context else "The story begins."
        
        writer_prompt = f"""You are a dramatic Manhwa narrator. Look at the action happening in this image.
        
        Here is what you need to know about the image:
        - Characters present: {present_chars}
        - Spoken Dialogue: {speech_text}
        
        Recent Story Context: {recent_story}
        
        Write EXACTLY ONE cinematic, dramatic sentence narrating the story. 
        DO NOT describe the image. Just tell the story based on what the characters are doing.
        Use the character names provided!
        If the image is just an empty wall or logo, reply ONLY with: SKIP
        """
        
        narrator_script = call_vision_ai(writer_prompt, encoded_string).replace("*", "").replace('"', '').strip()

        # Build Video
        if "SKIP" in narrator_script.upper() or len(narrator_script) < 10:
            print("     [!] Panel skipped.")
            continue
            
        print(f"     Narrator: {narrator_script}")
        story_context.append(narrator_script)

        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        gTTS(text=narrator_script, lang='en', slow=False).save(audio_path)
        video_path = f"videos/temp/video_{p_idx}.mp4"
        subprocess.run(["ffmpeg", "-loop", "1", "-y", "-i", img_path, "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest", video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        video_files.append(video_path)

# Stitching
if video_files:
    with open("videos/temp/vid_list.txt", "w") as f:
        for vf in video_files: f.write(f"file '{os.path.basename(vf)}'\n")
    final_path = "videos/final_recap.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "videos/temp/vid_list.txt", "-c", "copy", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Success! Video at {final_path}")
