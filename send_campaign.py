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
RECIPIENTS = ["918280244245"] 

def trigger_dynamic_campaign(campaign_id):
    print(f"🚀 Initializing Campaign: {campaign_id}...")
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    campaign = campaigns_col.find_one({"campaign_id": campaign_id})
    if not campaign:
        print(f"❌ Error: Campaign {campaign_id} not found.")
        return

    image_url = campaign.get("media_url")
    # REPLACE THE PLACEHOLDER: {{store}} -> Nike India Hubs
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