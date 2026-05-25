import os
import sqlite3
import uuid
import random
import logging
from datetime import datetime
import streamlit as st
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from werkzeug.security import generate_password_hash

from src.helper import create_vector_store, retrieve_relevant_context
from src.prompt import MEDICAL_SYSTEM_PROMPT, MEDICAL_ASSISTANT_PROMPT, DISCLAIMER_TEXT, WELCOME_MESSAGE

# Setup logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Bridge Streamlit Secrets to environment variables for helpers
try:
    for key, value in st.secrets.items():
        os.environ[key] = str(value)
except Exception:
    pass

# Database Helper Functions
DB_PATH = "medical_users.db"

def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                username TEXT UNIQUE, 
                password TEXT, 
                email TEXT UNIQUE, 
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            try: conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
            except Exception: pass
            try: conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
            except Exception: pass
            
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY, 
                user_id INTEGER, 
                title TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id))''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                session_id TEXT, 
                role TEXT, 
                content TEXT, 
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE)''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS otp_store (
                email TEXT PRIMARY KEY,
                otp_hash TEXT,
                expires_at TIMESTAMP,
                attempts INTEGER DEFAULT 0
            )''')
            
            # Indexing for performance
            conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_sess_user ON chat_sessions(user_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_msg_sess ON chat_messages(session_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
            
            # Create a default admin user if it doesn't exist
            # Password for default admin: MedAIAdmin2025!
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE username='admin'")
            if not cursor.fetchone():
                admin_pw_hash = generate_password_hash("MedAIAdmin2025!")
                cursor.execute("INSERT INTO users (username, password, email, name, role) VALUES (?, ?, ?, ?, ?)",
                             ('admin', admin_pw_hash, 'admin@medai.com', 'System Admin', 'admin'))
                conn.commit()
                logger.info("Default admin account created: admin / MedAIAdmin2025!")
            
            # Create a default guest user if it doesn't exist
            cursor.execute("SELECT id FROM users WHERE username='guest'")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO users (username, password, email, name, role) VALUES (?, ?, ?, ?, ?)",
                             ('guest', 'guest_pass_hash', 'guest@medai.com', 'Guest User', 'user'))
                conn.commit()
                logger.info("Default guest account created.")

            logger.info("Streamlit local DB successfully initialized.")
    except Exception as e:
        logger.error(f"DB Init Failed in Streamlit: {e}")

# Run DB initialization at start
init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_guest_user_id():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE username='guest'")
            row = cursor.fetchone()
            if row:
                return row['id']
            else:
                # Create guest user if missing
                cursor.execute("INSERT INTO users (username, password, email, name, role) VALUES (?, ?, ?, ?, ?)",
                             ('guest', 'guest_pass_hash', 'guest@medai.com', 'Guest User', 'user'))
                conn.commit()
                return cursor.lastrowid
    except Exception as e:
        logger.error(f"Error fetching/creating guest user ID: {e}")
        return None

def build_messages(user_message, context, chat_history):
    prompt = MEDICAL_ASSISTANT_PROMPT.format(
        context=context if context else "No vector documents available - rely on highly accurate medical knowledge.",
        chat_history=chat_history if chat_history else "No previous conversation.",
        question=user_message
    )
    return [SystemMessage(content=MEDICAL_SYSTEM_PROMPT), HumanMessage(content=prompt)]

def get_users():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username, name, role FROM users")
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return []

def get_user_sessions(user_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, updated_at FROM chat_sessions WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching sessions: {e}")
        return []

def create_session(session_id, user_id, title):
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO chat_sessions (id, user_id, title) VALUES (?, ?, ?)", (session_id, user_id, title))
            conn.commit()
    except Exception as e:
        logger.error(f"Error creating session: {e}")

def rename_session(session_id, title):
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE chat_sessions SET title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (title, session_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Error renaming session: {e}")

def delete_session(session_id):
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Error deleting session: {e}")

def get_session_messages(session_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, role, content, timestamp FROM chat_messages WHERE session_id=? ORDER BY timestamp ASC", (session_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        return []

def save_chat_message(session_id, role, content):
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)", (session_id, role, content))
            conn.execute("UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Error saving message: {e}")

# Service Lazy Loaders
@st.cache_resource
def load_llm():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        return ChatGroq(
            temperature=0.3, 
            max_tokens=2048, 
            groq_api_key=api_key, 
            model_name="llama-3.1-8b-instant", 
            streaming=True
        )
    except Exception as e:
        logger.error(f"Error initializing LLM: {e}")
        return None

@st.cache_resource
def load_vector_store():
    try:
        return create_vector_store()
    except Exception as e:
        logger.error(f"Error initializing Vector Store: {e}")
        return None

# Centralized Theme configuration - ensures identical colors & styles locally and in production
THEME = {
    "PRIMARY_COLOR": "#3b82f6",          # Sleek blue accent
    "BG_COLOR": "#0d1117",               # Dark background
    "CARD_BG": "#161b22",                # Darker card/component background
    "BORDER_COLOR": "#21262d",           # Border gray
    "TEXT_COLOR": "#c9d1d9",             # Light gray body text
    "ACCENT_COLOR": "#1e40af",           # Deep blue hover/header gradient
    "SUCCESS_COLOR": "#10b981",          # Green for active states
    "ERROR_COLOR": "#ef4444",            # Red for error states
    "WARNING_COLOR": "#f59e0b",          # Yellow for warnings
    "FONT_FAMILY": "'Outfit', 'Inter', sans-serif"
}

# Page Setup - Enforces layout="wide" to occupy screen optimally
st.set_page_config(
    page_title="MedAI - Medical AI Assistant",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Premium Styling - locks page sizes, paddings, and styles to prevent layout shifts
st.markdown(f"""
<style>
    /* Google Fonts Import */
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap');
    
    /* 1. Global Font and Layout Reset to avoid theme engine overriding styles */
    html, body, [class*="css"], .stApp {{
        font-family: {THEME["FONT_FAMILY"]};
        background-color: {THEME["BG_COLOR"]} !important;
        color: {THEME["TEXT_COLOR"]} !important;
    }}
    
    /* 2. Lock block-container width to prevent stretching on large screens and center content */
    .block-container {{
        max-width: 1200px !important;
        padding-top: 1.5rem !important;
        padding-bottom: 4rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
        margin: 0 auto !important;
    }}
    
    /* 3. Lock Sidebar Width to prevent sizing shifts in cloud environments */
    [data-testid="stSidebar"] {{
        min-width: 320px !important;
        max-width: 320px !important;
        background-color: {THEME["CARD_BG"]} !important;
        border-right: 1px solid {THEME["BORDER_COLOR"]} !important;
    }}
    
    /* 4. Hide default Streamlit headers, options menu, and footer to clean up UI */
    header, footer, [data-testid="stHeader"] {{
        visibility: hidden !important;
        height: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
    }}
    
    /* 5. Header SaaS Card Gradient - remains identical across platforms */
    .header-container {{
        background: linear-gradient(135deg, {THEME["ACCENT_COLOR"]} 0%, #0f172a 100%);
        padding: 2rem;
        border-radius: 12px;
        margin-bottom: 1.5rem;
        border: 1px solid {THEME["ACCENT_COLOR"]};
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
        position: relative;
        overflow: hidden;
    }}
    
    .header-container::after {{
        content: '';
        position: absolute;
        top: -50%;
        left: -50%;
        width: 200%;
        height: 200%;
        background: radial-gradient(circle, rgba(59, 130, 246, 0.1) 0%, transparent 70%);
        pointer-events: none;
    }}
    
    .header-title {{
        color: #ffffff;
        font-size: 2.2rem;
        font-weight: 700;
        margin: 0;
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    
    .header-subtitle {{
        color: #93c5fd;
        font-size: 1rem;
        margin-top: 0.4rem;
        font-weight: 400;
    }}
    
    /* 6. Pulse indicator */
    .pulse-indicator {{
        display: inline-block;
        width: 10px;
        height: 10px;
        background-color: {THEME["SUCCESS_COLOR"]};
        border-radius: 50%;
        box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
        animation: pulse 1.6s infinite;
    }}
    
    @keyframes pulse {{
        0% {{
            transform: scale(0.95);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
        }}
        70% {{
            transform: scale(1);
            box-shadow: 0 0 0 6px rgba(16, 185, 129, 0);
        }}
        100% {{
            transform: scale(0.95);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0);
        }}
    }}
    
    /* 7. Status Badge styling */
    .status-badge {{
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: 500;
        display: inline-flex;
        align-items: center;
        gap: 6px;
    }}
    .status-active {{
        background-color: rgba(16, 185, 129, 0.15);
        color: #34d399;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    
    /* 8. Locked Chat Message UI - prevents dynamic height shifting on streaming */
    [data-testid="stChatMessage"] {{
        background-color: {THEME["CARD_BG"]} !important;
        border: 1px solid {THEME["BORDER_COLOR"]} !important;
        border-radius: 10px !important;
        padding: 0.85rem !important;
        margin-bottom: 0.75rem !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2) !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }}
    [data-testid="stChatMessage"]:hover {{
        border-color: {THEME["PRIMARY_COLOR"]} !important;
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.15) !important;
    }}
    
    /* 9. Stable Sidebar Button Design with hover effects */
    .stButton > button {{
        background-color: #1f2937 !important;
        color: #ffffff !important;
        border: 1px solid #374151 !important;
        border-radius: 6px !important;
        padding: 0.4rem 0.8rem !important;
        font-size: 0.9rem !important;
        font-weight: 500 !important;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
        width: 100% !important;
    }}
    .stButton > button:hover {{
        background-color: {THEME["PRIMARY_COLOR"]} !important;
        border-color: {THEME["PRIMARY_COLOR"]} !important;
        color: #ffffff !important;
        box-shadow: 0 0 8px rgba(59, 130, 246, 0.3) !important;
    }}
    
    /* 10. Alignment fix for status labels inside the sidebar */
    .status-line {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0.4rem 0;
        font-size: 0.85rem;
        color: #8b949e;
        border-bottom: 1px solid rgba(33, 38, 45, 0.5);
    }}
    .status-line:last-child {{
        border-bottom: none;
    }}
    .status-line span {{
        font-weight: 400;
    }}
    
    /* 11. Info Card structure */
    .info-card {{
        background-color: {THEME["CARD_BG"]};
        border: 1px solid {THEME["BORDER_COLOR"]};
        border-radius: 10px;
        padding: 1.1rem;
        margin-bottom: 0.85rem;
    }}
    
    /* 12. Lock Chat Input Box aesthetics */
    [data-testid="stChatInput"] {{
        background-color: transparent !important;
        padding: 0.5rem 0 !important;
    }}
    [data-testid="stChatInput"] textarea {{
        background-color: {THEME["CARD_BG"]} !important;
        color: #ffffff !important;
        border: 1px solid {THEME["BORDER_COLOR"]} !important;
        border-radius: 8px !important;
    }}
    [data-testid="stChatInput"] textarea:focus {{
        border-color: {THEME["PRIMARY_COLOR"]} !important;
        box-shadow: 0 0 0 1px {THEME["PRIMARY_COLOR"]} !important;
    }}
    
    /* 13. Custom Scrollbar for modern SaaS styling */
    ::-webkit-scrollbar {{
        width: 6px;
        height: 6px;
    }}
    ::-webkit-scrollbar-track {{
        background: {THEME["BG_COLOR"]};
    }}
    ::-webkit-scrollbar-thumb {{
        background: {THEME["BORDER_COLOR"]};
        border-radius: 4px;
    }}
    ::-webkit-scrollbar-thumb:hover {{
        background: #374151;
    }}
</style>
""", unsafe_allow_html=True)

# Initialize Session State
if "current_user_id" not in st.session_state:
    st.session_state.current_user_id = None
if "current_session_id" not in st.session_state:
    st.session_state.current_session_id = None
if "selected_session_title" not in st.session_state:
    st.session_state.selected_session_title = ""

# Fetch/Create guest user automatically to bypass login and profile selectors
guest_id = get_guest_user_id()
if guest_id:
    st.session_state.current_user_id = guest_id
else:
    st.error("Error setting up guest session database profile. Please contact the administrator.")
    st.stop()

# --- SIDEBAR CONFIG ---
with st.sidebar:
    st.markdown("<h2 style='text-align: center; color: #3b82f6; margin-bottom: 1.5rem;'>🏥 MedAI Hub</h2>", unsafe_allow_html=True)
    
    # Visual Guest indicator
    st.markdown("<div style='text-align: center; background-color: rgba(59, 130, 246, 0.15); padding: 0.5rem; border-radius: 8px; margin-bottom: 1rem; border: 1px solid rgba(59, 130, 246, 0.3);'><span style='color: #93c5fd;'>Logged in as:</span> <b style='color: #ffffff;'>Guest User</b></div>", unsafe_allow_html=True)
        
    st.markdown("---")
    
    # 2. Session Manager
    if st.session_state.current_user_id:
        sessions = get_user_sessions(st.session_state.current_user_id)
        
        # New Chat Button
        if st.button("➕ New Chat Session", use_container_width=True):
            st.session_state.current_session_id = str(uuid.uuid4())
            st.session_state.selected_session_title = "New Session"
            # Creating dummy record
            create_session(st.session_state.current_session_id, st.session_state.current_user_id, "New Session")
            st.rerun()

        st.markdown("<h4 style='color: #8b949e; margin-top: 1rem;'>Recent Conversations</h4>", unsafe_allow_html=True)
        
        if sessions:
            # Set default session if none active
            if not st.session_state.current_session_id:
                st.session_state.current_session_id = sessions[0]['id']
                st.session_state.selected_session_title = sessions[0]['title']
            
            # Draw session buttons
            for sess in sessions:
                col1, col2 = st.columns([0.85, 0.15])
                with col1:
                    is_active = sess['id'] == st.session_state.current_session_id
                    btn_style = "👉 " if is_active else ""
                    if st.button(f"{btn_style}{sess['title'][:25]}", key=f"sess_{sess['id']}", use_container_width=True, type="secondary" if not is_active else "primary"):
                        st.session_state.current_session_id = sess['id']
                        st.session_state.selected_session_title = sess['title']
                        st.rerun()
                with col2:
                    if st.button("🗑️", key=f"del_{sess['id']}", help="Delete Session"):
                        delete_session(sess['id'])
                        if st.session_state.current_session_id == sess['id']:
                            st.session_state.current_session_id = None
                            st.session_state.selected_session_title = ""
                        st.rerun()
        else:
            st.info("No active chat sessions. Click 'New Chat Session' above.")
            if not st.session_state.current_session_id:
                # Force new session creation if empty
                st.session_state.current_session_id = str(uuid.uuid4())
                st.session_state.selected_session_title = "New Session"
                create_session(st.session_state.current_session_id, st.session_state.current_user_id, "New Session")
                st.rerun()
    
    st.markdown("---")
    
    # 3. System Status
    st.markdown("<h4 style='color: #8b949e; margin-top: 1.5rem;'>Services Status</h4>", unsafe_allow_html=True)
    
    llm = load_llm()
    vs = load_vector_store()
    
    # DB Status Check
    db_ok = False
    try:
        with get_db_connection() as conn:
            conn.execute("SELECT 1")
            db_ok = True
    except:
        pass
        
    status_db = "Ready" if db_ok else "Offline"
    status_llm = "Connected" if llm else "Error (Check Key)"
    status_vs = "Mounted" if vs else "Warning (No docs)"
    
    # Render aligned SaaS status lines using CSS classes
    st.markdown(f"""
    <div style="background-color: {THEME['CARD_BG']}; border: 1px solid {THEME['BORDER_COLOR']}; border-radius: 8px; padding: 0.75rem; margin-bottom: 1rem;">
        <div class="status-line">
            <span>Database</span>
            <span style="color: {'#10b981' if db_ok else '#ef4444'}; font-weight: 600;">{status_db}</span>
        </div>
        <div class="status-line">
            <span>Groq LLM</span>
            <span style="color: {'#10b981' if llm else '#ef4444'}; font-weight: 600;">{status_llm}</span>
        </div>
        <div class="status-line">
            <span>Pinecone RAG</span>
            <span style="color: {'#10b981' if vs else '#f59e0b'}; font-weight: 600;">{status_vs}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Disclaimer in sidebar footer
    st.markdown(f"<div style='font-size: 0.75rem; color: #8b949e; margin-top: 2rem; border-top: 1px solid {THEME['BORDER_COLOR']}; padding-top: 1rem;'>MedAI is an AI educational assistant. Always verify clinical choices with a qualified health professional.</div>", unsafe_allow_html=True)

# --- MAIN PAGE CONTENT ---

# Header Panel
st.markdown(f"""
<div class="header-container">
    <div style="display: flex; justify-content: space-between; align-items: center;">
        <h1 class="header-title">🏥 MedAI Assistant <span class="pulse-indicator"></span></h1>
        <div class="status-badge status-active">RAG Knowledge Base Active</div>
    </div>
    <p class="header-subtitle">Streamlined Clinical Guidelines Assistant & RAG Query Hub</p>
</div>
""", unsafe_allow_html=True)

# Main chat rendering loop
if st.session_state.current_session_id:
    # Title rename card
    col_t, col_rename = st.columns([0.7, 0.3])
    with col_t:
        st.markdown(f"### Session: *{st.session_state.selected_session_title}*")
    with col_rename:
        with st.popover("📝 Rename Session"):
            new_title = st.text_input("New Title", value=st.session_state.selected_session_title)
            if st.button("Save", use_container_width=True):
                rename_session(st.session_state.current_session_id, new_title)
                st.session_state.selected_session_title = new_title
                st.rerun()

    # Load messages
    messages = get_session_messages(st.session_state.current_session_id)
    
    # If no messages, render welcome
    if not messages:
        st.markdown(f"<div class='info-card'>{WELCOME_MESSAGE}</div>", unsafe_allow_html=True)
        
        # Suggestion Prompts
        st.markdown("#### Try asking about:")
        cols = st.columns(3)
        suggestions = [
            ("🌡️ Symptoms analysis", "What are the early warning signs (red flags) of diabetes?"),
            ("💊 Medication facts", "Tell me about Metformin typical dosages and side effects."),
            ("🏃 Lifestyle wellness", "What are WHO guidelines for cardiovascular exercise?")
        ]
        for i, (title, prompt_text) in enumerate(suggestions):
            with cols[i]:
                if st.button(title, key=f"sug_{i}", use_container_width=True):
                    # Save user message
                    save_chat_message(st.session_state.current_session_id, "User", prompt_text)
                    
                    # Update session title if default
                    if st.session_state.selected_session_title == "New Session":
                        title_extracted = prompt_text[:30] + "..." if len(prompt_text) > 30 else prompt_text
                        rename_session(st.session_state.current_session_id, title_extracted)
                        st.session_state.selected_session_title = title_extracted
                        
                    st.rerun()

    # Render conversation
    for msg in messages:
        avatar = "🧑‍💻" if msg['role'] == "User" else "🏥"
        with st.chat_message(msg['role'], avatar=avatar):
            st.markdown(msg['content'])

    # Chat Input Box
    if user_query := st.chat_input("Enter clinical queries or symptoms..."):
        # Display user message instantly
        with st.chat_message("User", avatar="🧑‍💻"):
            st.markdown(user_query)
            
        # Save user message to database
        save_chat_message(st.session_state.current_session_id, "User", user_query)
        
        # Extract title if session is brand new
        if st.session_state.selected_session_title == "New Session":
            title_extracted = user_query[:30] + "..." if len(user_query) > 30 else user_query
            rename_session(st.session_state.current_session_id, title_extracted)
            st.session_state.selected_session_title = title_extracted
            st.rerun()

        # Generate Assistant response
        with st.chat_message("Assistant", avatar="🏥"):
            response_placeholder = st.empty()
            
            # Fetch context and history
            vs = load_vector_store()
            context = ""
            if vs:
                try:
                    with st.spinner("Searching Medical Knowledge Base..."):
                        context = retrieve_relevant_context(user_query, vs, k=3)
                except Exception as e:
                    logger.warning(f"RAG retrieval failed: {e}")
            
            chat_history_text = ""
            for m in messages[-6:]:
                chat_history_text += f"{m['role']}: {m['content']}\n"
                
            messages_payload = build_messages(user_query, context, chat_history_text)
            
            # Run streaming model execution
            full_response = ""
            llm_inst = load_llm()
            
            if llm_inst:
                try:
                    # Stream tokens in real time
                    token_stream = llm_inst.stream(messages_payload)
                    for chunk in token_stream:
                        token = chunk.content
                        if token:
                            full_response += token
                            response_placeholder.markdown(full_response + "▌")
                    
                    # Disclaimer assessment
                    needs_disclaimer = any(
                        kw in user_query.lower()
                        for kw in ['diagnose', 'treatment', 'medicine', 'drug', 'symptom', 'pain', 'sick', 'disease']
                    )
                    if needs_disclaimer:
                        full_response += DISCLAIMER_TEXT
                    
                    # Finalize output rendering
                    response_placeholder.markdown(full_response)
                    
                    # Save response in database
                    save_chat_message(st.session_state.current_session_id, "Assistant", full_response)
                except Exception as e:
                    logger.error(f"Streaming failed: {e}")
                    st.error(f"Error during response generation: {e}")
            else:
                st.error("LLM Service is not configured. Please ensure GROQ_API_KEY is defined in .env.")
        st.rerun()
else:
    st.info("Select a conversation session in the sidebar to get started.")
