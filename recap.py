from seleniumbase import SB
from PIL import Image
import io
import base64
import requests
import os
import time

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

print(f"Loading: {CHAPTER_URL}")
image_urls =[]
site_cookies = {}

# 1. Launch Undetected Browser with Virtual Display
with SB(uc=True, xvfb=True, locale_code="en") as sb:
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    print("Checking for Cloudflare protection...")
    
    try:
        sb.uc_gui_click_captcha()
    except Exception:
        pass 
        
    print("Waiting for Cloudflare redirect to finish (up to 30s)...")
    
    try:
        sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
        print("Successfully bypassed Cloudflare! Manga page is loading.")
    except Exception:
        print("CRITICAL: Timed out waiting for Cloudflare redirect.")
        sb.save_screenshot("debug_screenshot.png")
        exit(1)

    # 2. Trigger Lazy Loading
    print("Scrolling down to trigger lazy-loaded images...")
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/4);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)
    
    sb.save_screenshot("debug_screenshot.png")

    # 3. Extract Image URLs and Cloudflare Cookies
    images = sb.find_elements("css selector", ".wp-manga-chapter-img")
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src and "http" in src:
            image_urls.append(src.strip())
            
    print(f"Found {len(image_urls)} images.")
    
    for cookie in sb.driver.get_cookies():
        site_cookies[cookie['name']] = cookie['value']

if len(image_urls) == 0:
    print("Failed to find image URLs even after redirect.")
    exit(1)

# 4. Process Images with Local AI
target_indices =[0, 1, 2, len(image_urls)//2, len(image_urls)-3, len(image_urls)-2, len(image_urls)-1]
scene_descriptions =[]

headers = {
    "Referer": "https://manhuaus.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

for idx in target_indices:
    if idx >= len(image_urls): continue
    
    img_url = image_urls[idx]
    print(f"Downloading image {idx+1}/{len(image_urls)}...")
    
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
        
        if img_response.status_code != 200:
            print(f"Failed to download image {idx+1}. Status: {img_response.status_code}")
            continue

        # --- NEW IMAGE CONVERSION LOGIC ---
        # 1. Open the downloaded WEBP image in memory
        image = Image.open(io.BytesIO(img_response.content))
        
        # 2. Convert to standard RGB (removes WEBP transparency if any)
        rgb_im = image.convert('RGB')
        
        # 3. Shrink the image to prevent Ollama from running out of RAM
        # We limit the width to 800px, which is plenty for the AI to read
        max_width = 800
        if rgb_im.width > max_width:
            ratio = max_width / rgb_im.width
            new_height = int(rgb_im.height * ratio)
            # Use Resampling.LANCZOS for modern Pillow versions
            rgb_im = rgb_im.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        # 4. Save as a standard JPEG buffer
        buffered = io.BytesIO()
        rgb_im.save(buffered, format="JPEG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')
        # ----------------------------------

        print(f"Sending formatted image {idx+1} to local AI (Moondream)...")
        
        payload = {
            "model": "moondream",
            "prompt": "This is a panel from a fantasy manhwa. Briefly describe the characters and what is happening in this specific scene.",
            "images":[encoded_string],
            "stream": False
        }
        
        # Give Ollama a timeout so it doesn't hang forever
        ai_response = requests.post("http://localhost:11434/api/generate", json=payload, timeout=120)
        
        if ai_response.status_code == 200:
            description = ai_response.json().get('response', '')
            print(f"Success on image {idx+1}!")
            scene_descriptions.append(f"- **Scene {idx+1}:** {description}")
        else:
            # THIS WILL PRINT EXACTLY WHY OLLAMA FAILS IF IT HAPPENS AGAIN
            print(f"AI Error on image {idx+1}: {ai_response.text}")
            
    except Exception as e:
        print(f"Error processing image {idx+1}: {e}")

# 5. Save the Recap
os.makedirs("recaps", exist_ok=True)
filename = "recaps/infinite_mage_chapter_122.md"

with open(filename, "w") as f:
    f.write(f"# Infinite Mage - Chapter 122 Recap\n\n")
    if scene_descriptions:
        f.write(f"### Scene Breakdown:\n")
        f.write("\n".join(scene_descriptions))
    else:
        f.write("*Failed to process images with AI. Check GitHub Actions logs.*")

print(f"Success! Saved to {filename}")
