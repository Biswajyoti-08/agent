import streamlit as st
from pymongo import MongoClient
import pandas as pd
from datetime import datetime, timedelta
import os

# 1. Setup & Connection
st.set_page_config(page_title="Nike India - Control Tower", layout="wide")
MONGO_URI = os.environ.get("MONGO_URI") # Use your same URI
client = MongoClient(MONGO_URI)
db = client["EnterpriseAgent"]
chat_history = db["ChatHistory"]

st.title("👟 Nike India: Retail AI Control Tower")
st.markdown("---")

# 2. Sidebar Filters
st.sidebar.header("Filters")
time_range = st.sidebar.selectbox("Time Range", ["Last 24 Hours", "Last 7 Days", "All Time"])

# 3. Data Retrieval
def get_data():
    data = list(chat_history.find().sort("timestamp", -1))
    return pd.DataFrame(data)

df = get_data()

if not df.empty:
    # 4. KPI Metrics
    col1, col2, col3, col4 = st.columns(4)
    
    total_leads = len(df['phone_number'].unique())
    active_human = len(df[df['is_human_active'] == True]['phone_number'].unique())
    goals_met = len(df[df['goal_reached'] == True]['phone_number'].unique())
    
    col1.metric("Total Athletes", total_leads)
    col2.metric("Goals Reached (Store Found)", goals_met)
    col3.metric("Human Handover Active", active_human)
    col4.metric("AI-Only Chats", total_leads - active_human)

    st.markdown("### 🚨 High-Priority Lead Decay")
    st.info("These Athletes found a store but haven't been contacted by a Manager yet.")
    
    # Logic: Goal Reached is True, but Human hasn't taken over
    decay_leads = df[(df['goal_reached'] == True) & (df['is_human_active'] != True)]
    
    if not decay_leads.empty:
        # Displaying specific columns for the manager
        display_df = decay_leads[['phone_number', 'timestamp', 'ai_reply']].copy()
        display_df.columns = ['Athlete Phone', 'Last Interaction', 'AI Last Action']
        st.table(display_df.head(10))
    else:
        st.success("All high-intent leads are being handled! No decay detected.")

    st.markdown("### 📋 Recent Activity Feed")
    st.dataframe(df[['timestamp', 'phone_number', 'user_msg', 'ai_reply', 'is_human_active']].head(20), use_container_width=True)

else:
    st.warning("No chat data found in MongoDB. Start a conversation to see data!")

# Auto-refresh logic (every 30 seconds)
# st.empty()
# time.sleep(30)
# st.rerun()
