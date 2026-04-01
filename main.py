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

# --- HELPER FUNCTIONS ---

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

def check_triage(text):
    """LLM Router: Detects if a human is needed for complex/angry queries."""
    triage_prompt = f"""
    Analyze this Nike Athlete message: "{text}"
    Determine if this needs a HUMAN MANAGER.
    Criteria for ESCALATION:
    1. Anger or extreme frustration.
    2. Explicit request for a human/person/manager.
    3. Complex issues: Bulk orders, manufacturing defects, or refund disputes.
    
    Reply ONLY with 'ESCALATE' or 'AI_HANDLE'.
    """
    check = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": triage_prompt}],
        model="llama-3.1-8b-instant"
    )
    return "ESCALATE" in check.choices[0].message.content

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    try:
        payload = await request.json()
        msg_data = payload.get("message", {})
        sender = msg_data.get("from") or msg_data.get("phone_number")
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = brand.get("manager_phone")
        user_text = msg_data.get("text", {}).get("body") or ""

        # --- 1. MANAGER TRACKING & SILENCING ---
        if sender == mgr_phone:
            customer_phone = msg_data.get("to")
            if customer_phone:
                # Log Manager's message for Dashboard Transcript Audit
                chat_history.insert_one({
                    "phone_number": customer_phone,
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
            return {"status": "manager_logged_ai_silenced"}

        # --- 2. INTELLIGENT TRIAGE (Complex Intent Detection) ---
        if user_text and check_triage(user_text):
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            alert = f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}"
            send_text(mgr_phone, alert)
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated_to_human"}

        # --- 3. CHECK AI SILENCE STATUS (TTL Logic) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=30):
                return {"status": "ai_muted_human_is_handling"}
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
                    "phone_number": sender, 
                    "user_msg": "[Shared Location Pin]", 
                    "ai_reply": f"SYSTEM_NOTE: Found {best.get('store_name')}. Goal achieved.",
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
                if mgr_phone:
                    send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is heading to {best.get('store_name')}.")
                return {"status": "routed"}

        # --- 5. NATURAL LANGUAGE AI ENGINE ---
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        instruction = "You are a professional Nike Concierge. Avoid robotic scripts. "
        if goal_reached:
            instruction += "The Athlete found a store. Be helpful with gear/stock questions, but DON'T ask for location."
        else:
            instruction += "If they want a store, guide them to click (📎 > Location) to share their 'Current Location' pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')} Signature: {brand.get('signature')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error_handled"}
    return {"status": "success"}

@app.get("/")
def home(): return {"status": "Retail AI OS Online - Intelligence Active"}