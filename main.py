import os
from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq

app = FastAPI()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    print(f"Received message from {From}: {Body}")
    
    try:
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful, concise AI live agent communicating over WhatsApp. Keep your answers brief, friendly, and use emojis when appropriate."
                },
                {
                    "role": "user",
                    "content": Body,
                }
            ],
            model="llama3-8b-8192",
        )
        ai_reply = chat_completion.choices[0].message.content
        
    except Exception as e:
        print(f"Groq Error: {e}")
        ai_reply = "Oops! My AI brain hit a slight glitch. Could you repeat that?"

    twiml_response = MessagingResponse()
    twiml_response.message(ai_reply)

    # FIX: Send back explicitly as an XML media type so Twilio hides the code!
    return Response(content=str(twiml_response), media_type="application/xml")

@app.get("/")
def read_root():
    return {"status": "Live Agent Webhook is Online!"}
