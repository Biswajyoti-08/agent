import os
import certifi
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Configuration
MONGO_URI = os.environ.get("MONGO_URI")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY")
COUNTRY_MANAGER_PHONE = "919437725393" # Target for the alert

def generate_regional_risk_report():
    if not MONGO_URI or not KAPSO_API_KEY:
        print("❌ Error: Environment keys missing.")
        return

    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["EnterpriseAgent"]
    chat_col = db["ChatHistory"]
    
    # Analyze the last 7 days
    time_limit = datetime.utcnow() - timedelta(days=7)
    
    # Logic: Find users who are 'escalated' but have 0 'manager_msg' in their history
    # We aggregate to find the 'Last Known Status' per athlete
    pipeline = [
        {"$match": {"timestamp": {"$gt": time_limit}}},
        {"$sort": {"timestamp": -1}},
        {"$group": {
            "_id": "$phone_number",
            "last_msg": {"$first": "$$ROOT"},
            "has_manager_reply": {"$max": {"$cond": [{"$gt": ["$manager_msg", None]}, 1, 0]}}
        }},
        {"$match": {"last_msg.muted": True, "has_manager_reply": 0}}
    ]
    
    ghosted_leads = list(chat_col.aggregate(pipeline))

    if not ghosted_leads:
        print("✅ Clean Sheet: All escalated athletes have been handled.")
        return

    # Build the Executive Report
    report = "🚨 *NIKE INDIA: REGIONAL RISK REPORT*\n"
    report += "_________________________________\n\n"
    report += f"Total Athletes Ghosted: *{len(ghosted_leads)}*\n"
    report += "The following high-intent leads are stuck in 'Escalated' status without a human reply:\n\n"
    
    for lead in ghosted_leads:
        phone = lead["_id"]
        timestamp = lead["last_msg"]["timestamp"].strftime("%d %b, %H:%M")
        report += f"• *{phone}* (Waiting since {timestamp})\n"

    report += "\n⚠️ *Action:* Store Managers must check the Kapso Inbox immediately to prevent revenue loss."

    # Send via Kapso
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "message": {
            "phone_number": COUNTRY_MANAGER_PHONE,
            "content": report,
            "message_type": "text"
        }
    }
    
    try:
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code in [200, 201]:
            print(f"📈 Risk Report delivered to Country Manager. Total Risk: {len(ghosted_leads)} leads.")
    except Exception as e:
        print(f"💥 Failed to send report: {e}")

if __name__ == "__main__":
    generate_regional_risk_report()