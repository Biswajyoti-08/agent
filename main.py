import os
import requests
import certifi
import math
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

STORES = [
    {
        "id": "NIKE_001",
        "name": "Nike Flagship - Brigade Road",
        "lat": 12.9719, "lon": 77.6070,
        "manager_phone": "919437725393", 
        "open": "09:00", "close": "22:00",
        "load": 0.3 # Capacity: 30% full
    },
    {
        "id": "NIKE_002",
        "name": "Nike Hub - Indiranagar",
        "lat": 12.9784, "lon": 77.6408,
        "manager_phone": "91XXXXXXXXXX", 
        "open": "10:00", "close": "21:30",
        "load": 0.5 # Capacity: 50% full
    }
]

# --- UTILS: Haversine Distance Logic ---
def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 # Earth radius in KM
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def find_best_store(u_lat, u_lon):
    now = datetime.now().strftime("%H:%M")
    valid_stores = []
    for s in STORES:
        is_open = s["open"] <= now <= s["close"]
        is_available = s["load"] < 0.85 # Redirect if store is > 85% busy
        if is_open and is_available:
            s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
            valid_stores.append(s)
    
    return min(valid_stores, key=lambda x: x["dist"]) if valid_stores else None

def send_whatsapp_message(phone_number: str, text: str):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {
        "message": {
            "phone_number": phone_number, 
            "content": text, 
            "message_type": "text"
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    print(f"Message to {phone_number} | Status: {response.status_code}", flush=True)

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    print(f"DEBUG: Payload Received: {payload}", flush=True)
    
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")

    # --- FEATURE: LOCATION SHARED (Nike Hyper-Local Routing) ---
    location = message_data.get("location")
    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        best = find_best_store(u_lat, u_lon)
        
        if best:
            # Step 3: Smooth Google Maps Link
            maps_url = f"https://www.google.com/maps?q={best['lat']},{best['lon']}"
            
            reply = (f"👟 *Athlete, great news!* \n\n"
                     f"Our *{best['name']}* is just {round(best['dist'], 1)}km away and has an advisor ready for your Trial Run. \n\n"
                     f"📍 *Get directions here:* {maps_url} \n\n"
                     f"Should I notify the Store Manager you're on your way? Just Do It.")
            
            # ALERT MANAGER (Handoff Alert)
            manager_msg = f"🚨 *NIKE ONGROUND ALERT*: Athlete {sender_phone} is nearby and heading to {best['name']}. Please prepare for their visit! 🏁"
            send_whatsapp_message(best["manager_phone"], manager_msg)
        else:
            reply = "I couldn't find a Nike Hub currently available nearby. Would you like me to schedule a virtual concierge call?"
        
        send_whatsapp_message(sender_phone, reply)
        return {"status": "redirect_success"}

    # --- STANDARD CHAT FLOW (Nike Persona) ---
    text_data = message_data.get("text", {})
    user_text = text_data.get("body", "")
    if not sender_phone or not user_text: return {"status": "ignored"}

    try:
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5))
        history.reverse()

        # Step 2: Branded System Prompt
        messages = [{
            "role": "system", 
            "content": """You are the 'OnGround.ai' Digital Manager for Nike India. 
            Your tone is athletic, premium, and professional. 
            - Refer to customers as 'Athletes'. 
            - If they ask about shoes (Air Max, Jordan, etc.) or visiting a store, answer briefly and say 'The best way to find your perfect fit is a Trial Run at our store'.
            - ALWAYS nudge them to 'Share Location' so you can check live inventory at the nearest Nike Hub.
            - End interactions with 'Just Do It.'"""
        }]
        
        for doc in history:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        # Save to DB with metadata for Problem 2 (Retention)
        chat_history.insert_one({
            "phone_number": sender_phone, 
            "user_msg": user_text, 
            "ai_reply": ai_reply,
            "timestamp": datetime.utcnow(), 
            "is_handled": False, # Will be used by the Watchdog script
            "brand": "Nike"
        })
        
        send_whatsapp_message(sender_phone, ai_reply)

    except Exception as e:
        print(f"Error: {e}", flush=True)

    return {"status": "success"}

@app.get("/")
def read_root(): return {"status": "OnGround.ai Nike Manager Online"}
