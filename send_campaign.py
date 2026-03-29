import os
import requests
import certifi
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
campaigns_col = db["Campaigns"]
stores_col = db["Stores"]

KAPSO_API_KEY = "67d30f40d1f73775087a3287"
RECIPIENTS = ["919437725393", "918660855203"]

def trigger_dynamic_campaign(brand_id):
    campaign = campaigns_col.find_one({"brand_id": brand_id, "status": "active"})
    
    if not campaign:
        print("❌ No active campaign found in MongoDB.")
        return

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    for phone in RECIPIENTS:
        store = stores_col.find_one({"brand_id": brand_id})
        store_name = store["store_name"] if store else "your nearest store"
        
        final_text = campaign["template_text"].replace("{{store}}", store_name)
        
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "image",
                "image": {"url": campaign["media_url"]},
                "content": final_text
            }
        }
        
        requests.post(url, headers=headers, json=payload)
        print(f"🚀 Sent '{campaign['campaign_name']}' to {phone}")

if __name__ == "__main__":
    trigger_dynamic_campaign("NIKE_IND")
