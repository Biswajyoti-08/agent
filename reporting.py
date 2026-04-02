import os
import certifi
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta

# 1. Configuration via Environment Variables
MONGO_URI = os.environ.get("MONGO_URI")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY")
COUNTRY_MANAGER_PHONE = "919437725393"

def generate_weekly_risk_report():
    if not MONGO_URI or not KAPSO_API_KEY:
        print("❌ Error: Security keys missing. Please set MONGO_URI and KAPSO_API_KEY environment variables.")
        return

    print(f"📊 Scanning for Ghosted Leads... {datetime.now().date()}")
    
    # Initialize DB Connection
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["EnterpriseAgent"]
    chat_history = db["ChatHistory"]
    
    # Define the time window (Last 7 Days)
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    
    # CRITICAL LOGIC: Find escalated leads with NO manager response
    risk_leads = list(chat_history.find({
        "is_human_active": True,
        "manager_msg": {"$exists": False},
        "timestamp": {"$gt": one_week_ago}
    }))

    if not risk_leads:
        print("✅ Success: All high-intent leads have been handled by managers. Clean sheet!")
        return

    # 2. Construct the Report
    report = "🚨 *NIKE REGIONAL RISK REPORT*\n"
    report += "_________________________________\n\n"
    report += "The following High-Intent Leads were escalated but have NOT received a human response:\n\n"
    
    brand_counts = {}
    lead_details = ""

    for lead in risk_leads:
        phone = lead.get("phone_number", "Unknown")
        brand_name = "Nike India" # Defaulting to Nike India for this execution
        brand_counts[brand_name] = brand_counts.get(brand_name, 0) + 1
        
        time_str = lead.get("timestamp").strftime("%d %b, %H:%M")
        lead_details += f"• *{phone}* (Escalated: {time_str})\n"

    # Summary Section
    for name, count in brand_counts.items():
        report += f"🔸 *{name}:* {count} Athletes Ghosted.\n"

    report += "\n📋 *Detailed Lead List:*\n"
    report += lead_details
    
    report += f"\n📉 *Total Revenue at Risk:* {len(risk_leads)} Leads"
    report += "\n\n⚠️ *Action Required:* Ensure Store Managers are monitoring the Kapso Inbox for 'Urgent Escalation' alerts."

    # 3. Push to Country Manager via Kapso
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
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code in [200, 201]:
            print(f"📈 Report sent! {len(risk_leads)} leaks reported to Country Manager.")
        else:
            print(f"❌ Kapso Error: {response.text}")
    except Exception as e:
        print(f"⚠️ Connection Error: {e}")

if __name__ == "__main__":
    generate_weekly_risk_report()