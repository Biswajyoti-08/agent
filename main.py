import os, requests, certifi, math
from datetime import datetime
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

        # --- 2. LOCATION LOGIC (With Memory Bridge) ---
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                # Update AI Memory so it doesn't ask for location again
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": "[Shared Location Pin]", 
                    "ai_reply": f"SYSTEM_NOTE: Successfully guided user to {best.get('store_name')}.",
                    "goal_reached": True,
                    "timestamp": datetime.utcnow()
                })

                response = (
                    f"📍 *Nearest {brand.get('brand_name')} Store Found!*\n\n"
                    f"Name: {best.get('store_name')}\n"
                    f"Distance: {round(best['dist'], 1)} km\n"
                    f"Get Directions: {best.get('maps_url')}\n\n"
                    f"I'm here if you need anything else. {brand.get('signature', 'Just Do It.')}"
                )
                send_text(sender, response)

                mgr_phone = brand.get("manager_phone")
                if mgr_phone:
                    alert = f"🚨 *NEW LEAD*: {sender} is heading to {best.get('store_name')}. Check in if needed!"
                    send_text(mgr_phone, alert)
                
                return {"status": "routed"}

        # --- 3. AI CONCIERGE ENGINE (Context Aware) ---
        user_text = msg_data.get("text", {}).get("body") or ""
        if not user_text: return {"status": "ignore"}

        # Fetch history to check if goal was reached
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        # Dynamic System Prompt
        instruction = "Be a helpful Nike manager. "
        if goal_reached:
            instruction += "The user has already found the store. Be polite, say you're welcome, and ask if they need anything else. Do NOT ask for location."
        else:
            instruction += "If they want a store, tell them to share their 'Current Location' using the (📎 > Location) icon."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')} Signature: {brand.get('signature')}"}]
        for doc in history:
            messages.append({"role": "user", "content": doc.get("user_msg")})
            messages.append({"role": "assistant", "content": doc.get("ai_reply")})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, 
            "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error_handled"}

    return {"status": "success"}

@app.get("/")
def home(): return {"status": "Retail AI OS Online"}