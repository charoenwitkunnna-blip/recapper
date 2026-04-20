from seleniumbase import SB
from PIL import Image
import io
import base64
import requests
import os
import time

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

# --- NEW FUNCTION: AUTO-SLICER ---
def split_into_panels(img, min_gap=30):
    """
    Scans the image horizontally to find solid white or black gaps (background).
    Slices the long manhwa strip into individual comic panels.
    """
    gray = img.convert("L")
    width, height = gray.size
    pixels = gray.load()

    split_y_positions =[]
    current_gap_start = None

    for y in range(height):
        # Sample every 10 pixels across the row to speed up processing
        row_samples = [pixels[x, y] for x in range(0, width, 10)]
        if not row_samples: 
            continue
        
        min_val, max_val = min(row_samples), max(row_samples)
        
        # Check if the row is solid white (>240) or solid black (<15)
        is_solid_bg = (max_val - min_val < 15) and (min_val > 240 or max_val < 15)

        if is_solid_bg:
            if current_gap_start is None:
                current_gap_start = y
        else:
            if current_gap_start is not None:
                gap_height = y - current_gap_start
                # If the solid color gap is taller than 30 pixels, it's a cut point!
                if gap_height >= min_gap:
                    split_y_positions.append(current_gap_start + (gap_height // 2))
                current_gap_start = None

    split_y_positions.append(height)

    panels =[]
    last_y = 0
    for y in split_y_positions:
        # Ignore slivers less than 150px tall
        if y - last_y > 150: 
            panel = img.crop((0, last_y, width, y))
            # Force-split if a panel is still insanely tall (e.g. action scenes bleeding together)
            if panel.height > 2500:
                panels.append(panel.crop((0, 0, panel.width, panel.height//2)))
                panels.append(panel.crop((0, panel.height//2, panel.width, panel.height)))
            else:
                panels.append(panel)
        last_y = y

    # Fallback just in case no gaps were found
    return panels if panels else [img]
# ---------------------------------

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

# 4. Process ALL Images with Local AI
scene_descriptions =[]

headers = {
    "Referer": "https://manhuaus.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# We removed the skipping logic. It now loops through every single image!
for idx, img_url in enumerate(image_urls):
    print(f"\n--- Downloading Strip {idx+1}/{len(image_urls)} ---")
    
    try:
        img_response = requests.get(img_url, headers=headers, cookies=site_cookies)
        
        if img_response.status_code != 200:
            print(f"Failed to download image {idx+1}. Status: {img_response.status_code}")
            continue

        # Open image and convert to RGB
        image = Image.open(io.BytesIO(img_response.content)).convert('RGB')
        
        # SLICE THE MANHWA STRIP INTO PANELS
        panels = split_into_panels(image, min_gap=30)
        print(f"Strip {idx+1} sliced into {len(panels)} individual panels.")
        
        for p_idx, panel in enumerate(panels):
            # Shrink panel width to 800px to save AI RAM
            max_width = 800
            if panel.width > max_width:
                ratio = max_width / panel.width
                new_height = int(panel.height * ratio)
                panel = panel.resize((max_width, new_height), Image.Resampling.LANCZOS)
            
            # Encode to Base64
            buffered = io.BytesIO()
            panel.save(buffered, format="JPEG")
            encoded_string = base64.b64encode(buffered.getvalue()).decode('utf-8')

            print(f"  -> Sending Panel {p_idx+1}/{len(panels)} to local AI...")
            
            payload = {
                "model": "moondream",
                "prompt": "You are reading a manga/manhwa. Describe the characters, expressions, and actions happening in this specific comic panel.",
                "images":[encoded_string],
                "stream": False
            }
            
            ai_response = requests.post("http://localhost:11434/api/generate", json=payload, timeout=120)
            
            if ai_response.status_code == 200:
                description = ai_response.json().get('response', '')
                scene_descriptions.append(f"- **Strip {idx+1}, Panel {p_idx+1}:** {description}")
            else:
                print(f"  -> AI Error on Panel {p_idx+1}: {ai_response.text}")
                
    except Exception as e:
        print(f"Error processing Strip {idx+1}: {e}")

# 5. Save the Recap
os.makedirs("recaps", exist_ok=True)
filename = "recaps/infinite_mage_chapter_122.md"

with open(filename, "w") as f:
    f.write(f"# Infinite Mage - Chapter 122 Recap\n\n")
    if scene_descriptions:
        f.write(f"### Complete Panel-by-Panel Breakdown:\n")
        f.write("\n".join(scene_descriptions))
    else:
        f.write("*Failed to process images with AI. Check GitHub Actions logs.*")

print(f"\nSuccess! Full chapter recap saved to {filename}")
