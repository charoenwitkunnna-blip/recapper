from seleniumbase import SB
import base64
import requests
import os
import time

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

print(f"Loading: {CHAPTER_URL}")
image_urls =[]

# 1. Launch Undetected Browser with Virtual Display
with SB(uc=True, xvfb=True, locale_code="en") as sb:
    
    # Temporarily disconnects WebDriver from Chrome to bypass initial CF detection
    sb.uc_open_with_reconnect(CHAPTER_URL, reconnect_time=6)
    
    print("Checking for Cloudflare protection...")
    
    # 2. Redirect / Verification Countermeasure
    try:
        sb.uc_gui_click_captcha()
        print("Captcha clicked. Waiting for resolution...")
    except Exception:
        pass 
        
    print("Waiting for Cloudflare redirect to finish (up to 30s)...")
    
    # Wait for the actual manga images class to load into the HTML
    try:
        sb.wait_for_element(".wp-manga-chapter-img", timeout=30)
        print("Successfully bypassed Cloudflare! Manga page is loading.")
    except Exception:
        print("CRITICAL: Timed out waiting for Cloudflare redirect.")
        sb.save_screenshot("debug_screenshot.png")
        print("Check 'debug_screenshot.png' in GitHub Artifacts to see what went wrong.")
        exit(1)

    # 3. Trigger Lazy Loading
    print("Scrolling down to trigger lazy-loaded images...")
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/4);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
    time.sleep(2)
    sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    # Take a screenshot to prove we made it to the chapter!
    sb.save_screenshot("debug_screenshot.png")

    # 4. Extract Image URLs
    images = sb.find_elements("css selector", ".wp-manga-chapter-img")
    
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src:
            image_urls.append(src.strip())
            
    print(f"Found {len(image_urls)} images.")

# AT THIS POINT THE BROWSER CLOSES TO FREE RAM FOR THE AI

if len(image_urls) == 0:
    print("Failed to find image URLs even after redirect.")
    exit(1)

# 5. Process Images with Local AI (Ollama)
target_indices =[0, 1, 2, len(image_urls)//2, len(image_urls)-3, len(image_urls)-2, len(image_urls)-1]
scene_descriptions =[]

headers = {
    "Referer": "https://manhuaus.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

for idx in target_indices:
    if idx >= len(image_urls): continue
    
    img_url = image_urls[idx]
    print(f"Downloading image {idx+1}/{len(image_urls)}...")
    
    try:
        img_response = requests.get(img_url, headers=headers)
        if img_response.status_code != 200:
            print(f"Failed to download image {idx+1}. Status: {img_response.status_code}")
            continue

        encoded_string = base64.b64encode(img_response.content).decode('utf-8')
        print(f"Sending image {idx+1} to local AI (Moondream)...")
        
        payload = {
            "model": "moondream",
            "prompt": "This is a panel from a fantasy manhwa. Describe the setting, the characters, and what is happening in this specific scene.",
            "images":[encoded_string],
            "stream": False
        }
        
        ai_response = requests.post("http://localhost:11434/api/generate", json=payload)
        
        if ai_response.status_code == 200:
            description = ai_response.json().get('response', '')
            scene_descriptions.append(f"- **Scene {idx+1}:** {description}")
        else:
            print(f"AI Error on image {idx+1}")
            
    except Exception as e:
        print(f"Error processing image {idx+1}: {e}")

# 6. Save the Recap to your Repo
if scene_descriptions:
    print("Combining scenes into final recap...")
    final_recap = "\n".join(scene_descriptions)

    os.makedirs("recaps", exist_ok=True)
    filename = "recaps/infinite_mage_chapter_122.md"

    with open(filename, "w") as f:
        f.write(f"# Infinite Mage - Chapter 122 Recap\n\n")
        f.write(f"### Scene Breakdown:\n")
        f.write(final_recap)

    print(f"Success! Saved to {filename}")
