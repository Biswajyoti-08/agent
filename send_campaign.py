import os
import requests
import certifi
from pymongo import MongoClient

# 1. MongoDB Configuration
MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
campaigns_col = db["Campaigns"]

# 2. Kapso Configuration
KAPSO_API_KEY = "ba4daeaf0baa99aef4ef48511b71de95168751a6af6a247dab949be0af96a4ef"
RECIPIENTS = ["918280244245"] # Sending to your secondary 'Athlete' phone

def trigger_dynamic_campaign(campaign_id):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    # Fetch the campaign details from MongoDB
    campaign = campaigns_col.find_one({"campaign_id": campaign_id})
    
    if not campaign:
        print(f"❌ Error: Campaign {campaign_id} not found in Database.")
        return

    # Extract Data
    image_url = campaign.get("media_url")
    body_text = campaign.get("template_text")

    for phone in RECIPIENTS:
        # Kapso Media Payload Structure
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "image",  # CRITICAL: Changed from 'text'
                "media_url": image_url,   # The Unsplash link
                "content": body_text      # This becomes the image caption
            }
        }
        
        response = requests.post(url, headers=headers, json=payload)
        
        # Kapso usually returns 200 or 201 on success
        if response.status_code in [200, 201]:
            print(f"✅ SUCCESS: Visual Campaign delivered to {phone}")
        else:
            print(f"⚠️ FAILED: {response.status_code} - {response.text}")

if __name__ == "__main__":
    # Ensure this ID matches your MongoDB 'campaign_id'
    trigger_dynamic_campaign("NIKE_RIO_2026")