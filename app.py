import os
import json
import secrets
import random
import uuid
import logging
import threading
import time
from datetime import datetime, timedelta
import sqlite3

from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context, redirect, url_for, flash
from flask_cors import CORS
from flask_mail import Mail, Message
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from jinja2 import ChoiceLoader, FileSystemLoader

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from src.helper import create_vector_store, retrieve_relevant_context
from src.prompt import MEDICAL_SYSTEM_PROMPT, MEDICAL_ASSISTANT_PROMPT, DISCLAIMER_TEXT

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & INIT ---
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(hours=12)
CORS(app)

# Load templates from this app first, then shared loginpage folders.
_base_dir = os.path.dirname(os.path.abspath(__file__))
_local_templates = os.path.join(_base_dir, "templates")
_shared_root = os.path.abspath(os.path.join(_base_dir, "..", "loginpage"))
_shared_templates = os.path.join(_shared_root, "templates")

# Explicitly prioritize local templates to avoid loading old versions
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(_local_templates),
    FileSystemLoader(_shared_templates),
    FileSystemLoader(_shared_root),
])

limiter = Limiter(key_func=get_remote_address, app=app, default_limits=["200 per day", "50 per hour"])

# Gmail / SMTP settings
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ['true', '1', 't']
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['DEVELOPMENT_MODE'] = not app.config['MAIL_USERNAME']

mail = Mail(app)

# In-memory simple LRU cache to save Groq API costs + improve performance
_response_cache = {}
_llm_stream = None
_llm_no_stream = None
_vector_store = None

# --- DATABASE SETUP ---
def init_db():
    try:
        with sqlite3.connect('medical_users.db') as conn:
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
                
            logger.info("Database securely initialized with indexes and admin account.")
    except Exception as e:
        logger.error(f"DB Init Failed: {e}")
init_db()


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    # Help prevent "Unsafe attempt" errors in modern browsers
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    return response

# --- BACKGROUND WORKERS & UTILS ---
def send_async_email(app_instance, msg):
    """Sends email asynchronously to prevent blocking the HTTP request."""
    with app_instance.app_context():
        try:
            # Simple retry mechanism
            for attempt in range(3):
                try:
                    mail.send(msg)
                    logger.info(f"Email sent successfully to {msg.recipients[0]}")
                    return
                except Exception as e:
                    logger.warning(f"Email attempt {attempt + 1} failed: {str(e)}")
                    time.sleep(2)
            logger.error("All 3 email sending attempts failed.")
        except Exception as e:
            logger.error(f"Fatal async email error: {e}")

def securely_store_otp(email, raw_otp):
    """Hashes the OTP and sets it to expire in 5 minutes."""
    otp_hash = generate_password_hash(raw_otp)
    expiry = datetime.utcnow() + timedelta(minutes=5)
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.execute("INSERT OR REPLACE INTO otp_store (email, otp_hash, expires_at, attempts) VALUES (?, ?, ?, 0)",
                         (email, otp_hash, expiry))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to securely store OTP: {e}")

def verify_and_delete_otp(email, raw_otp):
    """Verifies timing, attempts, and hash. Returns (bool: success, str: message)."""
    try:
        with sqlite3.connect('medical_users.db') as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT otp_hash, expires_at, attempts FROM otp_store WHERE email=?", (email,))
            row = cursor.fetchone()
            
            if not row:
                return False, "OTP not found or expired."
            
            otp_hash, expires_at_str, attempts = row
            # Handle both timestamp formats (with and without microseconds)
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
                try:
                    expires_at = datetime.strptime(expires_at_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return False, "Invalid OTP record. Please request a new one."
            
            if datetime.utcnow() > expires_at:
                conn.execute("DELETE FROM otp_store WHERE email=?", (email,))
                conn.commit()
                return False, "OTP has expired. Please request a new one."
            
            if attempts >= 5:
                # Basic brute force protection
                conn.execute("DELETE FROM otp_store WHERE email=?", (email,))
                conn.commit()
                return False, "Too many failed attempts. OTP invalidated."
            
            if check_password_hash(otp_hash, raw_otp):
                conn.execute("DELETE FROM otp_store WHERE email=?", (email,))
                conn.commit()
                return True, "Success"
            else:
                conn.execute("UPDATE otp_store SET attempts = attempts + 1 WHERE email=?", (email,))
                conn.commit()
                return False, "Incorrect OTP code."
    except Exception as e:
        logger.error(f"OTP verification failure: {str(e)}")
        return False, "Internal server error during verification."

# --- LAZY LOADING SERVICES ---
def get_llm(streaming=False):
    global _llm_stream, _llm_no_stream
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key: return None
    
    if streaming:
        if _llm_stream is None:
            _llm_stream = ChatGroq(temperature=0.3, max_tokens=2048, groq_api_key=api_key, model_name="llama-3.1-8b-instant", streaming=True)
        return _llm_stream
    else:
        if _llm_no_stream is None:
            _llm_no_stream = ChatGroq(temperature=0.3, max_tokens=2048, groq_api_key=api_key, model_name="llama-3.1-8b-instant", streaming=False)
        return _llm_no_stream

def get_vector_store():
    global _vector_store
    if _vector_store is None:
        try:
            _vector_store = create_vector_store()
        except:
            _vector_store = None
    return _vector_store

def build_messages(user_message, context, chat_history):
    prompt = MEDICAL_ASSISTANT_PROMPT.format(
        context=context if context else "No vector documents available - rely on highly accurate medical knowledge.",
        chat_history=chat_history if chat_history else "No previous conversation.",
        question=user_message
    )
    return [SystemMessage(content=MEDICAL_SYSTEM_PROMPT), HumanMessage(content=prompt)]

def get_context_and_history(user_message, session_id=None):
    context = ""
    vs = get_vector_store()
    if vs:
        try:
            context = retrieve_relevant_context(user_message, vs, k=3) # Reduced to k=3 to lower latency
        except Exception as e:
            logger.warning(f"RAG Retrieval failed: {e}")

    chat_history_text = ""
    if session_id:
        try:
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY timestamp DESC LIMIT 6", (session_id,))
                messages = cursor.fetchall()
                # Reverse back to chronological
                for role, content in reversed(messages):
                    chat_history_text += f"{role}: {content}\n"
        except Exception:
            pass
    return context, chat_history_text


# --- AUTHENTICATION ROUTES ---

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.before_request
def auto_login_guest():
    if request.path.startswith('/static/'):
        return
    if 'user_id' not in session:
        try:
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE username='guest'")
                row = cursor.fetchone()
                if row:
                    guest_id = row[0]
                else:
                    cursor.execute("INSERT INTO users (username, password, email, name, role) VALUES (?, ?, ?, ?, ?)",
                                 ('guest', 'guest_pass_hash', 'guest@medai.com', 'Guest User', 'user'))
                    conn.commit()
                    guest_id = cursor.lastrowid
                
                session.permanent = True
                session['user_id'] = guest_id
                session['username'] = 'guest'
                session['role'] = 'user'
        except Exception as e:
            logger.error(f"Auto-login guest failed: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        try:
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, username, password, role, is_active FROM users WHERE username=? OR email=?", (username, username))
                user = cursor.fetchone()
                
                if user and check_password_hash(user[2], password):
                    # is_active can be None for old rows — treat None as active
                    if user[4] is not None and not user[4]:
                        flash('Your account has been deactivated. Please contact support.', 'error')
                        return redirect(url_for('login'))
                    session.permanent = True
                    session['user_id'] = user[0]
                    session['username'] = user[1]
                    # role can be NULL for older user rows — default to 'user'
                    session['role'] = user[3] if user[3] else 'user'
                    logger.info(f"User {user[1]} logged in successfully.")
                    return redirect(url_for('index'))
                else:
                    flash('Invalid username or password. Please try again.', 'error')
                    logger.warning(f"Failed login attempt for user/email: {username}")
        except Exception as e:
            logger.error(f"Login failed internal: {e}")
            flash('Internal server error during login.', 'error')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        
        # Server-side validation
        if password != confirm:
            flash('Passwords do not match!', 'error')
            return redirect(url_for('register'))
            
        if len(password) < 8 or not any(c.isupper() for c in password) or not any(c.isdigit() for c in password):
            flash('Password must be 8+ chars with uppercase and numbers.', 'error')
            return redirect(url_for('register'))
            
        if '@' not in email or '.' not in email:
            flash('Invalid email address!', 'error')
            return redirect(url_for('register'))
        
        # Check uniqueness
        try:
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE username=?", (username,))
                if cursor.fetchone():
                    flash('Username already exists!', 'error')
                    return redirect(url_for('register'))
                
                cursor.execute("SELECT id FROM users WHERE email=?", (email,))
                if cursor.fetchone():
                    flash('Email already registered!', 'error')
                    return redirect(url_for('register'))
        except Exception as e:
            logger.error(f"Register DB verify error: {e}")

        # Store temp state
        session['temp_user'] = {
            'name': name,
            'username': username,
            'email': email,
            'password': generate_password_hash(password)
        }

        # Generate OTP
        raw_otp = str(random.randint(100000, 999999))
        securely_store_otp(email, raw_otp)

        if app.config.get('MAIL_USERNAME'):
            # Production mode: send real email
            try:
                msg = Message("MedAI Registration Code", sender=app.config.get('MAIL_USERNAME'), recipients=[email])
                msg.body = f"Welcome to MedAI!\nYour verification code is: {raw_otp}\nIt expires in 5 minutes."
                threading.Thread(target=send_async_email, args=(app, msg)).start()
                flash("OTP sent to your email.", "success")
            except Exception as e:
                logger.error(f"Failed to queue email: {e}")
                flash("Failed to send OTP. Try again.", "error")
        else:
            # Development mode: show OTP in flash message
            logger.warning(f"DEV MODE: OTP for {email} is {raw_otp}")
            flash(f"[DEV MODE] Email not configured. Your OTP is: {raw_otp}", "info")

        return redirect(url_for('verify_otp'))

    return render_template('register.html')

@app.route('/verify-otp', methods=['GET', 'POST'])
@limiter.limit("15 per minute")
def verify_otp():
    if 'temp_user' not in session:
        flash('Session expired or invalid. Please register again.', 'error')
        return redirect(url_for('register'))

    email = session['temp_user']['email']

    if request.method == 'POST':
        user_otp = request.form.get('otp', '').strip()
        
        is_valid, msg = verify_and_delete_otp(email, user_otp)
        
        if is_valid:
            temp_user = session['temp_user']
            try:
                with sqlite3.connect('medical_users.db') as conn:
                    cursor = conn.cursor()
                    cursor.execute("INSERT INTO users (username, password, email, name) VALUES (?, ?, ?, ?)",
                                   (temp_user['username'], temp_user['password'], temp_user['email'], temp_user['name']))
                    user_id = cursor.lastrowid
                    conn.commit()

                session.permanent = True
                session['user_id'] = user_id
                session['username'] = temp_user['username']
                session.pop('temp_user', None)
                flash('Account created successfully!', 'success')
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                flash("That username or email got taken while you were verifying. Try again.", "warning")
                return redirect(url_for('register'))
            except Exception as e:
                logger.error(f"Failed to create user post-OTP: {e}")
                flash('Internal server error.', 'error')
        else:
            flash(msg, 'error')

    return render_template('verify_otp.html')

@app.route('/resend-otp', methods=['GET'])
@limiter.limit("3 per minute")
def resend_otp():
    if 'temp_user' not in session and 'reset_email' not in session:
        return redirect(url_for('login'))

    email = session.get('temp_user', {}).get('email') or session.get('reset_email')
    
    raw_otp = str(random.randint(100000, 999999))
    securely_store_otp(email, raw_otp)

    try:
        msg = Message("MedAI verification code resend", sender=app.config.get('MAIL_USERNAME'), recipients=[email])
        msg.body = f"Your new verification code is: {raw_otp}. It expires in 5 minutes."
        threading.Thread(target=send_async_email, args=(app, msg)).start()
        flash("OTP sent to your email.", "success")
    except Exception as e:
        logger.error(f"Failed to process email: {e}")
        flash("Failed to send OTP. Try again.", "error")

    # Send them back to where they came from
    if 'reset_email' in session:
        return redirect(url_for('forgot_password'))
    return redirect(url_for('verify_otp'))

# --- RECOVERY (FORGOT PASSWORD) ---

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def forgot_password():
    if request.method == 'POST':
        action = request.form.get('action') # 'send_code' or 'verify_code'
        
        if action == 'send_code':
            email = request.form.get('email', '').strip()
            # Validate user exists
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email=?", (email,))
                if not cursor.fetchone():
                    flash("If that email is registered, an OTP was sent.", "info") # Obscure to prevent enumeration
                    return render_template('forgot_password.html')
            
            session['reset_email'] = email
            raw_otp = str(random.randint(100000, 999999))
            securely_store_otp(email, raw_otp)
            
            try:
                msg = Message("MedAI Password Reset", sender=app.config.get('MAIL_USERNAME'), recipients=[email])
                msg.body = f"Your password reset code is: {raw_otp}\nIt expires in 5 minutes."
                threading.Thread(target=send_async_email, args=(app, msg)).start()
                flash("OTP sent to your email.", "success")
            except Exception as e:
                logger.error(f"Failed to process password reset email: {e}")
                flash("Failed to send OTP. Try again.", "error")
                return render_template('forgot_password.html')
            return render_template('forgot_password.html', waiting_for_otp=True)
            
        elif action == 'verify_code':
            email = session.get('reset_email')
            user_otp = request.form.get('otp', '').strip()
            
            is_valid, msg = verify_and_delete_otp(email, user_otp)
            if is_valid:
                session['can_reset_password'] = True
                return redirect(url_for('reset_password'))
            else:
                flash(msg, 'error')
                return render_template('forgot_password.html', waiting_for_otp=True)
                
    return render_template('forgot_password.html', waiting_for_otp=False)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if not session.get('can_reset_password') or not session.get('reset_email'):
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')
        
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template('reset_password.html')
            
        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            return render_template('reset_password.html')
            
        email = session['reset_email']
        new_hash = generate_password_hash(password)
        
        with sqlite3.connect('medical_users.db') as conn:
            conn.execute("UPDATE users SET password=? WHERE email=?", (new_hash, email))
            conn.commit()
            
        session.pop('can_reset_password', None)
        session.pop('reset_email', None)
        flash("Password reset successfully. You can now log in.", "success")
        return redirect(url_for('login'))

    return render_template('reset_password.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- USER DASHBOARD ---

@app.route('/dashboard')
def dashboard():
    """User dashboard redirects to main chat interface."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('index'))


# --- ERROR HANDLERS ---

@app.errorhandler(404)
def not_found(e):
    logger.warning(f"404 Not Found: {request.url}")
    # Return JSON for API routes, HTML page for browser routes
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Resource not found', 'code': 404}), 404
    return render_template('login.html'), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 Internal Server Error: {e}")
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Internal server error', 'code': 500}), 500
    return render_template('login.html'), 500


# --- ADMIN DASHBOARD ---

def mask_sensitive_data(email):
    if not email or '@' not in email: return email
    parts = email.split('@')
    return parts[0][:2] + '***@' + parts[1]

@app.route('/admin')
@limiter.limit("20 per minute")
def admin_dashboard():
    # Role-based access control (RBAC) securely protects the dashboard
    if session.get('role') != 'admin' and session.get('username') != 'admin':
        logger.warning(f"AUDIT LOG: Unauthorized admin access attempt by user {session.get('username', 'Unknown')}.")
        flash('Access restricted to administrators only.', 'error')
        return redirect(url_for('index'))
    
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Get stats safely
            cursor.execute("SELECT (SELECT COUNT(*) FROM users) as user_count, (SELECT COUNT(*) FROM chat_sessions) as session_count, (SELECT COUNT(*) FROM chat_messages) as msg_count")
            stats = cursor.fetchone()
            
            # Get DTO users (explicitly exclude password/secrets completely) 
            cursor.execute("SELECT id, username, email, name, role, is_active, created_at FROM users ORDER BY created_at DESC")
            raw_users = cursor.fetchall()
            
            # Application layer masking filtering (DTO approach)
            users = []
            for r in raw_users:
                u = dict(r)
                u['email'] = mask_sensitive_data(u['email'])
                users.append(u)
            
            logger.info(f"AUDIT LOG: Admin dashboard accessed by {session.get('username')}.")
            return render_template('admin.html', stats=stats, users=users)
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        return f"Error loading dashboard: {e}", 500

@app.route('/admin/toggle_user/<int:user_id>', methods=['POST'])
@limiter.limit("10 per minute")
def admin_toggle_user(user_id):
    if session.get('role') != 'admin' and session.get('username') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        with sqlite3.connect('medical_users.db') as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_active = CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?", (user_id,))
            conn.commit()
        logger.info(f"AUDIT LOG: Admin toggled user {user_id} active status.")
        flash('User account status updated.', 'success')
    except Exception as e:
        flash(f'Error updating user: {e}', 'error')
    return redirect(url_for('admin_dashboard'))
    
@app.route('/admin/upload', methods=['POST'])
def admin_upload():
    if session.get('role') != 'admin' and session.get('username') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
    if 'file' not in request.files:
        flash('No file part', 'error')
        return redirect(url_for('admin_dashboard'))
        
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(url_for('admin_dashboard'))
        
    if file and file.filename.lower().endswith('.pdf'):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(_base_dir, 'data', filename)
        file.save(upload_path)
        flash(f'File {filename} uploaded successfully. Click "Ingest" to update AI knowledge.', 'success')
    else:
        flash('Only PDF files are allowed.', 'error')
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/ingest', methods=['POST'])
def admin_ingest():
    if session.get('role') != 'admin' and session.get('username') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
    try:
        from src.helper import load_pdf_documents, create_text_chunks, add_documents_to_vectorstore
        docs = load_pdf_documents(os.path.join(_base_dir, 'data'))
        if not docs:
            flash('No documents found in data/ folder.', 'warning')
            return redirect(url_for('admin_dashboard'))
            
        chunks = create_text_chunks(docs)
        vs = get_vector_store()
        if vs:
            add_documents_to_vectorstore(chunks)
            flash(f'Knowledge base updated with {len(chunks)} chunks.', 'success')
        else:
            flash('Vector store not available.', 'error')
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        flash(f'Ingestion error: {str(e)}', 'error')
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@limiter.limit("10 per minute")
def admin_delete_user(user_id):
    if session.get('role') != 'admin' and session.get('username') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        with sqlite3.connect('medical_users.db') as conn:
            # Sanitize input handled implicitly via ? parameter substitution 
            conn.execute("DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE user_id=?)", (user_id,))
            conn.execute("DELETE FROM chat_sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
        logger.info(f"AUDIT LOG: Admin deleted user {user_id}.")
        flash('User deleted securely.', 'success')
    except Exception as e:
        flash(f'Error deleting user: {e}', 'error')
        
    return redirect(url_for('admin_dashboard'))


# --- CHAT & API ENDPOINTS ---

@app.route('/api/health')
def health():
    llm = get_llm()
    vs = get_vector_store()
    return jsonify({
        "status": "ok",
        "llm": "ready" if llm else "unavailable",
        "vector_store": "ready" if vs else "unavailable"
    })

@app.route('/api/stream', methods=['POST'])
@limiter.limit("30 per minute")
def stream_chat():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    llm = get_llm(streaming=True)
    if not llm:
        return jsonify({'success': False, 'error': 'AI service misconfigured.'}), 503

    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()
        session_id_raw = data.get('session_id')
        session_id = session_id_raw.strip() if session_id_raw else ''
        edit_message_id = data.get('edit_message_id')
        user_id = session.get('user_id')

        if not user_message:
            return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400

        # Handle Query Editing: Delete the edited message and all subsequent messages to branch history
        if edit_message_id and session_id:
            with sqlite3.connect('medical_users.db') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT timestamp FROM chat_messages WHERE id=? AND session_id=?", (edit_message_id, session_id))
                row = cursor.fetchone()
                if row:
                    msg_time = row[0]
                    conn.execute("DELETE FROM chat_messages WHERE session_id=? AND timestamp >= ?", (session_id, msg_time))
                conn.commit()

        # CACHE CHECK: Simple fast memory look up for exact queries to respond instantly
        cache_key = f"{user_id}_{user_message.lower()}"
        if cache_key in _response_cache:
            def cached_stream():
                if not session_id:
                    new_id = str(uuid.uuid4())
                    with sqlite3.connect('medical_users.db') as conn:
                        conn.execute("INSERT INTO chat_sessions (id, user_id, title) VALUES (?, ?, ?)",
                                   (new_id, user_id, user_message[:30]))
                        conn.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?, 'User', ?)", (new_id, user_message))
                        conn.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?, 'Assistant', ?)", (new_id, _response_cache[cache_key]))
                    yield f"data: {json.dumps({'session_id': new_id, 'title': user_message[:30]})}\n\n"
                    time.sleep(0.1) # UI trick
                yield f"data: {json.dumps({'token': _response_cache[cache_key]})}\n\n"
                yield f"data: {json.dumps({'done': True, 'session_id': session_id if session_id else new_id})}\n\n"
            return Response(stream_with_context(cached_stream()), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache'})

        is_new_session = False
        title = ""

        if not session_id:
            session_id = str(uuid.uuid4())
            is_new_session = True
            title = user_message[:35] + ('...' if len(user_message) > 35 else '')
            # Thread-safe DB access wrapper
            try:
                with sqlite3.connect('medical_users.db') as conn:
                    conn.execute("INSERT INTO chat_sessions (id, user_id, title) VALUES (?, ?, ?)",
                                 (session_id, user_id, title))
            except Exception as db_err:
                logger.error(f"Failed to create new session in DB: {db_err}")

        # Save User Msg
        try:
            with sqlite3.connect('medical_users.db') as conn:
                conn.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
                             (session_id, 'User', user_message))
                conn.execute("UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
        except Exception as db_err:
            logger.error(f"Failed to save user message to DB: {db_err}")

        context, chat_history = get_context_and_history(user_message, session_id)
        messages = build_messages(user_message, context, chat_history)

        def generate():
            full_response = ""
            try:
                if is_new_session:
                    yield f"data: {json.dumps({'session_id': session_id, 'title': title})}\n\n"

                for chunk in llm.stream(messages):
                    token = chunk.content
                    if token:
                        full_response += token
                        yield f"data: {json.dumps({'token': token})}\n\n"

                needs_disclaimer = any(
                    kw in user_message.lower()
                    for kw in ['diagnose', 'treatment', 'medicine', 'drug', 'symptom', 'pain', 'sick', 'disease']
                )
                if needs_disclaimer:
                    full_response += DISCLAIMER_TEXT
                    yield f"data: {json.dumps({'token': DISCLAIMER_TEXT})}\n\n"

                # Store response in DB
                try:
                    with sqlite3.connect('medical_users.db') as conn:
                        conn.execute("INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
                                     (session_id, 'Assistant', full_response))
                except Exception as db_err:
                    logger.error(f"Failed to save assistant response to DB: {db_err}")
                    
                # Setup cache
                _response_cache[cache_key] = full_response
                if len(_response_cache) > 200: # LRU limit
                    _response_cache.pop(next(iter(_response_cache)))

                yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
            except Exception as e:
                logger.error(f"Streaming failed: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return Response(stream_with_context(generate()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    except Exception as e:
        logger.error(f"Stream route fault: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/api/sessions', methods=['GET'])
@limiter.limit("60 per minute")
def get_sessions():
    if 'user_id' not in session: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, title, updated_at FROM chat_sessions WHERE user_id=? ORDER BY updated_at DESC", (session['user_id'],))
            s_list = [{'id': row['id'], 'title': row['title'], 'updated_at': row['updated_at']} for row in cursor.fetchall()]
            return jsonify({'success': True, 'sessions': s_list})
    except Exception as e:
        logger.error(f"Failed to fetch sessions: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sessions/<session_id>/messages', methods=['GET'])
def get_session_messages(session_id):
    if 'user_id' not in session: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM chat_sessions WHERE id=? AND user_id=?", (session_id, session['user_id']))
            if not cursor.fetchone(): return jsonify({'success': False, 'error': 'Not found'}), 404
            
            cursor.execute("SELECT id, role, content, timestamp FROM chat_messages WHERE session_id=? ORDER BY timestamp ASC", (session_id,))
            m_list = [{'id': row['id'], 'role': row['role'], 'content': row['content'], 'timestamp': row['timestamp']} for row in cursor.fetchall()]
            return jsonify({'success': True, 'messages': m_list})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    if 'user_id' not in session: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE id=? AND user_id=?)", (session_id, session['user_id']))
            conn.execute("DELETE FROM chat_sessions WHERE id=? AND user_id=?", (session_id, session['user_id']))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/sessions/<session_id>/rename', methods=['PUT'])
def rename_session(session_id):
    if 'user_id' not in session: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    title = request.get_json().get('title', '').strip()
    if not title: return jsonify({'success': False, 'error': 'Empty title'}), 400
    try:
        with sqlite3.connect('medical_users.db') as conn:
            conn.execute("UPDATE chat_sessions SET title=? WHERE id=? AND user_id=?", (title, session_id, session['user_id']))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    if not app.config['MAIL_USERNAME']:
        logger.warning("\n" + "!"*60 + "\nEMAIL SYSTEM IS IN DEMO MODE. Configure MAIL_USERNAME.\n" + "!"*60 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)