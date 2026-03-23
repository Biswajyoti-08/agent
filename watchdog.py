import os
import datetime
from pymongo import MongoClient
import certifi
import requests

# 1. Hardcoded Credentials for Local Demo
MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
KAPSO_API_KEY = "d7a9dd3a062d70357c9cd21b1f3d81f8644e4fe0a0c0d41dd3dec7a38cf3810e"

# 2. Setup MongoDB Connection
try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["WhatsAppAgent"]
    chat_history = db["ChatHistory"]
    print("✅ Connected to MongoDB Atlas successfully.")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")

def send_whatsapp_escalation(manager_phone, customer_phone, last_msg):
    """Fires the high-priority alert to the Manager"""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    text = (
        f"🚨 *ON-GROUND: LEAD DECAY ALERT*\n\n"
        f"Athlete *{customer_phone}* is stalling!\n"
        f"💬 *Last Query:* \"{last_msg[:50]}...\"\n"
        f"⏳ *Wait Time:* >24 Hours (Demo: >1 Min)\n\n"
        f"📍 *Action Required:* Reply 'Claim' to the customer chat NOW to prevent revenue leakage. Just Do It."
    )
    
    payload = {
        "message": {
            "phone_number": manager_phone, 
            "content": text, 
            "message_type": "text"
        }
    }
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code in [200, 201]:
        print(f"🚀 Escalated lead {customer_phone} to Manager {manager_phone}")
    else:
        print(f"❌ Kapso Error: {response.status_code} | {response.text}")

def run_retention_audit():
    print(f"🔍 Auditor Active... Checking for ghosted leads.")
    
    # DEMO THRESHOLD: 1 minute 
    # (Matches the timestamp format saved in your main.py)
    threshold = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    
    # Find leads that are NOT handled and were last updated > 1 min ago
    stalled_leads = chat_history.find({
        "is_handled": False,
        "timestamp": {"$lt": threshold}
    })

    found_leads = False
    for lead in stalled_leads:
        found_leads = True
        customer = lead.get("phone_number")
        last_msg = lead.get("user_msg", "New lead intent detected.")
        
        # Using your phone number for the escalation alert
        manager_to_alert = "919437725393" 
        
        send_whatsapp_escalation(manager_to_alert, customer, last_msg)

    if not found_leads:
        print("✨ All athletes are currently being served. No lead leakage.")

if __name__ == "__main__":
    run_retention_audit()
