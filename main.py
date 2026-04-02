import os, requests, certifi, math, re
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

# --- HELPER FUNCTIONS ---

def clean_phone(phone_str):
    """STABILITY FIX: Prevents Kapso 422 errors by ensuring raw digits only."""
    if not phone_str: return None
    return re.sub(r'\D', '', str(phone_str))

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_text(phone, text):
    """STABILITY FIX: Cleans the phone number before every API call."""
    target = clean_phone(phone)
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": target, "content": text, "message_type": "text"}}
    return requests.post(url, headers=headers, json=payload)

def check_triage(text):
    """LLM Router: Detects if a human is needed for complex/angry queries."""
    triage_prompt = f"""
    Analyze this Nike Athlete message: "{text}"
    Determine if this needs a HUMAN MANAGER.
    Criteria: Anger, Refund, Bulk Order, or Requesting a Person.
    Reply ONLY 'ESCALATE' or 'AI_HANDLE'.
    """
    check = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": triage_prompt}],
        model="llama-3.1-8b-instant",
        temperature=0 # Consistency fix
    )
    return "ESCALATE" in check.choices[0].message.content.upper()

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    try:
        payload = await request.json()
        msg_data = payload.get("message", {})
        
        # SANDBOX FIX: Detect outbound/sandbox messages
        direction = str(payload.get("direction", "")).lower()
        is_dashboard = (direction == "outbound") or (not msg_data.get("from"))
        
        sender = clean_phone(msg_data.get("from") or msg_data.get("phone_number"))
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = clean_phone(brand.get("manager_phone"))
        user_text = msg_data.get("text", {}).get("body") or ""

        # --- 1. MANAGER TRACKING & SILENCING ---
        if (sender == mgr_phone) or is_dashboard:
            customer_phone = clean_phone(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            if customer_phone and user_text:
                chat_history.insert_one({
                    "phone_number": customer_phone,
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
                # Re-sync all records for this user to be active
                chat_history.update_many({"phone_number": customer_phone}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            return {"status": "manager_active"}

        # --- 2. INTELLIGENT TRIAGE ---
        if user_text and check_triage(user_text):
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            alert = f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}"
            send_text(mgr_phone, alert)
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated"}

        # --- 3. CHECK AI SILENCE STATUS (30-Min Window) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=30):
                # Reset timer on user reply to keep AI silent
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                return {"status": "ai_muted"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                chat_history.insert_one({
                    "phone_number": sender, "user_msg": "[Location Pin]", 
                    "ai_reply": f"SYSTEM: Found {best.get('store_name')}", "goal_reached": True, "timestamp": datetime.utcnow()
                })
                response = f"📍 *Nearest Nike Store Found!*\n\nName: {best.get('store_name')}\nDistance: {round(best['dist'], 1)} km\n\n{brand.get('signature', 'Just Do It.')}"
                send_text(sender, response)
                send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is visiting {best.get('store_name')}.")
                return {"status": "routed"}

        # --- 5. AI ENGINE ---
        if not user_text: return {"status": "ignore"}
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        instruction = "You are a professional Nike Concierge. Max 2 paragraphs. "
        if goal_reached:
            instruction += "Athlete found a store. Be helpful with gear, but DON'T ask for location."
        else:
            instruction += "If they want a store, guide them to share their GPS location pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Manager previously said]: {doc.get('manager_msg')}"})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", max_tokens=300)
        ai_reply = completion.choices[0].message.content
        chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()})
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error_handled"}
    return {"status": "success"}

@app.get("/")
def home(): return {"status": "Nike Retail AI OS - Stability Build Active"}