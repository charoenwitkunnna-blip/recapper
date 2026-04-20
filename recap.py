from playwright.sync_api import sync_playwright
import base64
import requests
import os

CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

print(f"Scraping: {CHAPTER_URL}")

with sync_playwright() as p:
    # Launch a real Chromium browser
    browser = p.chromium.launch(headless=True)
    # Mask as a standard Windows Chrome browser
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    # 1. Load the page and take a screenshot
    print("Loading webpage...")
    page.goto(CHAPTER_URL, timeout=60000)
    
    # Wait 5 seconds to let Cloudflare verify and JS images lazy-load
    page.wait_for_timeout(5000) 
    
    print("Taking debug screenshot...")
    page.screenshot(path="debug_screenshot.png", full_page=True)
    print("Screenshot saved to debug_screenshot.png!")

    # 2. Extract Image URLs
    image_elements = page.locator('.wp-manga-chapter-img').all()
    image_urls =[]

    for img in image_elements:
        img_url = img.get_attribute('data-src') or img.get_attribute('src')
        if img_url:
            image_urls.append(img_url.strip())

    print(f"Found {len(image_urls)} images in this chapter.")
    
    # Safely exit if Cloudflare still blocked us, preventing the IndexError
    if len(image_urls) == 0:
        print("CRITICAL: No images found! Check 'debug_screenshot.png' in GitHub Action Artifacts to see the block page.")
        browser.close()
        exit(1)

    # 3. Process Images with Local AI (Ollama)
    target_indices =[0, 1, 2, len(image_urls)//2, len(image_urls)-3, len(image_urls)-2, len(image_urls)-1]
    scene_descriptions =[]

    for idx in target_indices:
        if idx >= len(image_urls): continue
        
        img_url = image_urls[idx]
        print(f"Downloading image {idx+1}/{len(image_urls)}...")
        
        # Download the image using the authenticated browser context
        img_response = page.request.get(img_url)
        img_data = img_response.body()
        encoded_string = base64.b64encode(img_data).decode('utf-8')
        
        print(f"Sending image {idx+1} to local AI (Moondream)...")
        
        payload = {
            "model": "moondream",
            "prompt": "This is a panel from a fantasy manhwa. Describe the setting, the characters, and what is happening in this specific scene.",
            "images":[encoded_string],
            "stream": False
        }
        
        try:
            ai_response = requests.post("http://localhost:11434/api/generate", json=payload)
            if ai_response.status_code == 200:
                description = ai_response.json().get('response', '')
                scene_descriptions.append(f"- **Scene {idx+1}:** {description}")
            else:
                print(f"AI Error on image {idx+1}")
        except Exception as e:
            print(f"Failed to connect to local AI: {e}")

    browser.close()

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
