import os, requests, certifi, math
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Clients & DB
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]
chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_text(phone, text):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    return requests.post(url, headers=headers, json=payload)

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    try:
        payload = await request.json()
        msg_data = payload.get("message", {})
        sender = msg_data.get("from") or msg_data.get("phone_number")
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = brand.get("manager_phone")

        # --- 1. SEAMLESS HANDOFF (Manager Detection) ---
        if sender == mgr_phone:
            # When manager speaks, we identify which customer they are talking to
            # This logic assumes the manager is replying to a specific thread
            customer_phone = msg_data.get("to") 
            chat_history.update_many(
                {"phone_number": customer_phone},
                {"$set": {"is_human_active": True, "last_human_ts": datetime.utcnow()}}
            )
            print(f"Manager in control for {customer_phone}. AI Silenced.")
            return {"status": "manager_speaking_ai_silenced"}

        # --- 2. VOICE & TEXT INTEGRATION ---
        # Prioritize text, fallback to Kapso's voice transcription
        user_text = msg_data.get("text", {}).get("body")
        if not user_text:
            user_text = msg_data.get("audio", {}).get("transcription")

        location = msg_data.get("location")
        if not user_text and not location:
            return {"status": "ignore_empty_payload"}

        # --- 3. SILENCE CHECK (Auto-Release Logic) ---
        current_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if current_state and current_state.get("is_human_active"):
            last_ts = current_state.get("last_human_ts")
            # If manager spoke in the last 30 mins, AI stays silent
            if last_ts and (datetime.utcnow() - last_ts) < timedelta(minutes=30):
                print(f"AI Silent: Human is handling {sender}")
                return {"status": "human_in_control"}
            else:
                # Auto-release if manager hasn't spoken for 30 mins
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC (With Memory Bridge) ---
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": "[Location Shared]", 
                    "ai_reply": f"SYSTEM_NOTE: Guided user to {best.get('store_name')}.",
                    "goal_reached": True,
                    "timestamp": datetime.utcnow(),
                    "is_human_active": False
                })

                response = (
                    f"📍 *Nearest {brand.get('brand_name')} Store Found!*\n\n"
                    f"Name: {best.get('store_name')}\n"
                    f"Distance: {round(best['dist'], 1)} km\n"
                    f"Get Directions: {best.get('maps_url')}\n\n"
                    f"I'm here if you need anything else. {brand.get('signature', 'Just Do It.')}"
                )
                send_text(sender, response)

                if mgr_phone:
                    alert = f"🚨 *NEW LEAD*: {sender} is heading to {best.get('store_name')}. Jump in to assist!"
                    send_text(mgr_phone, alert)
                
                return {"status": "routed"}

        # --- 5. AI CONCIERGE ENGINE ---
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        instruction = "You are a professional Nike Manager. "
        if goal_reached:
            instruction += "The Athlete has found the store. Be polite and helpful. Do NOT ask for location again."
        else:
            instruction += "If they need a store, ask them to click (📎 > Location) to share their 'Current Location' pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')} Signature: {brand.get('signature')}"}]
        for doc in history:
            messages.append({"role": "user", "content": doc.get("user_msg")})
            messages.append({"role": "assistant", "content": doc.get("ai_reply")})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, 
            "timestamp": datetime.utcnow(), "is_human_active": False
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error_handled"}

    return {"status": "success"}

@app.get("/")
def home(): return {"status": "Retail AI OS Online - Seamless Handoff Active"}