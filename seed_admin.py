import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = "medical_users.db"
USERNAME = "admin"
PASSWORD = "AdminPassword2026!"
EMAIL = "admin@medai.local"
NAME = "System Admin"

def create_admin():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database {DB_PATH} not found.")
        return

    hashed_pw = generate_password_hash(PASSWORD)
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # Check if admin already exists
            cursor.execute("SELECT id FROM users WHERE username=?", (USERNAME,))
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE users SET password=?, email=?, role='admin', is_active=1 WHERE username=?",
                    (hashed_pw, EMAIL, USERNAME)
                )
                print(f"Admin user '{USERNAME}' updated with role=admin, is_active=1.")
            else:
                cursor.execute(
                    "INSERT INTO users (username, password, email, name, role, is_active) VALUES (?, ?, ?, ?, 'admin', 1)", 
                    (USERNAME, hashed_pw, EMAIL, NAME)
                )
                print(f"Admin user '{USERNAME}' created with role=admin.")
            
            conn.commit()
            print(f"\n✅ Login credentials:\n   Username: {USERNAME}\n   Password: {PASSWORD}\n   URL:      http://127.0.0.1:5000/login")
    except Exception as e:
        print(f"Failed to create/update admin user: {e}")

if __name__ == "__main__":
    create_admin()
