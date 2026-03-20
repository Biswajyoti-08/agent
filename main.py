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

# --- DUMMY DATA: Enriched Store Registry (Problem 1) ---
STORES = [
    {
        "id": "ST_001",
        "name": "Jayanagar Showroom",
        "lat": 12.9307, "lon": 77.5838,
        "manager_phone": "9194377XXXXX", 
        "open": "09:00", "close": "20:00",
        "load": 0.4 
    },
    {
        "id": "ST_002",
        "name": "Indiranagar Hub",
        "lat": 12.9719, "lon": 77.6412,
        "manager_phone": "9188223XXXXX",
        "open": "10:00", "close": "21:00",
        "load": 0.9 
    }
]

# --- UTILS: Distance & Routing Logic ---
def get_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def find_best_store(u_lat, u_lon):
    now = datetime.now().strftime("%H:%M")
    print(f"Checking stores at current time: {now}", flush=True)
    valid_stores = []
    for s in STORES:
        # Debugging store status
        is_open = s["open"] <= now <= s["close"]
        is_not_busy = s["load"] < 0.85
        if is_open and is_not_busy:
            s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
            valid_stores.append(s)
            print(f"Store {s['name']} is valid. Distance: {s['dist']}km", flush=True)
        else:
            print(f"Store {s['name']} skipped. Open: {is_open}, Not Busy: {is_not_busy}", flush=True)
            
    return min(valid_stores, key=lambda x: x["dist"]) if valid_stores else None

def send_whatsapp_message(phone_number: str, text: str):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone_number, "content": text, "message_type": "text"}}
    resp = requests.post(url, headers=headers, json=payload)
    print(f"Message sent to {phone_number}. Status: {resp.status_code}", flush=True)

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    print(f"Received Webhook Payload: {payload}", flush=True)
    
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")

    # --- FEATURE: LOCATION SHARED (Problem 1 Execution) ---
    location = message_data.get("location")
    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        print(f"Location detected: {u_lat}, {u_lon} from {sender_phone}", flush=True)
        
        best = find_best_store(u_lat, u_lon)
        
        if best:
            reply = f"📍 Great news! Our {best['name']} is just {round(best['dist'], 1)}km away and has an advisor ready for you. Should I book your VIP slot?"
            print(f"Best store found: {best['name']}. Notifying manager...", flush=True)
            
            # ALERT MANAGER
            send_whatsapp_message(best["manager_phone"], f"🚨 REDIRECT ALERT: Customer {sender_phone} is nearby and looking to visit {best['name']}. Please be ready.")
        else:
            reply = "I couldn't find a nearby showroom currently available. Would you like me to schedule a callback?"
            print("No valid stores found.", flush=True)
        
        send_whatsapp_message(sender_phone, reply)
        return {"status": "redirect_success"}

    # --- STANDARD CHAT FLOW ---
    text_data = message_data.get("text", {})
    user_text = text_data.get("body", "")
    if not sender_phone or not user_text: 
        print("Ignoring message: No phone or text body.", flush=True)
        return {"status": "ignored"}

    print(f"Processing text message: {user_text}", flush=True)

    try:
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5))
        history.reverse()

        messages = [{"role": "system", "content": "You are a Showroom AI Agent. If a user wants to visit, see a car, or take a test drive, kindly ask them to 'Share their Location' so you can find the nearest available branch."}]
        
        for doc in history:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({"phone_number": sender_phone, "user_msg": user_text, "ai_reply": ai_reply})
        send_whatsapp_message(sender_phone, ai_reply)

    except Exception as e:
        print(f"Error in chat flow: {e}", flush=True)

    return {"status": "success"}

@app.get("/")
def read_root(): return {"status": "Manager Persona Agent Online"}
