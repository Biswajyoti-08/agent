import os, requests, certifi, math
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Clients & DB Setup
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
    """LLM Router: Silently detects if a human manager is needed."""
    triage_prompt = f"""
    Analyze this Nike Athlete message: "{text}"
    Determine if this needs a HUMAN MANAGER.
    Criteria: Anger, explicit human request, or complex issues (Bulk, Defect, Refund).
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

        # --- 1. MANAGER TRACKING (Now with String Casting for Streamlit) ---
        if sender == mgr_phone:
            # Capturing the athlete's number from the 'to' field in Kapso
            customer_phone = msg_data.get("to") or msg_data.get("recipient_id")
            if customer_phone:
                chat_history.insert_one({
                    "phone_number": str(customer_phone), # String cast for DB consistency
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
            return {"status": "manager_logged"}

        # --- 2. STEALTH TRIAGE (Escalates without telling the user) ---
        if user_text and check_triage(user_text):
            chat_history.update_many(
                {"phone_number": str(sender)}, 
                {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
            )
            alert = f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}"
            send_text(mgr_phone, alert)
            # We don't send a text to the user here to keep it 'Stealth'

        # --- 3. CHECK AI SILENCE STATUS (Refined to 5-Minute Window) ---
        user_state = chat_history.find_one({"phone_number": str(sender)}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            # If manager spoke in last 5 mins, AI stays silent
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=5):
                return {"status": "ai_muted_human_is_handling"}
            else:
                # 5 mins passed, AI regains control automatically
                chat_history.update_many({"phone_number": str(sender)}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                chat_history.insert_one({
                    "phone_number": str(sender), 
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
        if not user_text: return {"status": "ignore"}
        
        history = list(chat_history.find({"phone_number": str(sender)}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        instruction = "You are a professional Nike Concierge. Use natural, athletic language. "
        if goal_reached:
            instruction += "Athlete found a store. Answer gear questions but DON'T ask for location."
        else:
            instruction += "If they want a store, guide them to click (📎 > Location) to share their pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')} Signature: {brand.get('signature')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant")
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": str(sender), "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error_handled"}
    return {"status": "success"}

@app.get("/")
def home(): return {"status": "Retail AI OS Online - Stealth Triage Active"}