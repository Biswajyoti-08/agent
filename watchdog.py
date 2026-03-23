import os
import datetime
from pymongo import MongoClient
import certifi
import requests
from dotenv import load_dotenv

# Load local .env if you are running this on your desktop
load_dotenv()

# 1. Setup Connections
MONGO_URI = os.environ.get("MONGO_URI")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["WhatsAppAgent"]
chat_history = db["ChatHistory"]

def send_whatsapp_escalation(manager_phone, customer_phone, last_msg):
    """Fires the high-priority alert to the Manager/Regional Head"""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    # Professional B2B Escalation Template
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
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code in [200, 201]:
            print(f"✅ Escalated lead {customer_phone} to Manager {manager_phone}")
        else:
            print(f"❌ Failed to escalate. Status: {response.status_code} | {response.text}")
    except Exception as e:
        print(f"❌ Error during API call: {e}")

def run_retention_audit():
    print(f"🔍 Auditor Active... Checking for ghosted Nike leads.")
    
    # DEMO THRESHOLD: 1 minute (Change to 'hours=24' for production)
    threshold = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    
    # Find leads where:
    # 1. is_handled is False (AI is still in charge)
    # 2. Last message is older than the threshold
    stalled_leads = chat_history.find({
        "is_handled": False,
        "timestamp": {"$lt": threshold}
    })

    found_leads = False
    for lead in stalled_leads:
        found_leads = True
        customer = lead.get("phone_number")
        last_msg = lead.get("user_msg", "Unknown Inquiry")
        
        # For the PoC, we escalate to your primary testing number
        # In scale, this would lookup the Store Manager's number from the 'Stores' DB
        manager_to_alert = "919437725393" 
        
        send_whatsapp_escalation(manager_to_alert, customer, last_msg)

    if not found_leads:
        print("✅ No lead leakage detected. All athletes are being served.")

if __name__ == "__main__":
    run_retention_audit()
