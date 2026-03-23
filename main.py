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

# 1. Initialize API Clients
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")

# 2. MongoDB Setup
mongo_uri = os.environ.get("MONGO_URI")
mongo_client = MongoClient(mongo_uri, tlsCAFile=certifi.where()) 
db = mongo_client["WhatsAppAgent"]
chat_history = db["ChatHistory"]

# --- DUMMY DATA: Nike Store Registry ---
# Ensure BOTH manager_phone fields are updated to avoid the 422 error!
STORES = [
    {
        "id": "NIKE_001",
        "name": "Nike Flagship - Brigade Road",
        "lat": 12.9719, "lon": 77.6070,
        "manager_phone": "919437725393", 
        "open": "09:00", "close": "22:00",
        "load": 0.3 
    },
    {
        "id": "NIKE_002",
        "name": "Nike Hub - Indiranagar",
        "lat": 12.9784, "lon": 77.6408,
        "manager_phone": "919437725393", # Updated to your real number
        "open": "10:00", "close": "21:30",
        "load": 0.5 
    }
]

# --- UTILS: Haversine Distance Logic ---
def get_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def find_best_store(u_lat, u_lon):
    now = datetime.now().strftime("%H:%M")
    valid_stores = []
    for s in STORES:
        if s["open"] <= now <= s["close"] and s["load"] < 0.85:
            s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
            valid_stores.append(s)
    return min(valid_stores, key=lambda x: x["dist"]) if valid_stores else None

def send_whatsapp_message(phone_number: str, text: str):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone_number, "content": text, "message_type": "text"}}
    response = requests.post(url, headers=headers, json=payload)
    print(f"To: {phone_number} | Status: {response.status_code}", flush=True)

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")
    
    text_data = message_data.get("text", {})
    user_text = text_data.get("body", "").strip()

    # --- 1. LEAD CLAIM LOGIC (Problem 2: The Safety Net) ---
    if user_text.lower() == "claim":
        chat_history.update_many(
            {"phone_number": sender_phone}, 
            {"$set": {"is_handled": True}}
        )
        send_whatsapp_message(sender_phone, "✅ *System:* Lead officially claimed. Human advisor is now in control. Watchdog disabled.")
        return {"status": "claimed"}

    # --- 2. LOCATION SHARED (Problem 1: Hyper-Local Routing) ---
    location = message_data.get("location")
    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        best = find_best_store(u_lat, u_lon)
        if best:
            maps_url = f"https://www.google.com/maps?q={best['lat']},{best['lon']}"
            reply = (f"👟 *Athlete, great news!* \n\n"
                     f"Our *{best['name']}* is just {round(best['dist'], 1)}km away. \n\n"
                     f"📍 *Get directions:* {maps_url} \n\n"
                     f"Should I notify the Manager you're on your way? Just Do It.")
            
            # ALERT MANAGER
            manager_msg = f"🚨 *NIKE ONGROUND*: Athlete {sender_phone} is nearby {best['name']}. Prepare for Trial Run! 🏁"
            send_whatsapp_message(best["manager_phone"], manager_msg)
            time.sleep(1) # Prevents Kapso rate-limiting
        else:
            reply = "I couldn't find a Nike Hub available nearby. Schedule a callback?"
        
        send_whatsapp_message(sender_phone, reply)
        return {"status": "redirect_success"}

    # --- 3. STANDARD CHAT FLOW (Nike Persona) ---
    if not sender_phone or not user_text: return {"status": "ignored"}

    try:
        # Check if the lead is already handled by a human
        last_interaction = chat_history.find_one({"phone_number": sender_phone}, sort=[("_id", -1)])
        if last_interaction and last_interaction.get("is_handled"):
            print(f"Skipping AI reply for {sender_phone}: Lead is already handled by human.", flush=True)
            return {"status": "human_active"}

        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5))
        history.reverse()

        messages = [{"role": "system", "content": "You are the 'OnGround.ai' Digital Manager for Nike India. Your tone is athletic and premium. Refer to customers as 'Athletes'. If they want gear or a visit, nudge for 'Location' to find a Nike Hub. End with 'Just Do It.'"}]
        for doc in history:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        # Save with metadata for the Watchdog (Problem 2)
        chat_history.insert_one({
            "phone_number": sender_phone, 
            "user_msg": user_text, 
            "ai_reply": ai_reply,
            "timestamp": datetime.utcnow(), 
            "is_handled": False # Reset to False so the Watchdog can track new questions
        })
        send_whatsapp_message(sender_phone, ai_reply)

    except Exception as e:
        print(f"Error: {e}", flush=True)

    return {"status": "success"}

@app.get("/")
def read_root(): return {"status": "OnGround.ai Unified Engine Online"}
