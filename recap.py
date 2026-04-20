from seleniumbase import Driver
import time
import base64
import requests
import os

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

print(f"Bypassing Cloudflare for: {CHAPTER_URL}")

# 1. Launch Undetected Browser (Headless=False is required to trick Cloudflare, 
# but it won't crash because xvfb provides a virtual screen in the Action runner)
driver = Driver(uc=True, headless=False)

try:
    driver.get(CHAPTER_URL)
    
    # Wait 15 seconds to ensure the Cloudflare Turnstile "Verify you are human" completes
    # SeleniumBase UC mode is designed to automatically solve/bypass this.
    print("Waiting 15 seconds for Cloudflare and lazy-loaded images...")
    time.sleep(15)
    
    # 2. Extract Image URLs
    images = driver.find_elements("css selector", ".wp-manga-chapter-img")
    image_urls =[]
    
    for img in images:
        src = img.get_attribute("data-src") or img.get_attribute("src")
        if src:
            image_urls.append(src.strip())
            
    print(f"Found {len(image_urls)} images.")
    
finally:
    # CRITICAL: We MUST close the browser here to free up RAM before running the AI.
    driver.quit()
    print("Browser closed. RAM freed.")

if len(image_urls) == 0:
    print("CRITICAL: Still couldn't find images. Cloudflare might have upgraded their block.")
    exit(1)

# 3. Process Images with Local AI (Ollama)
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
        # Download the raw image file directly
        img_response = requests.get(img_url, headers=headers)
        if img_response.status_code != 200:
            print(f"Failed to download image {idx+1}. Status: {img_response.status_code}")
            continue

        encoded_string = base64.b64encode(img_response.content).decode('utf-8')
        
        print(f"Sending image {idx+1} to local AI (Moondream)...")
        
        payload = {
            "model": "moondream",
            "prompt": "This is a panel from a fantasy manhwa. Describe the setting, the characters, and what is happening in this specific scene.",
            "images": [encoded_string],
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

# 4. Save the Recap to your Repo
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
