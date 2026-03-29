import os
import requests
import certifi
import math
from datetime import datetime
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")

mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]
chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 # Earth radius in km
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_kapso_text(phone, text):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    requests.post(url, headers=headers, json=payload)

def send_kapso_interactive(phone, body_text, buttons):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {
        "message": {
            "phone_number": phone,
            "message_type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": buttons}
            }
        }
    }
    requests.post(url, headers=headers, json=payload)

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")
    recipient_phone = message_data.get("to") 

    brand = brands_col.find_one({"brand_phone": recipient_phone})
    if not brand: 
        brand = brands_col.find_one({"brand_id": "NIKE_IND"}) 

    user_text = message_data.get("text", {}).get("body", "")
    
    if not user_text:
        user_text = message_data.get("audio", {}).get("transcription", "")
        if user_text:
            print(f"🎙️ Voice Note Received from {sender_phone}: {user_text}", flush=True)

    user_text = user_text.strip()
    user_text_low = user_text.lower()
    location = message_data.get("location")

    if not user_text and not location:
        return {"status": "ignored_unsupported_media"}

    button_id = message_data.get("interactive", {}).get("button_reply", {}).get("id")
    
    if button_id == "claim_lead" or user_text_low == brand.get("claim_command"):
        chat_history.update_many({"phone_number": sender_phone}, {"$set": {"is_handled": True}})
        send_kapso_text(sender_phone, f"✅ *System:* {brand['brand_name']} Agent is now handling this request.")
        return {"status": "claimed"}

    if user_text_low == brand.get("release_command"):
        chat_history.update_many({"phone_number": sender_phone}, {"$set": {"is_handled": False}})
        send_kapso_text(sender_phone, f"🤖 *System:* {brand['brand_name']} AI resumed. {brand['signature']}")
        return {"status": "released"}

    last_doc = chat_history.find_one({"phone_number": sender_phone}, sort=[("_id", -1)])
    if last_doc and last_doc.get("is_handled") == True:
        return {"status": "human_active_silencing_ai"}

    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        stores = list(stores_col.find({"brand_id": brand["brand_id"]}))
        
        if stores:
            for s in stores: s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
            best = min(stores, key=lambda x: x["dist"])
            
            reply = f"📍 Closest {brand['brand_name']}: {best['store_name']} ({round(best['dist'], 1)}km).\n{best['maps_url']}\n{brand['signature']}"
            send_kapso_text(sender_phone, reply)
            
            alert_text = brand["manager_alert_text"].replace("{{phone}}", sender_phone).replace("{{store}}", best["store_name"])
            buttons = [{"type": "reply", "reply": {"id": "claim_lead", "title": "Claim Lead"}}]
            send_kapso_interactive(best["manager_phone"], alert_text, buttons)
            return {"status": "routed"}

    try:
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(3))
        history.reverse()

        system_prompt = (
            f"You are the {brand['brand_name']} manager. Persona: {brand['persona']}. "
            f"Goal: {brand['nudge_goal']}. Signature: {brand['signature']}"
        )
        
        messages = [{"role": "system", "content": system_prompt}]
        for doc in history:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender_phone, 
            "brand_id": brand["brand_id"],
            "user_msg": user_text, 
            "ai_reply": ai_reply,
            "timestamp": datetime.utcnow(), 
            "is_handled": False
        })
        send_kapso_text(sender_phone, ai_reply)

    except Exception as e:
        print(f"AI Generation Error: {e}")

    return {"status": "success"}

@app.get("/")
def read_root(): return {"status": "Enterprise Multi-Tenant Engine Online (Voice-Ready)"}
