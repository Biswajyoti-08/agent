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
    """Calculates km distance between two points for store routing"""
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_kapso_text(phone, text):
    """Standard text message sender"""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    requests.post(url, headers=headers, json=payload)

def generate_manager_summary(history, brand_name):
    """MODIFICATION 3: Generates a 3-bullet point brief for the manager to reduce friction"""
    text_history = "\n".join([f"User: {h['user_msg']}\nAI: {h['ai_reply']}" for h in history])
    prompt = f"You are a retail expert. Summarize this {brand_name} customer's intent in exactly 3 short bullet points. Focus on: Product, Urgency, and Location preference."
    
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": f"{prompt}\n\nChat History:\n{text_history}"}],
            model="llama-3.1-8b-instant"
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Summary Error: {e}")
        return "• Error generating summary. Please check history manually."

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    message_data = payload.get("message", {})
    sender_phone = message_data.get("from") or message_data.get("phone_number")
    recipient_phone = message_data.get("to")

    brand = brands_col.find_one({"brand_phone": recipient_phone})
    if not brand: brand = brands_col.find_one({"brand_id": "NIKE_IND"})

    interactive = message_data.get("interactive", {})
    button_id = interactive.get("button_reply", {}).get("id")

    if button_id in ["slot_11am", "slot_5pm"]:
        slot_time = "11:00 AM" if button_id == "slot_11am" else "5:00 PM"
        confirm_msg = f"✅ *Slot Confirmed!*\nWe have reserved your VIP visit for tomorrow at {slot_time}. {brand['signature']}"
        send_kapso_text(sender_phone, confirm_msg)
        
        send_kapso_text(brand["manager_phone"], f"📅 *NEW APPOINTMENT*: Lead {sender_phone} booked for {slot_time} tomorrow.")
        return {"status": "slot_booked"}

    user_text = message_data.get("text", {}).get("body", "")
    if not user_text:
        user_text = message_data.get("audio", {}).get("transcription", "")
    
    user_text = user_text.strip()
    user_text_low = user_text.lower()
    location = message_data.get("location")

    if button_id == "claim_lead" or user_text_low == brand.get("claim_command"):
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5))
        history.reverse()
        
        summary = generate_manager_summary(history, brand["brand_name"])
        
        chat_history.update_many({"phone_number": sender_phone}, {"$set": {"is_handled": True}})
        
        briefing = f"📋 *AI LEAD BRIEFING*:\n{summary}\n\n✅ *Action:* Human takeover complete. Start chatting now."
        send_kapso_text(brand["manager_phone"], briefing)
        return {"status": "claimed_with_briefing"}

    last_doc = chat_history.find_one({"phone_number": sender_phone}, sort=[("_id", -1)])
    if last_doc and last_doc.get("is_handled") == True:
        return {"status": "human_active"}

    if location:
        u_lat, u_lon = location.get("latitude"), location.get("longitude")
        stores = list(stores_col.find({"brand_id": brand["brand_id"]}))
        
        if stores:
            for s in stores: s["dist"] = get_distance(u_lat, u_lon, s["lat"], s["lon"])
            best = min(stores, key=lambda x: x["dist"])
            
            # A. Send Store Info
            reply = f"📍 Nearest {brand['brand_name']}: {best['store_name']} ({round(best['dist'], 1)}km away).\n{best['maps_url']}\n{brand['signature']}"
            send_kapso_text(sender_phone, reply)
            
            url = "https://app.kapso.ai/api/v1/whatsapp_messages"
            headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
            payload = {
                "message": {
                    "phone_number": sender_phone,
                    "message_type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": "Would you like to book a VIP Slot for a priority trial at this store?"},
                        "action": {
                            "buttons": [
                                {"type": "reply", "reply": {"id": "slot_11am", "title": "Tomorrow 11 AM"}},
                                {"type": "reply", "reply": {"id": "slot_5pm", "title": "Tomorrow 5 PM"}}
                            ]
                        }
                    }
                }
            }
            requests.post(url, headers=headers, json=payload)
            
            alert_text = brand["manager_alert_text"].replace("{{phone}}", sender_phone).replace("{{store}}", best["store_name"])
            manager_payload = {
                "message": {
                    "phone_number": best["manager_phone"], 
                    "message_type": "interactive", 
                    "interactive": {
                        "type": "button", 
                        "body": {"text": alert_text}, 
                        "action": {"buttons": [{"type": "reply", "reply": {"id": "claim_lead", "title": "Claim Lead"}}]}}
                }
            }
            requests.post(url, headers=headers, json=manager_payload)
            return {"status": "routed"}

    try:
        history = list(chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(3))
        history.reverse()
        
        system_prompt = f"You are the {brand['brand_name']} manager. Persona: {brand['persona']}. Goal: {brand['nudge_goal']}. Signature: {brand['signature']}"
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
        print(f"AI Engine Error: {e}")

    return {"status": "success"}

@app.get("/")
def read_root(): 
    return {"status": "Enterprise AI Retail OS Online", "timestamp": datetime.now().isoformat()}
