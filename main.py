import os
import requests
import certifi
import math
import time
from datetime import datetime
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Initialize Clients
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")

# 2. MongoDB Setup
mongo_uri = os.environ.get("MONGO_URI")
mongo_client = MongoClient(mongo_uri, tlsCAFile=certifi.where()) 
db = mongo_client["WhatsAppAgent"]
chat_history = db["ChatHistory"]

# --- NIKE STORE REGISTRY ---
STORES = [
    {"id": "NIKE_001", "name": "Nike Flagship - Brigade Road", "lat": 12.9719, "lon": 77.6070, "manager_phone": "919437725393", "open": "09:00", "close": "22:00", "load": 0.3},
    {"id": "NIKE_002", "name": "Nike Hub - Indiranagar", "lat": 12.9784, "lon": 77.6408, "manager_phone": "919437725393", "open": "10:00", "close": "21:30", "load": 0.5}
]

# --- SIMULATED ENTERPRISE INTEGRATIONS ---
def sync_to_zoho_crm(phone, status):
    """Simulates updating a Lead record in Zoho CRM"""
    print(f"☁️ [ZOHO CRM] Syncing Lead {phone} | New Status: {status}", flush=True)

def sync_to_sap_erp(phone, action):
    """Simulates logging a transaction/handoff event in SAP"""
    print(f"⚙️ [SAP ERP] Logging System Event: {action} for Athlete {phone}", flush=True)

# --- UTILS ---
def get_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def find_best_store(u_lat, u_lon):
    now = datetime.now().strftime("%H:%M")
    valid_stores = [s for s in STORES if s["open"] <= now <= s["close"]]
    for s in valid_stores: s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
    return min(valid_stores, key=lambda x: x["dist"]) if valid_stores else None

def send_whatsapp_message(phone_number: str, text: str):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone_number, "content": text, "message_type": "text"}}
    requests.post(url, headers=headers, json=payload)

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")
    user_text = message_data.get("text", {}).get("body", "").strip()
    user_text_low = user_text.lower()

    # --- 1. HYBRID HANDOVER LOGIC (The Claim/Release Toggle) ---
    if user_text_low == "claim":
        chat_history.update_many({"phone_number": sender_phone}, {"$set": {"is_handled": True}})
        sync_to_zoho_crm(sender_phone, "HUMAN_IN_PROGRESS")
        sync_to_sap_erp(sender_phone, "AI_TO_HUMAN_HANDOVER")
        send_whatsapp_message(sender_phone, "✅ *System:* Lead claimed. AI is now in 'Silent Mode'. Human advisor is in control.")
        return {"status": "claimed"}

    if user_text_low == "release":
        chat_history.update_many({"phone_number": sender_phone}, {"$set": {"is_handled": False}})
        sync_to_zoho_crm(sender_phone, "AI_NURTURING")
        sync_to_sap_erp(sender_phone, "HUMAN_TO_AI_RELEASE")
        send_whatsapp_message(sender_phone, "🤖 *System:* AI is back online. How else can I assist you, Athlete? Just Do It.")
        return {"status": "released"}

    # --- 2. LOCATION LOGIC ---
    location = message_data.get("location")
    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        best = find_best_store(u_lat, u_lon)
        if best:
            maps_url = f"https://www.google.com/maps?q={best['lat']},{best['lon']}"
            reply = f"👟 *Athlete!* Our *{best['name']}* is {round(best['dist'], 1)}km away.\n📍 Directions: {maps_url}\nI've alerted the Manager. Just Do It."
            send_whatsapp_message(best["manager_phone"], f"🚨 *NIKE ALERT*: Athlete {sender_phone} is nearby {best['name']}.")
            time.sleep(1)
            send_whatsapp_message(sender_phone, reply)
            return {"status": "redirected"}

    # --- 3. AI CHAT FLOW ---
    if not sender_phone or not user_text: return {"status": "ignored"}

    try:
        # Check if Human is in control
        last_interaction = chat_history.find_one({"phone_number": sender_phone}, sort=[("_id", -1)])
        if last_interaction and last_interaction.get("is_handled"):
            print(f"🚫 AI SILENCED for {sender_phone} (Manager Active)", flush=True)
            return {"status": "human_active"}

        # Fetch History & Generate AI Response
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5))
        history.reverse()
        messages = [{"role": "system", "content": "You are the Nike Digital Manager. Tone: Athletic/Premium. Refer to users as 'Athletes'. End with 'Just Do It.'"}]
        for doc in history:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        # Save to DB with metadata
        chat_history.insert_one({
            "phone_number": sender_phone, "user_msg": user_text, "ai_reply": ai_reply,
            "timestamp": datetime.utcnow(), "is_handled": False
        })
        send_whatsapp_message(sender_phone, ai_reply)

    except Exception as e:
        print(f"Error: {e}", flush=True)

    return {"status": "success"}

@app.get("/")
def read_root(): return {"status": "OnGround.ai Hybrid Enterprise Engine Online"}
