from seleniumbase import SB
from PIL import Image
from gtts import gTTS
import io
import base64
import requests
import os
import time
import subprocess
import pytesseract

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
        if bottom - top > 100: 
            panel = img.crop((0, top, width, bottom))
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
    return panels if panels else [img]

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
        
        if panel.width > 800:
            panel = panel.resize((800, int(panel.height * (800 / panel.width))), Image.Resampling.LANCZOS)
        
        w, h = panel.size
        panel = panel.crop((0, 0, w - (w%2), h - (h%2)))
        img_path = f"videos/temp/panel_{p_idx}.jpg"
        panel.save(img_path, format="JPEG")

        buffered = io.BytesIO()
        panel.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

        # STEP 1: OCR
        ocr_text = pytesseract.image_to_string(panel).strip()
        ocr_text = " ".join([w for w in ocr_text.split() if len(w) > 1])
        
        # STEP 2: MOONDREAM (VISION) - STRICT ANTI-HALLUCINATION PROMPT
        vision_prompt = "Describe ONLY the literal physical appearance of the main character (hair, clothes) and their exact physical action. Do not guess plot, backstory, or names. Keep it to 1 sentence."
        try:
            res_vision = requests.post("http://localhost:11434/api/generate", json={
                "model": "moondream", "prompt": vision_prompt, "images": [encoded_string], "stream": False
            }, timeout=60)
            raw_visuals = res_vision.json().get('response', '').strip()
        except: raw_visuals = "A character is visible."

        # STEP 3: LLAMA 3.2 (WRITER) - STRICT ANTI-HALLUCINATION PROMPT
        recent_story = " ".join(story_context[-2:]) if story_context else "The story starts."
        writer_prompt = f"""You are a strict, factual narrator recapping a Manhwa. Write EXACTLY ONE engaging sentence narrating what happens in this scene.

        Past Context: {recent_story}
        Visuals: {raw_visuals}
        Spoken Text (OCR): {ocr_text if ocr_text else "None."}

        STRICT RULES TO PREVENT HALLUCINATIONS:
        1. DO NOT invent names! If the OCR text does not explicitly state a name, refer to them purely by their visuals (e.g., "The blonde man", "The woman in the cape", "The warrior").
        2. DO NOT make up magic powers or lore that isn't literally in the Visuals or OCR text.
        3. DO NOT say "The image shows" or "In this panel".
        4. If the visual is empty or just an object, reply ONLY with the word: SKIP
        """
        
        try:
            res_writer = requests.post("http://localhost:11434/api/generate", json={
                "model": "llama3.2", # <-- UPGRADED MODEL HERE
                "prompt": writer_prompt, "stream": False
            }, timeout=90)
            narrator_script = res_writer.json().get('response', '').strip()
        except: continue

        narrator_script = narrator_script.replace("*", "").replace('"', '').strip()
        if "SKIP" in narrator_script.upper() or len(narrator_script) < 10: continue
            
        print(f"     Narrator: {narrator_script}")
        story_context.append(narrator_script)

        # STEP 4: VIDEO BUILD
        audio_path = f"videos/temp/audio_{p_idx}.mp3"
        gTTS(text=narrator_script, lang='en', slow=False).save(audio_path)
        video_path = f"videos/temp/video_{p_idx}.mp4"
        subprocess.run(["ffmpeg", "-loop", "1", "-y", "-i", img_path, "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest", video_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        video_files.append(video_path)

if not video_files: exit(1)

with open("videos/temp/vid_list.txt", "w") as f:
    for vf in video_files: f.write(f"file '{os.path.basename(vf)}'\n")

final_path = "videos/final_recap.mp4"
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "videos/temp/vid_list.txt", "-c", "copy", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"Success! Video at {final_path}")
