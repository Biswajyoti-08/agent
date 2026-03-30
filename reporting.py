import certifi
import requests
from pymongo import MongoClient
from datetime import datetime, timedelta

MONGO_URI = "mongodb+srv://mohapatrasamanta25_db_user:oKAcibWmspK0cbgP@cluster0.61vmyt1.mongodb.net/?appName=Cluster0"
KAPSO_API_KEY = "ba4daeaf0baa99aef4ef48511b71de95168751a6af6a247dab949be0af96a4ef"
COUNTRY_MANAGER_PHONE = "919437725393"

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client["EnterpriseAgent"]
chat_history = db["ChatHistory"]
brands_col = db["Brands"]

def generate_weekly_risk_report():
    print(f"📊 Generating Regional Risk Report for {datetime.now().date()}...")
    
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    risk_leads = list(chat_history.find({
        "is_handled": "ALERTED", 
        "timestamp": {"$gt": one_week_ago}
    }))

    if not risk_leads:
        print("✅ Clean sheet! No revenue at risk this week.")
        return

    report = "📊 *WEEKLY REVENUE AT RISK REPORT*\n"
    report += "_________________________________\n\n"
    
    brand_stats = {}
    for lead in risk_leads:
        b_id = lead.get("brand_id", "Unknown")
        brand_stats[b_id] = brand_stats.get(b_id, 0) + 1

    total_leaks = 0
    for b_id, count in brand_stats.items():
        brand_doc = brands_col.find_one({"brand_id": b_id})
        name = brand_doc["brand_name"] if brand_doc else b_id
        report += f"🔸 *{name}:* {count} leads ghosted.\n"
        total_leaks += count

    report += f"\n📉 *Total Leads at Risk:* {total_leaks}"
    report += "\n\n⚠️ *Country Manager Action:* Regional managers in high-leak areas must audit store response times immediately."

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
            print("📈 Report pushed successfully to Country Manager.")
        else:
            print(f"❌ Kapso Error: {response.text}")
    except Exception as e:
        print(f"⚠️ Connection Error: {e}")

if __name__ == "__main__":
    generate_weekly_risk_report()
