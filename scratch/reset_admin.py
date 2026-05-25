import sqlite3
from werkzeug.security import generate_password_hash

def reset_admin():
    db_path = 'medical_users.db'
    new_password = "MedAIAdmin2025!"
    hashed_pw = generate_password_hash(new_password)
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if exists
        cursor.execute("SELECT id FROM users WHERE username='admin'")
        user = cursor.fetchone()
        
        if user:
            print(f"Found admin (ID: {user[0]}). Updating password...")
            cursor.execute("UPDATE users SET password=?, role='admin' WHERE username='admin'", (hashed_pw,))
        else:
            print("Admin user not found. Creating...")
            cursor.execute("INSERT INTO users (username, password, email, name, role) VALUES (?, ?, ?, ?, ?)",
                         ('admin', hashed_pw, 'admin@medai.com', 'System Admin', 'admin'))
        
        conn.commit()
        print("Admin account successfully reset to: admin / MedAIAdmin2025!")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    reset_admin()
