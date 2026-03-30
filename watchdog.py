import os
import time
import certifi
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta

MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
KAPSO_API_KEY = "ba4daeaf0baa99aef4ef48511b71de95168751a6af6a247dab949be0af96a4ef"

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
chat_history = db["ChatHistory"]
stores_col = db["Stores"]
brands_col = db["Brands"]

DECAY_THRESHOLD_MINUTES = 1

def apply_kapso_label(phone, brand_name):
    """MODIFICATION 3: Automatically tags the chat in the Shared Inbox for Regional Managers"""
    url = f"https://app.kapso.ai/api/v1/conversations/labels"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    payload = {
        "phone_number": phone,
        "labels": [f"{brand_name} LEAD", "REVENUE AT RISK", "DECAYING"]
    }
    try:
        requests.post(url, headers=headers, json=payload)
        print(f"🏷️ Label 'REVENUE AT RISK' applied to {phone} in Kapso Dashboard.")
    except Exception as e:
        print(f"❌ Label Error: {e}")

def send_whatsapp_escalation(manager_phone, customer_phone, brand_name):
    """Standard WhatsApp Alert for Store Manager"""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    text = (
        f"🚨 *{brand_name.upper()} REVENUE AT RISK* 🚨\n\n"
        f"Athlete *{customer_phone}* has been ghosted for >{DECAY_THRESHOLD_MINUTES}m.\n"
        f"This lead is now visible to the Regional Manager. Claim it NOW."
    )
    
    payload = {
        "message": {
            "phone_number": manager_phone, 
            "content": text, 
            "message_type": "text"
        }
    }
    requests.post(url, headers=headers, json=payload)

def run_retention_audit():
    print(f"🔍 Auditor Scanning... {datetime.now().strftime('%H:%M:%S')}")
    threshold = datetime.utcnow() - timedelta(minutes=DECAY_THRESHOLD_MINUTES)
    
    stalled_leads = list(chat_history.find({
        "is_handled": False,
        "timestamp": {"$lt": threshold}
    }))

    for lead in stalled_leads:
        customer = lead.get("phone_number")
        brand_id = lead.get("brand_id", "NIKE_IND")
        
        brand_config = brands_col.find_one({"brand_id": brand_id})
        store_config = stores_col.find_one({"brand_id": brand_id})
        
        if brand_config and store_config:
            send_whatsapp_escalation(store_config["manager_phone"], customer, brand_config["brand_name"])
            
            apply_kapso_label(customer, brand_config["brand_name"])
            
            chat_history.update_one({"_id": lead["_id"]}, {"$set": {"is_handled": "ALERTED"}})

if __name__ == "__main__":
    while True:
        run_retention_audit()
        time.sleep(30)
