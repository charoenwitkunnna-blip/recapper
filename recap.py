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
    panels = []
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

def call_ai(model, prompt, image_b64=None):
    payload = {"model": model, "prompt": prompt, "stream": False}
    if image_b64:
        payload["images"] = [image_b64]
    
    try:
        res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=300)
        return res.json().get('response', '').strip()
    except Exception as e:
        return f"Error: {e}"

print(f"Loading: {CHAPTER_URL}")
image_urls, site_cookies = [], {}

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
headers = {"Referer": "https://manhuaus.com/", "User-Agent": "Mozilla/5.0"}
video_files, story_context = [], []

for idx, img_url in enumerate(image_urls):
    if idx > 0: break # Process only first strip for testing

    img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
    if img_response.status_code != 200: continue

    image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
    panels = split_into_panels(image, min_gap=30)

    for p_idx, panel in enumerate(panels):
        print(f"\n  -> Processing Panel {p_idx+1}/{len(panels)}...")
        
        # Resize for AI processing
        if panel.width > 800:
            panel = panel.resize((800, int(panel.height * (800 / panel.width))), Image.Resampling.LANCZOS)
        
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # AI 1: THE EYES (Visuals)
        print("     [1/3] Moondream: Analyzing visuals...")
        visual_desc = call_ai("moondream", "Describe the characters, their expressions, and the physical action in detail. Do not read any text.", encoded_string)

        # AI 2: THE EARS (Speech Reading)
        print("     [2/3] MiniCPM-V: Reading speech bubbles...")
        speech_text = call_ai("minicpm-v", "Extract every word written in speech bubbles or text boxes. Output only the transcribed text.", encoded_string)

        # AI 3: THE WRITER (Narrative)
        print("     [3/3] Llama 3.2: Drafting story sentence...")
        recent = " ".join(story_context[-2:]) if story_context else "The story begins."
        narrative_prompt = f"""You are a dramatic Manhwa narrator. 
        Recent Context: {recent}
        Visual Details: {visual_desc}
        Dialogue Found: {speech_text}
        
        Combine these into ONE intense, story-driven sentence. Use third-person. 
        Do not say 'In the image'. If dialogue reveals a name, use it. 
        If the panel is non-story (logo/blank), reply 'SKIP'."""
        
        narrator_script = call_ai("llama3.2", narrative_prompt)

        # Build Video
        if "SKIP" in narrator_script.upper() or len(narrator_script) < 10:
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
