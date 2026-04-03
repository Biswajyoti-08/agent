import os
import re
import requests
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Configuration via Environment Variables
MONGO_URI = os.environ.get("MONGO_URI")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY")

# List of target recipients
RECIPIENTS = ["919437725393"] 

def clean_phone(phone):
    """Strips all non-digit characters from the phone number string."""
    return re.sub(r"\D", "", str(phone))

def trigger_dynamic_campaign(campaign_id):
    if not MONGO_URI or not KAPSO_API_KEY:
        print("❌ Error: Security keys missing. Please set MONGO_URI and KAPSO_API_KEY.")
        return

    print(f"🚀 Initializing Campaign: {campaign_id}...")
    
    # Initialize MongoDB Client
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["EnterpriseAgent"]
    campaigns_col = db["Campaigns"]

    # Kapso API Configuration
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    # Fetch Campaign Data
    campaign = campaigns_col.find_one({"campaign_id": campaign_id})
    if not campaign:
        print(f"❌ Error: Campaign '{campaign_id}' not found in MongoDB.")
        return

    image_url = campaign.get("media_url")
    # Basic personalization: replacing the {{store}} placeholder
    body_text = campaign.get("template_text", "").replace("{{store}}", "Nike India Hubs")

    for raw_phone in RECIPIENTS:
        # 1. Clean the phone number to ensure it's digits only
        phone = clean_phone(raw_phone)
        
        if len(phone) < 10:
            print(f"⚠️ SKIPPED: Invalid phone number format '{raw_phone}'")
            continue

        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "image",
                "media_url": image_url,
                "content": body_text,
                "caption": body_text 
            }
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code in [200, 201]:
                print(f"✅ SUCCESS: Visual Campaign delivered to {phone}")
            else:
                print(f"⚠️ FAILED: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"⚠️ Connection Error for {phone}: {e}")

if __name__ == "__main__":
    # Ensure this matches your 'campaign_id' in MongoDB
    trigger_dynamic_campaign("NIKE_RIO_2026")