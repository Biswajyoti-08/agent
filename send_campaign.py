import os
import requests
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Configuration via Environment Variables
MONGO_URI = os.environ.get("MONGO_URI")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY")
RECIPIENTS = ["918280244245","919008235018"] 

def trigger_dynamic_campaign(campaign_id):
    if not MONGO_URI or not KAPSO_API_KEY:
        print("❌ Error: Security keys missing. Please set MONGO_URI and KAPSO_API_KEY.")
        return

    print(f"🚀 Initializing Campaign: {campaign_id}...")
    
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["EnterpriseAgent"]
    campaigns_col = db["Campaigns"]

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    campaign = campaigns_col.find_one({"campaign_id": campaign_id})
    if not campaign:
        print(f"❌ Error: Campaign '{campaign_id}' not found in MongoDB.")
        return

    image_url = campaign.get("media_url")
    body_text = campaign.get("template_text").replace("{{store}}", "Nike India Hubs")

    for phone in RECIPIENTS:
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
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code in [200, 201]:
                print(f"✅ SUCCESS: Visual Campaign delivered to {phone}")
            else:
                print(f"⚠️ FAILED: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"⚠️ Connection Error: {e}")

if __name__ == "__main__":
    trigger_dynamic_campaign("NIKE_RIO_2026")