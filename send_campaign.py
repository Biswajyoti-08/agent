import os
import requests
import certifi
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
campaigns_col = db["Campaigns"]
stores_col = db["Stores"]

KAPSO_API_KEY = "ba4daeaf0baa99aef4ef48511b71de95168751a6af6a247dab949be0af96a4ef"
RECIPIENTS = ["919437725393"]

def trigger_dynamic_campaign(brand_id):
    campaign = campaigns_col.find_one({"brand_id": brand_id, "status": "active"})
    
    if not campaign:
        print(f"❌ No active campaign found for {brand_id}")
        return

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    for phone in RECIPIENTS:
        body_text = campaign["template_text"].replace("{{store}}", "Brigade Road Hub")
        
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": "find_store", "title": "Find Nearest Store"}}
                        ]
                    }
                }
            }
        }
        
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code == 201:
            print(f"✅ SUCCESS: '{campaign['campaign_name']}' delivered!")
        else:
            print(f"⚠️ FAILED: {response.status_code} - {response.text}")

if __name__ == "__main__":
    trigger_dynamic_campaign("NIKE_IND")
