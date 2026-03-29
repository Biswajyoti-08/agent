import os
import time
import certifi
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta

MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
KAPSO_API_KEY = "67d30f40d1f73775087a3287"

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"] 
chat_history = db["ChatHistory"]
stores_col = db["Stores"]
brands_col = db["Brands"]

DECAY_THRESHOLD_MINUTES = 1

def send_dynamic_escalation(manager_phone, customer_phone, brand_name, signature):
    """Fires a brand-specific alert to the correct manager"""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    text = (
        f"🚨 *{brand_name.upper()} LEAD DECAY* 🚨\n\n"
        f"Customer *{customer_phone}* is stalling!\n"
        f"⏳ *Wait Time:* >{DECAY_THRESHOLD_MINUTES} Minute(s)\n\n"
        f"📍 *Action:* Open the Kapso Inbox and click 'Claim' to assist this lead immediately.\n\n"
        f"{signature}"
    )
    
    payload = {
        "message": {
            "phone_number": manager_phone, 
            "content": text, 
            "message_type": "text"
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code in [200, 201]:
            print(f"🚀 Escalated {brand_name} lead {customer_phone} to Manager {manager_phone}")
    except Exception as e:
        print(f"❌ Alert Error: {e}")

def run_retention_audit():
    print(f"🔍 Auditor Active at {datetime.now().strftime('%H:%M:%S')}... Scanning for ghosted leads.")
    
    threshold = datetime.utcnow() - timedelta(minutes=DECAY_THRESHOLD_MINUTES)
    
    stalled_leads = list(chat_history.find({
        "is_handled": False,
        "timestamp": {"$lt": threshold}
    }))

    if not stalled_leads:
        print("✨ No lead leakage detected across brands.")
        return

    for lead in stalled_leads:
        customer = lead.get("phone_number")
        brand_id = lead.get("brand_id", "NIKE_IND") 
        
        brand_config = brands_col.find_one({"brand_id": brand_id})
        
        store_config = stores_col.find_one({"brand_id": brand_id})
        
        if brand_config and store_config:
            manager_phone = store_config["manager_phone"]
            brand_name = brand_config["brand_name"]
            signature = brand_config["signature"]
            
            send_dynamic_escalation(manager_phone, customer, brand_name, signature)
            
            chat_history.update_one({"_id": lead["_id"]}, {"$set": {"is_handled": "ALERTED"}})

if __name__ == "__main__":
    while True:
        run_retention_audit()
        time.sleep(30)
