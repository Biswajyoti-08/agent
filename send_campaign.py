import requests
import certifi
from pymongo import MongoClient

# 1. MongoDB Configuration
MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
campaigns_col = db["Campaigns"]

# 2. Updated Kapso Key
KAPSO_API_KEY = "ba4daeaf0baa99aef4ef48511b71de95168751a6af6a247dab949be0af96a4ef"
RECIPIENTS = ["919437725393"]

def trigger_dynamic_campaign(brand_id):
    campaign = campaigns_col.find_one({"brand_id": brand_id, "status": "active"})
    if not campaign: 
        print("❌ No active campaign found.")
        return

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    for phone in RECIPIENTS:
        # Use a simple string for the body to avoid formatting errors
        body_text = "🚀 Official Air Jordan 10 Rio Launch! Athlete, find your nearest Nike Hub for an exclusive trial."
        
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply", 
                                "reply": {"id": "find_store", "title": "Find Store"}
                            }
                        ]
                    }
                }
            }
        }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 201:
            print(f"✅ SUCCESS: Campaign delivered to {phone}")
        else:
            print(f"⚠️ FAILED: {response.status_code} - {response.text}")

if __name__ == "__main__":
    trigger_dynamic_campaign("NIKE_IND")