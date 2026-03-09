import os
from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
from groq import Groq

# Initialize the FastAPI app
app = FastAPI()

# Initialize the Groq Client
# We will securely set this API key in Render later, so it's not hardcoded!
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

@app.post("/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    """
    Twilio hits this endpoint whenever someone sends a WhatsApp message.
    Body = The text the user sent.
    From = The user's WhatsApp number.
    """
    print(f"Received message from {From}: {Body}")
    
    try:
        # 1. Ask the AI (Groq Llama 3)
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
            model="llama3-8b-8192", # Extremely fast model
        )
        
        # Extract the AI's text response
        ai_reply = chat_completion.choices[0].message.content
        
    except Exception as e:
        print(f"Groq Error: {e}")
        ai_reply = "Oops! My AI brain hit a slight glitch. Could you repeat that?"

    # 2. Build the Twilio XML Response (TwiML)
    twiml_response = MessagingResponse()
    twiml_response.message(ai_reply)

    # 3. Send it back to WhatsApp
    return str(twiml_response)

# Optional: A simple health check route just to verify the server is running
@app.get("/")
def read_root():
    return {"status": "Live Agent Webhook is Online!"}