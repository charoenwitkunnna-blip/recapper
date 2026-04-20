import cloudscraper
from bs4 import BeautifulSoup
import base64
import requests
import json
import os

# The URL of the chapter you want to recap
CHAPTER_URL = "https://manhuaus.com/manga/infinite-mage/chapter-122/"

print(f"Scraping: {CHAPTER_URL}")

# 1. Scrape the Manhua site
scraper = cloudscraper.create_scraper() # Bypasses basic Cloudflare/Anti-bot
response = scraper.get(CHAPTER_URL)
soup = BeautifulSoup(response.text, 'html.parser')

# 2. Extract Image URLs based on your HTML snippet
image_elements = soup.select('.wp-manga-chapter-img')
image_urls =[]

for img in image_elements:
    # Some images use lazy loading with 'data-src', others use 'src'
    img_url = img.get('data-src') or img.get('src')
    if img_url:
        image_urls.append(img_url.strip())

print(f"Found {len(image_urls)} images in this chapter.")

# 3. Process Images with Local AI (Ollama)
# NOTE: To prevent the GitHub Action from taking 2 hours or running out of RAM, 
# we might only want to sample every 2nd or 3rd image, or limit it to the first 5 and last 5.
# For this example, let's process the first 3, middle 2, and last 3 images to get the plot.
target_indices =[0, 1, 2, len(image_urls)//2, len(image_urls)-3, len(image_urls)-2, len(image_urls)-1]
scene_descriptions =[]

for idx in target_indices:
    if idx >= len(image_urls): continue
    
    img_url = image_urls[idx]
    print(f"Downloading image {idx+1}/{len(image_urls)}...")
    
    # Download the webp image
    img_data = scraper.get(img_url).content
    encoded_string = base64.b64encode(img_data).decode('utf-8')
    
    print(f"Sending image {idx+1} to local AI (Moondream)...")
    
    # Ask the Vision AI to describe the panel
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

# 4. Generate Final Chapter Recap Text
print("Combining scenes into final recap...")
final_recap = "\n".join(scene_descriptions)

# 5. Save the Recap to your Repo
os.makedirs("recaps", exist_ok=True)
filename = "recaps/infinite_mage_chapter_122.md"

with open(filename, "w") as f:
    f.write(f"# Infinite Mage - Chapter 122 Recap\n\n")
    f.write(f"### Scene Breakdown:\n")
    f.write(final_recap)

print(f"Success! Saved to {filename}")
