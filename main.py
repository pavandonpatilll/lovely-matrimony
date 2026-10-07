from fastapi import FastAPI
from fastapi import UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import hashlib
import shutil
import uuid
import os
import json
import threading
import datetime
import secrets
from urllib.parse import quote, unquote
import firebase_admin
from firebase_admin import credentials, firestore, auth as firebase_auth
try:
    from firebase_admin import storage as firebase_storage
except Exception:
    firebase_storage = None

# =====================================================
# APP
# =====================================================

app = FastAPI(
    title="JeevanSaathi API",
    version="1.0"
)

# =====================================================
# CORS
# =====================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================
# FIREBASE AUTO MIRROR FOR ALL WRITE ACTIONS
# =====================================================
# Firebase sync is intentionally debounced. A full-table mirror after every
# write can make a busy app slow and create many concurrent Firestore jobs.
@app.middleware("http")
async def firebase_mirror_middleware(request, call_next):
    response = await call_next(request)
    # Do not start a full-table Firestore mirror for high-frequency or
    # authentication-only requests. Local SQLite is the fast source of truth;
    # Firebase remains the durable backup and is synced for real data changes.
    sync_paths = {
        "/register", "/update-profile", "/change-password",
        "/send-message", "/upload-chat-photo", "/delete-message",
        "/send-interest", "/respond-interest", "/block-user",
        "/favorite", "/remove-favorite", "/premium",
        "/upload-profile-photo", "/upload-profile-photos",
        "/delete-profile-photo", "/set-main-photo",
        "/delete-account"
    }
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (request.url.path in sync_paths or request.url.path.startswith("/admin/")):
        firebase_sync_async()
    return response


# =====================================================
# FOLDERS
# =====================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =====================================================
# REAL FIREBASE (SECURE BACKEND CONNECTION)
# =====================================================
# Put the Firebase service-account JSON in Render as:
# FIREBASE_SERVICE_ACCOUNT_JSON
# The private service-account key is NEVER sent to the browser.
FIREBASE_ENABLED = False
firebase_db = None
FIREBASE_STORAGE_ENABLED = False
firebase_bucket = None

def init_firebase():
    global FIREBASE_ENABLED, firebase_db, FIREBASE_STORAGE_ENABLED, firebase_bucket
    try:
        raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw:
            return False

        info = json.loads(raw)

        if not firebase_admin._apps:
            firebase_admin.initialize_app(
                credentials.Certificate(info)
            )

        firebase_db = firestore.client()
        FIREBASE_ENABLED = True

        # Firebase Storage is optional because the project must have Storage/billing enabled.
        if firebase_storage is not None:
            try:
                firebase_bucket = firebase_storage.bucket()
                FIREBASE_STORAGE_ENABLED = True
            except Exception as storage_error:
                firebase_bucket = None
                FIREBASE_STORAGE_ENABLED = False
                print("Firebase Storage unavailable (local upload fallback):", storage_error)
        return True
    except Exception as e:
        print("Firebase init warning:", e)
        FIREBASE_ENABLED = False
        firebase_db = None
        FIREBASE_STORAGE_ENABLED = False
        firebase_bucket = None
        return False

init_firebase()

def store_uploaded_bytes(content, folder, filename, content_type):
    """Store media in Firebase Storage when available; otherwise keep the existing local upload path."""
    if FIREBASE_STORAGE_ENABLED and firebase_bucket is not None:
        blob_name = f"prem-milan/{folder}/{filename}"
        blob = firebase_bucket.blob(blob_name)
        token = secrets.token_urlsafe(32)
        blob.metadata = {"firebaseStorageDownloadTokens": token}
        blob.upload_from_string(content, content_type=content_type or "application/octet-stream")
        bucket_name = firebase_bucket.name
        return f"https://firebasestorage.googleapis.com/v0/b/{bucket_name}/o/{quote(blob_name, safe='')}?alt=media&token={quote(token, safe='')}"
    return None

def delete_stored_media(media_url, local_dir=None):
    if not media_url:
        return
    try:
        if media_url.startswith("https://firebasestorage.googleapis.com/") and FIREBASE_STORAGE_ENABLED and firebase_bucket is not None:
            marker = "/o/"
            if marker in media_url:
                encoded = media_url.split(marker,1)[1].split("?",1)[0]
                blob_name = unquote(encoded)
                firebase_bucket.blob(blob_name).delete()
            return
        if local_dir and media_url.startswith("/uploads/"):
            path = os.path.join(BASE_DIR, media_url.lstrip("/"))
            if os.path.isfile(path):
                os.remove(path)
    except Exception as storage_delete_error:
        print("Media delete warning:", storage_delete_error)

def firebase_email_for_mobile(mobile):
    digits = "".join(ch for ch in str(mobile) if ch.isdigit())
    return f"{digits}@premmilan.app"

def ensure_firebase_user(mobile, password, name="Prem Milan User"):
    """Create/update the hidden Firebase Auth identity for mobile/password login."""
    if not FIREBASE_ENABLED:
        return None

    email = firebase_email_for_mobile(mobile)
    try:
        u = firebase_auth.get_user_by_email(email)
        firebase_auth.update_user(
            u.uid,
            password=password,
            display_name=name or "Prem Milan User"
        )
        return u
    except firebase_auth.UserNotFoundError:
        return firebase_auth.create_user(
            email=email,
            password=password,
            display_name=name or "Prem Milan User"
        )
    except Exception as e:
        print("Firebase Auth sync warning:", e)
        return None

def firebase_sync_all():
    """Mirror every SQLite table into private Firestore collections."""
    if not FIREBASE_ENABLED:
        return

    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        c = db.cursor()

        c.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE name
                WHEN 'users' THEN 1
                WHEN 'admin' THEN 2
                ELSE 3
            END, name
        """)
        tables = [r[0] for r in c.fetchall()]

        for table in tables:
            c.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in c.fetchall()]
            c.execute(f"SELECT * FROM {table}")
            rows = c.fetchall()
            collection = db_firestore_collection = firebase_db.collection(
                f"premmilan_{table}"
            )

            existing = set()
            for row in rows:
                data = dict(row)
                doc_id = str(data.get("id", uuid.uuid4()))
                # Firestore gets the hash, not the plaintext password.
                collection.document(doc_id).set(data, merge=True)
                existing.add(doc_id)

            # Remove stale mirror documents.
            for doc in collection.stream():
                if doc.id not in existing:
                    collection.document(doc.id).delete()

        db.close()
        print("Firebase mirror sync: OK")
    except Exception as e:
        print("Firebase mirror sync warning:", e)

_firebase_sync_lock = threading.Lock()
_firebase_sync_pending = False

def firebase_sync_async():
    global _firebase_sync_pending
    if not FIREBASE_ENABLED:
        return
    with _firebase_sync_lock:
        if _firebase_sync_pending:
            return
        _firebase_sync_pending = True

    def worker():
        global _firebase_sync_pending
        try:
            # Coalesce a burst of writes (login/register/chat/admin actions).
            import time
            time.sleep(10.0)
            firebase_sync_all()
        finally:
            with _firebase_sync_lock:
                _firebase_sync_pending = False

    threading.Thread(target=worker, daemon=True).start()


@app.middleware("http")
async def media_cache_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/uploads/"):
        response.headers["Cache-Control"] = "public, max-age=604800, stale-while-revalidate=86400"
    return response


def firebase_restore_if_empty():
    """Restore local SQLite from Firestore after a Render restart/redeploy."""
    if not FIREBASE_ENABLED:
        return

    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        c = db.cursor()

        c.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE name
                WHEN 'users' THEN 1
                WHEN 'admin' THEN 2
                ELSE 3
            END, name
        """)
        tables = [r[0] for r in c.fetchall()]
        restored_any = False

        for table in tables:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            if c.fetchone()[0] > 0:
                continue

            docs = list(firebase_db.collection(f"premmilan_{table}").stream())
            if not docs:
                continue

            c.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in c.fetchall()]
            placeholders = ",".join("?" for _ in cols)
            col_sql = ",".join(cols)

            for doc in docs:
                data = doc.to_dict() or {}
                values = [data.get(col) for col in cols]
                c.execute(
                    f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
                    values
                )
                restored_any = True

        if restored_any:
            db.commit()
            print("Firebase -> SQLite restore: OK")
        db.close()
    except Exception as e:
        print("Firebase restore warning:", e)


UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
PROFILE_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "profile")
CHAT_UPLOADS_DIR = os.path.join(UPLOADS_DIR, "chat")

os.makedirs(PROFILE_UPLOADS_DIR, exist_ok=True)
os.makedirs(CHAT_UPLOADS_DIR, exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory=UPLOADS_DIR),
    name="uploads"
)

# =====================================================
# DATABASE
# =====================================================

# Render Free does not provide /var/data unless a persistent disk is attached.
# Keep SQLite beside this main.py so the service can start on the Free instance.
# NOTE: Render's ephemeral filesystem can reset this database on redeploy/restart.
DB_PATH = os.path.join(BASE_DIR, "database.db")

conn = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

conn.row_factory = sqlite3.Row

cursor = conn.cursor()

# =====================================================
# USERS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(

id INTEGER PRIMARY KEY AUTOINCREMENT,

name TEXT NOT NULL,

mobile TEXT UNIQUE NOT NULL,

password TEXT NOT NULL,

gender TEXT,

looking_for TEXT,

dob TEXT,

age INTEGER,

height TEXT,

religion TEXT,

caste TEXT,

education TEXT,

occupation TEXT,

about TEXT,

photo TEXT,

city TEXT,

state TEXT,

country TEXT,

is_verified INTEGER DEFAULT 0,

is_premium INTEGER DEFAULT 0,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# Safe migration for existing users table.
for _sql in [
    "ALTER TABLE users ADD COLUMN is_deleted INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN deleted_at TEXT",
]:
    try:
        cursor.execute(_sql)
    except Exception:
        pass
conn.commit()

# =====================================================
# PROFILE PHOTOS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS profile_photos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    photo TEXT NOT NULL,
    is_main INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

# Backfill each existing main profile photo into the gallery table once.
try:
    cursor.execute("""
        SELECT id, photo FROM users
        WHERE photo IS NOT NULL AND TRIM(photo) != ''
    """)
    for _u in cursor.fetchall():
        cursor.execute("SELECT id FROM profile_photos WHERE user_id=?", (_u[0],))
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO profile_photos(user_id,photo,is_main) VALUES(?,?,1)",
                (_u[0], _u[1])
            )
    conn.commit()
except Exception as _e:
    print("Profile photo backfill warning:", _e)

# Useful indexes for fast search, chat and profile lookups.
for _sql in [
    "CREATE INDEX IF NOT EXISTS idx_users_gender_age ON users(gender,age)",
    "CREATE INDEX IF NOT EXISTS idx_users_city ON users(city)",
    "CREATE INDEX IF NOT EXISTS idx_users_religion ON users(religion)",
    "CREATE INDEX IF NOT EXISTS idx_users_caste ON users(caste)",
    "CREATE INDEX IF NOT EXISTS idx_messages_sender_receiver ON messages(sender_id,receiver_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_receiver_sender ON messages(receiver_id,sender_id)",
    "CREATE INDEX IF NOT EXISTS idx_profile_views_pair ON profile_views(viewer_id,profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_profile_photos_user ON profile_photos(user_id,is_main)",
]:
    try:
        cursor.execute(_sql)
    except Exception:
        pass
conn.commit()

# =====================================================
# INTERESTS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS interests(

id INTEGER PRIMARY KEY AUTOINCREMENT,

sender_id INTEGER NOT NULL,

receiver_id INTEGER NOT NULL,

status TEXT DEFAULT 'Pending',

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# MESSAGES TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages(

id INTEGER PRIMARY KEY AUTOINCREMENT,

sender_id INTEGER NOT NULL,

receiver_id INTEGER NOT NULL,

message TEXT NOT NULL,

is_read INTEGER DEFAULT 0,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# Safe chat-media migration for existing databases
try:
    cursor.execute("PRAGMA table_info(messages)")
    _message_cols = {row[1] for row in cursor.fetchall()}
    if "media_url" not in _message_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN media_url TEXT")
    if "media_type" not in _message_cols:
        cursor.execute("ALTER TABLE messages ADD COLUMN media_type TEXT DEFAULT 'text'")
    conn.commit()
except Exception as _e:
    print("Chat media migration warning:", _e)

# =====================================================
# NOTIFICATIONS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS notifications(

id INTEGER PRIMARY KEY AUTOINCREMENT,

user_id INTEGER NOT NULL,

title TEXT,

message TEXT,

is_read INTEGER DEFAULT 0,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# FAVORITES TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS favorites(

id INTEGER PRIMARY KEY AUTOINCREMENT,

user_id INTEGER NOT NULL,

favorite_user INTEGER NOT NULL,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# PROFILE VIEWS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS profile_views(

id INTEGER PRIMARY KEY AUTOINCREMENT,

viewer_id INTEGER NOT NULL,

profile_id INTEGER NOT NULL,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# BLOCKS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS blocks(

id INTEGER PRIMARY KEY AUTOINCREMENT,

user_id INTEGER NOT NULL,

blocked_user INTEGER NOT NULL,

reason TEXT,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# REPORTS TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS reports(

id INTEGER PRIMARY KEY AUTOINCREMENT,

reporter_id INTEGER NOT NULL,

reported_id INTEGER NOT NULL,

reason TEXT,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# PREMIUM TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS premium(

id INTEGER PRIMARY KEY AUTOINCREMENT,

user_id INTEGER NOT NULL,

plan TEXT,

amount REAL,

payment_id TEXT,

payment_status TEXT,

start_date TEXT,

end_date TEXT,

status TEXT,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

# =====================================================
# ADMIN TABLE
# =====================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS admin(

id INTEGER PRIMARY KEY AUTOINCREMENT,

username TEXT UNIQUE,

password TEXT,

created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

)
""")

conn.commit()

try:
    cursor.execute("ALTER TABLE premium ADD COLUMN amount REAL")
except:
    pass

try:
    cursor.execute("ALTER TABLE premium ADD COLUMN payment_status TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE premium ADD COLUMN status TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE premium ADD COLUMN start_date TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE premium ADD COLUMN end_date TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE blocks ADD COLUMN reason TEXT")
except:
    pass

conn.commit()

# Create indexes again after every table exists.
for _sql in [
    "CREATE INDEX IF NOT EXISTS idx_messages_sender_receiver ON messages(sender_id,receiver_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_receiver_sender ON messages(receiver_id,sender_id)",
    "CREATE INDEX IF NOT EXISTS idx_profile_views_pair ON profile_views(viewer_id,profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_interests_receiver_status ON interests(receiver_id,status)",
    "CREATE INDEX IF NOT EXISTS idx_interests_sender_status ON interests(sender_id,status)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_blocks_user_blocked ON blocks(user_id,blocked_user)",
    "CREATE INDEX IF NOT EXISTS idx_profile_photos_user ON profile_photos(user_id,is_main)",
]:
    try:
        cursor.execute(_sql)
    except Exception:
        pass
conn.commit()

# =====================================================
# MODELS
# =====================================================

class RegisterModel(BaseModel):

    name: str
    mobile: str
    password: str

    gender: str = ""
    looking_for: str = ""
    dob: str = ""
    age: int = 0

    height: str = ""

    religion: str = ""
    caste: str = ""

    education: str = ""
    occupation: str = ""

    about: str = ""

    city: str = ""
    state: str = ""
    country: str = ""


class LoginModel(BaseModel):

    mobile: str
    password: str


class UpdateProfileModel(BaseModel):

    user_id: int

    name: str
    gender: str
    looking_for: str

    dob: str
    age: int

    height: str

    religion: str
    caste: str

    education: str
    occupation: str

    about: str

    city: str
    state: str
    country: str


class ChangePasswordModel(BaseModel):

    user_id: int
    old_password: str
    new_password: str


class SearchModel(BaseModel):

    user_id: int

    gender: str = ""

    age_from: int = 0
    age_to: int = 100

    religion: str = ""
    caste: str = ""

    education: str = ""

    occupation: str = ""

    city: str = ""


class InterestModel(BaseModel):

    sender_id: int
    receiver_id: int
    reason: str = "Reported"

class InterestActionModel(BaseModel):

    interest_id:int

    status:str   


class MessageModel(BaseModel):

    sender_id: int
    receiver_id: int
    message: str


class PremiumModel(BaseModel):

    user_id: int
    plan: str
    amount: float
    payment_id: str = ""


class AdminLoginModel(BaseModel):

    username: str
    password: str


class NotificationModel(BaseModel):

    title: str

    message: str

# =====================================================
# HELPER FUNCTIONS
# =====================================================

def hash_password(password):

    return hashlib.sha256(
        password.encode()
    ).hexdigest()


def create_notification(
    user_id,
    title,
    message
):

    cursor.execute(

        """
        INSERT INTO notifications(

        user_id,
        title,
        message

        )

        VALUES(

        ?,?,?

        )
        """,

        (

            user_id,
            title,
            message

        )

    )

    conn.commit()

# =====================================================
# DEFAULT ADMIN
# =====================================================

cursor.execute(

    "SELECT id FROM admin WHERE username=?",

    (

        "admin",

    )

)

admin = cursor.fetchone()

if admin is None:

    cursor.execute(

        """
        INSERT INTO admin(

        username,
        password

        )

        VALUES(

        ?,?

        )
        """,

        (

            "admin",

            hash_password("admin123")

        )

    )

    conn.commit()

# Restore cloud data if this Render instance starts with an empty SQLite DB.
firebase_restore_if_empty()

def active_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id=? AND COALESCE(is_deleted,0)=0", (int(user_id),))
    return cursor.fetchone()

def ensure_active_user(user_id):
    return active_user(user_id) is not None

def users_blocked(user_a, user_b):
    cursor.execute("""
        SELECT id FROM blocks
        WHERE (user_id=? AND blocked_user=?)
           OR (user_id=? AND blocked_user=?)
        LIMIT 1
    """, (int(user_a), int(user_b), int(user_b), int(user_a)))
    return cursor.fetchone() is not None

def ensure_pair_available(user_a, user_b):
    if not ensure_active_user(user_a) or not ensure_active_user(user_b):
        return False, "This account is unavailable."
    if users_blocked(user_a, user_b):
        return False, "Chat is blocked between these users."
    return True, ""

# =====================================================
# HOME
# =====================================================

@app.get("/")
def home():

    return {
        "status": True,
        "message": " Prem Milan Backend Running ❤️"
    }

# =====================================================
# SESSION STATUS
# =====================================================
@app.get("/session-status/{user_id}")
def session_status(user_id:int):
    user=active_user(user_id)
    return {
        "status": True,
        "active": bool(user),
        "message": "Account active" if user else "This profile has been disabled by admin."
    }


# =====================================================
# TOTAL USERS
# =====================================================

@app.get("/total-users")
def total_users():

    cursor.execute("SELECT COUNT(*) FROM users")

    total = cursor.fetchone()[0]

    return {
        "status": True,
        "total_users": total
    }

# =====================================================
# REGISTER
# =====================================================

@app.post("/register")
def register(user: RegisterModel):

    cursor.execute(
        "SELECT id FROM users WHERE mobile=?",
        (user.mobile,)
    )

    if cursor.fetchone():

        return {
            "status": False,
            "message": "Mobile Number Already Registered"
        }

    cursor.execute(
        """
        INSERT INTO users(

        name,
        mobile,
        password,
        gender,
        looking_for,
        dob,
        age,
        height,
        religion,
        caste,
        education,
        occupation,
        about,
        city,
        state,
        country

        )

        VALUES(

        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?

        )
        """,

        (

            user.name,
            user.mobile,
            hash_password(user.password),
            user.gender,
            user.looking_for,
            user.dob,
            user.age,
            user.height,
            user.religion,
            user.caste,
            user.education,
            user.occupation,
            user.about,
            user.city,
            user.state,
            user.country

        )

    )

    conn.commit()

    firebase_user = ensure_firebase_user(
        user.mobile,
        user.password,
        user.name
    )
    firebase_sync_async()

    return {

        "status": True,
        "message": "Registration Successful",
        "firebase_enabled": bool(firebase_user)

    }

# =====================================================
# LOGIN
# =====================================================

@app.post("/login")
def login(user: LoginModel):

    cursor.execute(
        "SELECT * FROM users WHERE mobile=?",
        (user.mobile,)
    )

    data = cursor.fetchone()

    if data is None:

        return {
            "status": False,
            "message": "User Not Found"
        }

    if int(data["is_deleted"] or 0) == 1:
        return {"status":False,"message":"This profile has been disabled by admin."}

    if data["password"] != hash_password(user.password):

        return {
            "status": False,
            "message": "Wrong Password"
        }

    firebase_user = ensure_firebase_user(
        data["mobile"],
        user.password,
        data["name"]
    )
    firebase_sync_async()

    return {

        "status": True,
        "message": "Login Successful",
        "firebase_enabled": bool(firebase_user),
        "firebase_email": firebase_email_for_mobile(data["mobile"]) if FIREBASE_ENABLED else "",

        "user": {

            "id": data["id"],
            "name": data["name"],
            "mobile": data["mobile"],
            "gender": data["gender"],
            "looking_for": data["looking_for"],
            "photo": data["photo"],
            "city": data["city"],
            "state": data["state"],
            "country": data["country"],
            "is_verified": data["is_verified"],
            "is_premium": data["is_premium"]

        }

    }

# =====================================================
# GET PROFILE
# =====================================================

@app.get("/profile/{user_id}")

def get_profile(user_id:int):

    cursor.execute(

        """

        SELECT *

        FROM users

        WHERE id=? AND COALESCE(is_deleted,0)=0

        """,

        (

            user_id,

        )

    )

    user=cursor.fetchone()

    if user is None:

        return{

            "status":False,

            "message":"User Not Found"

        }

    profile=dict(user)
    profile.pop("password", None)
    return{

        "status":True,

        "profile":profile

    }

# =====================================================
# FULL PUBLIC PROFILE + GALLERY
# =====================================================

@app.get("/profile-full/{user_id}")
def profile_full(user_id:int):
    cursor.execute("SELECT * FROM users WHERE id=? AND COALESCE(is_deleted,0)=0", (user_id,))
    user = cursor.fetchone()
    if user is None:
        return {"status":False,"message":"Profile not found"}

    cursor.execute("""
        SELECT id, photo, is_main, created_at
        FROM profile_photos
        WHERE user_id=?
        ORDER BY is_main DESC, id ASC
        LIMIT 4
    """, (user_id,))
    photos = [dict(r) for r in cursor.fetchall()]

    # Legacy safety: expose users.photo even if gallery backfill has not run.
    if not photos and user["photo"]:
        photos = [{"id":0,"photo":user["photo"],"is_main":1}]

    profile=dict(user)
    profile.pop("password", None)

    # Keep the legacy users.photo synchronized with the selected main gallery photo.
    if photos:
        main_photo = next((p for p in photos if int(p.get("is_main") or 0) == 1), photos[0])
        if main_photo.get("photo") and profile.get("photo") != main_photo.get("photo"):
            profile["photo"] = main_photo["photo"]

    return {
        "status":True,
        "profile":profile,
        "photos":photos
    }



# Compatibility alias for older frontend deployments.
@app.get("/user-profile/{user_id}")
def user_profile_alias(user_id: int):
    return profile_full(user_id)

@app.post("/upload-profile-photos/{user_id}")
def upload_profile_photos(user_id:int, photos:list[UploadFile]=File(...)):
    cursor.execute("SELECT id,photo FROM users WHERE id=? AND COALESCE(is_deleted,0)=0", (user_id,))
    user = cursor.fetchone()
    if user is None:
        return {"status":False,"message":"User not found"}

    allowed={".jpg",".jpeg",".png",".webp",".gif"}
    cursor.execute("SELECT COUNT(*) FROM profile_photos WHERE user_id=?", (user_id,))
    current_count = int(cursor.fetchone()[0])
    remaining = max(0, 4-current_count)
    if remaining <= 0:
        return {"status":False,"message":"Maximum 4 profile photos allowed."}

    saved=[]
    try:
        for upload in photos[:remaining]:
            ext=os.path.splitext(upload.filename or "")[1].lower()
            if ext not in allowed:
                continue
            content=upload.file.read()
            if len(content) > 8*1024*1024:
                continue
            filename=f"{uuid.uuid4().hex}{ext}"
            content_type=getattr(upload, "content_type", None) or "image/jpeg"
            url=store_uploaded_bytes(content, "profile", filename, content_type)
            if not url:
                filepath=os.path.join(PROFILE_UPLOADS_DIR,filename)
                with open(filepath,"wb") as f:
                    f.write(content)
                url=f"/uploads/profile/{filename}"
            cursor.execute("SELECT COUNT(*) FROM profile_photos WHERE user_id=?",(user_id,))
            has_any = cursor.fetchone()[0] > 0
            is_main = 0 if has_any else 1
            cursor.execute(
                "INSERT INTO profile_photos(user_id,photo,is_main) VALUES(?,?,?)",
                (user_id,url,is_main)
            )
            saved.append(url)
            try: upload.file.close()
            except Exception: pass

        if saved:
            cursor.execute("SELECT photo FROM profile_photos WHERE user_id=? ORDER BY is_main DESC,id ASC LIMIT 1",(user_id,))
            main = cursor.fetchone()
            if main:
                cursor.execute("UPDATE users SET photo=? WHERE id=?",(main["photo"],user_id))
        conn.commit()
        firebase_sync_async()
        return {"status":True,"message":f"{len(saved)} photo(s) uploaded","photos":saved}
    except Exception as e:
        conn.rollback()
        return {"status":False,"message":"Unable to upload photos."}


@app.post("/delete-profile-photo/{user_id}")
def delete_profile_photo(user_id:int, data:dict):
    if not ensure_active_user(user_id):
        return {"status":False,"message":"This profile has been disabled by admin."}
    photo_id=int(data.get("photo_id",0))
    cursor.execute("SELECT * FROM profile_photos WHERE id=? AND user_id=? AND EXISTS(SELECT 1 FROM users WHERE id=? AND COALESCE(is_deleted,0)=0)",(photo_id,user_id,user_id))
    row=cursor.fetchone()
    if row is None:
        return {"status":False,"message":"Photo not found"}

    was_main=int(row["is_main"] or 0)==1
    old_url=row["photo"]
    cursor.execute("DELETE FROM profile_photos WHERE id=?",(photo_id,))

    delete_stored_media(old_url, PROFILE_UPLOADS_DIR)

    cursor.execute("SELECT id,photo FROM profile_photos WHERE user_id=? ORDER BY is_main DESC,id ASC LIMIT 1",(user_id,))
    replacement=cursor.fetchone()
    if replacement:
        if was_main:
            cursor.execute("UPDATE profile_photos SET is_main=0 WHERE user_id=?",(user_id,))
            cursor.execute("UPDATE profile_photos SET is_main=1 WHERE id=?",(replacement["id"],))
        cursor.execute("UPDATE users SET photo=? WHERE id=?",(replacement["photo"],user_id))
    else:
        cursor.execute("UPDATE users SET photo='' WHERE id=?",(user_id,))

    conn.commit()
    firebase_sync_async()
    return {"status":True,"message":"Photo deleted"}


@app.post("/set-main-photo/{user_id}")
def set_main_photo(user_id:int, data:dict):
    if not ensure_active_user(user_id):
        return {"status":False,"message":"This profile has been disabled by admin."}
    photo_id=int(data.get("photo_id",0))
    cursor.execute("SELECT id,photo FROM profile_photos WHERE id=? AND user_id=?",(photo_id,user_id))
    row=cursor.fetchone()
    if row is None:
        return {"status":False,"message":"Photo not found"}
    cursor.execute("UPDATE profile_photos SET is_main=0 WHERE user_id=?",(user_id,))
    cursor.execute("UPDATE profile_photos SET is_main=1 WHERE id=?",(photo_id,))
    cursor.execute("UPDATE users SET photo=? WHERE id=?",(row["photo"],user_id))
    conn.commit()
    firebase_sync_async()
    return {"status":True,"message":"Main photo updated"}


# =====================================================
# UPLOAD PROFILE PHOTO
# =====================================================

@app.post("/upload-profile-photo/{user_id}")
def upload_profile_photo(
    user_id:int,
    photo: UploadFile = File(...)
):
    cursor.execute("SELECT photo FROM users WHERE id=?", (user_id,))
    user = cursor.fetchone()
    if user is None:
        return {"status":False,"message":"User Not Found"}

    allowed={".jpg",".jpeg",".png",".webp",".gif"}
    ext=os.path.splitext(photo.filename or "")[1].lower()
    if ext not in allowed:
        return {"status":False,"message":"Only JPG, PNG, WEBP or GIF images are allowed."}

    try:
        content=photo.file.read()
        if len(content)>8*1024*1024:
            return {"status":False,"message":"Photo must be below 8 MB."}
        filename=str(uuid.uuid4())+ext
        filepath=os.path.join(PROFILE_UPLOADS_DIR,filename)
        with open(filepath,"wb") as buffer:
            buffer.write(content)
        photo_url="/uploads/profile/"+filename

        # Remove previous main-photo record and file.
        cursor.execute("SELECT photo FROM profile_photos WHERE user_id=? AND is_main=1 LIMIT 1",(user_id,))
        old_main=cursor.fetchone()
        if old_main and old_main["photo"] and old_main["photo"] != photo_url:
            old_path=os.path.join(BASE_DIR,old_main["photo"].lstrip("/"))
            try:
                if os.path.isfile(old_path): os.remove(old_path)
            except Exception: pass
        cursor.execute("DELETE FROM profile_photos WHERE user_id=? AND is_main=1",(user_id,))
        cursor.execute("INSERT INTO profile_photos(user_id,photo,is_main) VALUES(?,?,1)",(user_id,photo_url))
        cursor.execute("UPDATE users SET photo=? WHERE id=?",(photo_url,user_id))
        conn.commit()
        firebase_sync_async()
        return {"status":True,"message":"Profile Photo Uploaded Successfully","photo":photo_url}
    except Exception as e:
        try:
            if 'filepath' in locals() and os.path.exists(filepath): os.remove(filepath)
        except Exception: pass
        return {"status":False,"message":"Unable to upload photo."}
    finally:
        try: photo.file.close()
        except Exception: pass


# =====================================================
# UPDATE PROFILE
# =====================================================

@app.post("/update-profile")

def update_profile(user:UpdateProfileModel):

    if not ensure_active_user(user.user_id):
        return {"status":False,"message":"This profile has been disabled by admin."}

    cursor.execute(

        """

        UPDATE users

        SET

        name=?,

        gender=?,

        looking_for=?,

        dob=?,

        age=?,

        height=?,

        religion=?,

        caste=?,

        education=?,

        occupation=?,

        about=?,

        city=?,

        state=?,

        country=?

        WHERE id=?

        """,

        (

            user.name,

            user.gender,

            user.looking_for,

            user.dob,

            user.age,

            user.height,

            user.religion,

            user.caste,

            user.education,

            user.occupation,

            user.about,

            user.city,

            user.state,

            user.country,

            user.user_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Profile Updated Successfully"

    }


# =====================================================
# CHANGE PASSWORD
# =====================================================

@app.post("/change-password")

def change_password(data:ChangePasswordModel):

    if not ensure_active_user(data.user_id):
        return {"status":False,"message":"This profile has been disabled by admin."}

    cursor.execute(

        """

        SELECT password

        FROM users

        WHERE id=?

        """,

        (

            data.user_id,

        )

    )

    user=cursor.fetchone()

    if user is None:

        return{

            "status":False,

            "message":"User Not Found"

        }

    if user["password"]!=hash_password(data.old_password):

        return{

            "status":False,

            "message":"Old Password Incorrect"

        }

    cursor.execute(

        """

        UPDATE users

        SET password=?

        WHERE id=?

        """,

        (

            hash_password(data.new_password),

            data.user_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Password Changed Successfully"

    }

# =====================================================
# SEARCH USERS
# =====================================================

@app.post("/search")
def search_users(data: SearchModel):

    query = """

    SELECT

    id,
    name,
    age,
    height,
    religion,
    caste,
    education,
    occupation,
    city,
    photo

    FROM users

    WHERE id != ? AND COALESCE(is_deleted,0)=0

    """

    params = [data.user_id]

    if data.gender != "":
        query += " AND gender=?"
        params.append(data.gender)

    query += " AND age>=?"
    params.append(data.age_from)

    query += " AND age<=?"
    params.append(data.age_to)

    if data.religion != "":
        query += " AND religion LIKE ?"
        params.append("%" + data.religion + "%")

    if data.caste != "":
        query += " AND caste LIKE ?"
        params.append("%" + data.caste + "%")

    if data.education != "":
        query += " AND education LIKE ?"
        params.append("%" + data.education + "%")

    if data.occupation != "":
        query += " AND occupation LIKE ?"
        params.append("%" + data.occupation + "%")

    if data.city != "":
        query += " AND city LIKE ?"
        params.append("%" + data.city + "%")

    query += " ORDER BY id DESC LIMIT 60"

    cursor.execute(query, tuple(params))

    users = cursor.fetchall()
    users = [row for row in users if not users_blocked(data.user_id, row["id"])]

    return {

        "status": True,

        "total": len(users),

        "profiles": [dict(row) for row in users]

    }

# =====================================================
# SEND INTEREST
# =====================================================

@app.post("/send-interest")
def send_interest(data: InterestModel):

    if data.sender_id == data.receiver_id:

        return {

            "status": False,
            "message": "Invalid User"

        }

    available, reason = ensure_pair_available(data.sender_id, data.receiver_id)
    if not available:
        return {"status":False,"message":reason}

    cursor.execute(

        """
        SELECT id

        FROM interests

        WHERE sender_id=?
        AND receiver_id=?

        """,

        (
            data.sender_id,
            data.receiver_id
        )

    )

    if cursor.fetchone():

        return {

            "status": False,
            "message": "Interest Already Sent"

        }

    cursor.execute(

        """
        INSERT INTO interests(

        sender_id,
        receiver_id

        )

        VALUES(

        ?,?

        )

        """,

        (
            data.sender_id,
            data.receiver_id
        )

    )

    conn.commit()

    create_notification(

        data.receiver_id,

        "New Interest ❤️",

        "Someone sent you an interest."

    )

    return {

        "status": True,
        "message": "Interest Sent Successfully"

    }


# =====================================================
# INTEREST ACTION (ACCEPT / REJECT)
# =====================================================

@app.post("/interest-action")
def interest_action(data: InterestActionModel):

    cursor.execute(
        """
        SELECT id
        FROM interests
        WHERE id=?
        """,
        (data.interest_id,)
    )

    interest = cursor.fetchone()

    if interest is None:

        return {
            "status": False,
            "message": "Interest Not Found"
        }

    cursor.execute(
        """
        UPDATE interests
        SET status=?
        WHERE id=?
        """,
        (
            data.status,
            data.interest_id
        )
    )

    conn.commit()

    return {
        "status": True,
        "message": "Interest " + data.status + " Successfully ❤️"
    }

# =====================================================
# RECEIVED INTERESTS
# =====================================================

@app.get("/received-interests/{user_id}")

def received_interests(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","interests":[]}

    cursor.execute(

        """

        SELECT

        interests.id AS interest_id,

        interests.status,

        users.id AS sender_id,

        users.name,

        users.age,

        users.height,

        users.religion,

        users.caste,

        users.education,

        users.occupation,

        users.city,

        users.photo

        FROM interests

        INNER JOIN users

        ON interests.sender_id = users.id

        WHERE interests.receiver_id=?

        ORDER BY interests.id DESC

        """,

        (user_id,)

    )

    data = cursor.fetchall()

    return{

        "status":True,

        "total":len(data),

        "interests":[dict(row) for row in data]

    }


# =====================================================
# SENT INTERESTS
# =====================================================

@app.get("/sent-interests/{user_id}")
def sent_interests(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","interests":[]}

    cursor.execute(

        """
        SELECT

        interests.id,
        interests.status,

        users.id as receiver_id,
        users.name,
        users.age,
        users.city,
        users.photo

        FROM interests

        JOIN users

        ON interests.receiver_id = users.id

        WHERE interests.sender_id = ?

        ORDER BY interests.id DESC

        """,

        (
            user_id,
        )

    )

    data = cursor.fetchall()

    return {

        "status": True,

        "total": len(data),

        "interests": [dict(i) for i in data]

    }

# =====================================================
# SEND MESSAGE
# =====================================================

@app.post("/send-message")

def send_message(data: MessageModel):

    if data.sender_id == data.receiver_id:

        return{

            "status":False,

            "message":"Invalid User"

        }

    available, reason = ensure_pair_available(data.sender_id, data.receiver_id)
    if not available:
        return {"status":False,"message":reason,"blocked":True}

    cursor.execute(

        """

        INSERT INTO messages(

        sender_id,

        receiver_id,

        message

        )

        VALUES(

        ?,?,?

        )

        """,

        (

            data.sender_id,

            data.receiver_id,

            data.message

        )

    )

    conn.commit()

    create_notification(

        data.receiver_id,

        "New Message 💬",

        "You received a new message."

    )

    return{

        "status":True,

        "message":"Message Sent Successfully"

    }


# =====================================================
# CHAT PHOTO UPLOAD / DELETE
# =====================================================

@app.post("/upload-chat-photo/{sender_id}/{receiver_id}")
def upload_chat_photo(sender_id:int, receiver_id:int, photo:UploadFile=File(...)):

    if sender_id == receiver_id:
        return {"status":False,"message":"Invalid User"}

    available, reason = ensure_pair_available(sender_id, receiver_id)
    if not available:
        return {"status":False,"message":reason,"blocked":True}

    allowed={".jpg",".jpeg",".png",".webp",".gif"}
    ext=os.path.splitext(photo.filename or "")[1].lower()
    if ext not in allowed:
        return {"status":False,"message":"Only JPG, PNG, WEBP or GIF images are allowed."}

    filename=f"{uuid.uuid4().hex}{ext}"
    path=os.path.join(CHAT_UPLOADS_DIR,filename)
    try:
        content=photo.file.read()
        if len(content)>8*1024*1024:
            return {"status":False,"message":"Photo must be below 8 MB."}
        content_type=getattr(photo,"content_type",None) or "image/jpeg"
        url=store_uploaded_bytes(content,"chat",filename,content_type)
        if not url:
            with open(path,"wb") as f:
                f.write(content)
            url=f"/uploads/chat/{filename}"
        cursor.execute("""
            INSERT INTO messages(sender_id,receiver_id,message,media_url,media_type)
            VALUES(?,?,?,?,?)
        """,(sender_id,receiver_id,"[Photo]",url,"image"))
        conn.commit()
        create_notification(receiver_id,"New Photo 💬","You received a new photo.")
        return {"status":True,"message":"Photo Sent","media_url":url}
    except Exception as e:
        try:
            if os.path.exists(path): os.remove(path)
        except Exception:
            pass
        return {"status":False,"message":"Unable to upload photo."}
    finally:
        try: photo.file.close()
        except Exception: pass

@app.delete("/delete-message/{message_id}")
def delete_message(message_id:int, user_id:int):
    if not ensure_active_user(user_id):
        return {"status":False,"message":"This profile has been disabled by admin."}
    cursor.execute("SELECT * FROM messages WHERE id=?",(message_id,))
    row=cursor.fetchone()
    if row is None:
        return {"status":False,"message":"Message not found"}
    if int(row["sender_id"]) != int(user_id):
        return {"status":False,"message":"You can delete only your own message."}

    media=row["media_url"] if "media_url" in row.keys() else None
    cursor.execute("DELETE FROM messages WHERE id=?",(message_id,))
    conn.commit()

    if media:
        try:
            filename=os.path.basename(media)
            path=os.path.join(CHAT_UPLOADS_DIR,filename)
            if os.path.isfile(path): os.remove(path)
        except Exception:
            pass

    return {"status":True,"message":"Message deleted"}

# =====================================================
# CHAT HISTORY
# =====================================================

@app.get("/chat/{sender_id}/{receiver_id}")

def get_chat(sender_id:int,receiver_id:int):

    available, reason = ensure_pair_available(sender_id, receiver_id)
    if not available:
        return {"status":True,"blocked":True,"message":reason,"messages":[]}

    cursor.execute(

        """

        SELECT * FROM (
            SELECT *
            FROM messages
            WHERE (sender_id=? AND receiver_id=?)
               OR (sender_id=? AND receiver_id=?)
            ORDER BY id DESC
            LIMIT 200
        ) recent_messages
        ORDER BY id ASC

        """,

        (

            sender_id,

            receiver_id,

            receiver_id,

            sender_id

        )

    )

    chats=cursor.fetchall()

    return{

        "status":True,
        "blocked":False,

        "messages":[dict(row) for row in chats]

    }


# =====================================================
# CONVERSATION LIST
# =====================================================

@app.get("/conversations/{user_id}")
def conversations(user_id:int):
    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","conversations":[]}
    cursor.execute("""
        SELECT DISTINCT
            u.id, u.name, u.photo, u.city
        FROM users u
        INNER JOIN (
            SELECT DISTINCT
                CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END AS partner_id
            FROM messages
            WHERE sender_id=? OR receiver_id=?
        ) p ON p.partner_id=u.id
        WHERE COALESCE(u.is_deleted,0)=0
          AND NOT EXISTS (
              SELECT 1 FROM blocks b
              WHERE (b.user_id=? AND b.blocked_user=u.id)
                 OR (b.user_id=u.id AND b.blocked_user=?)
          )
        ORDER BY u.id DESC
        LIMIT 50
    """,(user_id,user_id,user_id,user_id,user_id))
    users=[dict(r) for r in cursor.fetchall()]
    return {
        "status":True,
        "total":len(users),
        "conversations":users
    }


# =====================================================
# UNREAD MESSAGE COUNT
# =====================================================

@app.get("/unread-count/{user_id}")

def unread_count(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","unread":0}

    cursor.execute(

        """

        SELECT COUNT(*)

        FROM messages

        WHERE receiver_id=?

        AND is_read=0

        """,

        (

            user_id,

        )

    )

    total=cursor.fetchone()[0]

    return{

        "status":True,

        "unread":total

    }

# =====================================================
# ADD TO FAVORITES
# =====================================================

@app.post("/favorite")

def add_favorite(data: InterestModel):

    if not ensure_active_user(data.sender_id) or not ensure_active_user(data.receiver_id):
        return {"status":False,"message":"Profile unavailable."}
    if users_blocked(data.sender_id,data.receiver_id):
        return {"status":False,"message":"This user is blocked."}

    cursor.execute(

        """

        SELECT id

        FROM favorites

        WHERE user_id=?
        AND favorite_user=?

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    if cursor.fetchone():

        return{

            "status":False,

            "message":"Already Added"

        }

    cursor.execute(

        """

        INSERT INTO favorites(

        user_id,

        favorite_user

        )

        VALUES(

        ?,?

        )

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Added To Favorites"

    }


# =====================================================
# REMOVE FAVORITE
# =====================================================

@app.post("/remove-favorite")

def remove_favorite(data:InterestModel):

    if not ensure_active_user(data.sender_id) or not ensure_active_user(data.receiver_id):
        return {"status":False,"message":"Profile unavailable."}

    cursor.execute(

        """

        DELETE FROM favorites

        WHERE user_id=?
        AND favorite_user=?

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Favorite Removed"

    }


# =====================================================
# MY FAVORITES
# =====================================================

@app.get("/favorites/{user_id}")

def my_favorites(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","favorites":[]}

    cursor.execute(

        """

        SELECT

        users.id,
        users.name,
        users.age,
        users.city,
        users.photo

        FROM favorites

        JOIN users

        ON favorites.favorite_user=users.id

        WHERE favorites.user_id=?

        """,

        (

            user_id,

        )

    )

    data=cursor.fetchall()

    return{

        "status":True,

        "favorites":[dict(i) for i in data]

    }


# =====================================================
# PROFILE VIEW
# =====================================================

@app.post("/profile-view")

def profile_view(data: InterestModel):

    cursor.execute(

        """

        SELECT id

        FROM profile_views

        WHERE viewer_id=?

        AND profile_id=?

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    if cursor.fetchone():

        return{

            "status":True,

            "message":"Already Viewed"

        }

    cursor.execute(

        """

        INSERT INTO profile_views(

        viewer_id,

        profile_id

        )

        VALUES(

        ?,?

        )

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Profile View Saved"

    }


# =====================================================
# BLOCK USER
# =====================================================

@app.post("/block-user")
def block_user(data: InterestModel):

    if data.sender_id == data.receiver_id:
        return {"status":False,"message":"Invalid User"}
    if not ensure_active_user(data.sender_id) or not ensure_active_user(data.receiver_id):
        return {"status":False,"message":"This account is unavailable."}

    cursor.execute(

        """

        SELECT id

        FROM blocks

        WHERE user_id=?

        AND blocked_user=?

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    if cursor.fetchone():

        return{

            "status":False,

            "message":"User Already Blocked"

        }

    cursor.execute(

        """

        INSERT INTO blocks(

        user_id,

        blocked_user,

        reason

        )

        VALUES(

        ?,?,?

        )

        """,

        (

            data.sender_id,

            data.receiver_id,

            data.reason

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"User Blocked Successfully"

    }

# =====================================================
# REPORT USER
# =====================================================

@app.post("/report-user")
def report_user(data: InterestModel):

    if data.sender_id == data.receiver_id:
        return {"status":False,"message":"Invalid User"}
    if not ensure_active_user(data.sender_id) or not ensure_active_user(data.receiver_id):
        return {"status":False,"message":"This account is unavailable."}

    cursor.execute(

        """

        SELECT id

        FROM reports

        WHERE reporter_id=?

        AND reported_id=?

        """,

        (

            data.sender_id,

            data.receiver_id

        )

    )

    if cursor.fetchone():

        return{

            "status":False,

            "message":"User Already Reported"

        }

    cursor.execute(

        """

        INSERT INTO reports(

        reporter_id,

        reported_id,

        reason

        )

        VALUES(

        ?,?,?

        )

        """,

        (

            data.sender_id,

            data.receiver_id,

            data.reason

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"User Reported Successfully"

    }


@app.get("/admin/reports")
def admin_reports():

    cursor.execute("""

    SELECT

    reports.id,
    u1.name,
    u2.name,
    reports.reason

    FROM reports

    JOIN users u1
    ON reports.reporter_id=u1.id

    JOIN users u2
    ON reports.reported_id=u2.id

    ORDER BY reports.id DESC

    """)

    rows = cursor.fetchall()

    reports=[]

    for row in rows:

        reports.append({

            "id":row[0],
            "reporter":row[1],
            "reported":row[2],
            "reason":row[3]

        })

    return{

        "status":True,

        "reports":reports

    }


# =====================================================
# MY NOTIFICATIONS
# =====================================================

@app.get("/notifications/{user_id}")

def my_notifications(user_id: int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","notifications":[]}

    cursor.execute(

        """

        SELECT

        id,

        title,

        message,

        created_at

        FROM notifications

        WHERE user_id=?

        ORDER BY id DESC

        """,

        (

            user_id,

        )

    )

    data = cursor.fetchall()

    return{

        "status":True,

        "notifications":[dict(i) for i in data]

    }

# =====================================================
# BUY PREMIUM
# =====================================================

@app.post("/buy-premium")
def buy_premium(data: PremiumModel):

    try:

        cursor.execute(
            """
            INSERT INTO premium(
                user_id,
                plan,
                amount,
                payment_id,
                payment_status,
                status
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                data.user_id,
                data.plan,
                data.amount,
                data.payment_id,
                "Success",
                "Active"
            )
        )

        cursor.execute(
            """
            UPDATE users
            SET is_premium=1
            WHERE id=?
            """,
            (data.user_id,)
        )

        conn.commit()

        return {
            "status": True,
            "message": "Premium Activated"
        }

    except Exception as e:

        conn.rollback()

        return {
            "status": False,
            "message": str(e)
        }


# =====================================================
# PREMIUM STATUS
# =====================================================

@app.get("/premium-status/{user_id}")

def premium_status(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin."}

    cursor.execute(

        """

        SELECT is_premium

        FROM users

        WHERE id=?

        """,

        (

            user_id,

        )

    )

    user=cursor.fetchone()

    if user is None:

        return{

            "status":False,

            "message":"User Not Found"

        }

    return{

        "status":True,

        "premium":bool(user["is_premium"])

    }


# =====================================================
# ADMIN LOGIN
# =====================================================

@app.post("/admin-login")

def admin_login(data:AdminLoginModel):

    cursor.execute(

        """

        SELECT *

        FROM admin

        WHERE username=?

        """,

        (

            data.username,

        )

    )

    admin=cursor.fetchone()

    if admin is None:

        return{

            "status":False,

            "message":"Invalid Username"

        }

    if admin["password"]!=hash_password(data.password):

        return{

            "status":False,

            "message":"Invalid Password"

        }

    firebase_admin_user = ensure_firebase_user(
        "admin",
        data.password,
        "Prem Milan Admin"
    )

    firebase_sync_async()

    return{

        "status":True,

        "message":"Admin Login Successful",
        "firebase_enabled": bool(firebase_admin_user),
        "firebase_email": firebase_email_for_mobile("admin") if FIREBASE_ENABLED else ""

    }


# =====================================================
# ADMIN DASHBOARD
# =====================================================

@app.get("/admin/dashboard")

def admin_dashboard():

    cursor.execute("SELECT COUNT(*) FROM users WHERE COALESCE(is_deleted,0)=0")
    total_users=cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM interests")
    total_interests=cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM messages")
    total_messages=cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM premium WHERE status='Active'")
    premium_users=cursor.fetchone()[0]

    return{

        "status":True,

        "total_users":total_users,

        "premium_users":premium_users,

        "total_interests":total_interests,

        "total_messages":total_messages

    }


# =====================================================
# ALL USERS
# =====================================================

@app.get("/admin/users")

def admin_users():

    cursor.execute(

        """

        SELECT

        id,
        name,
        mobile,
        gender,
        city,
        photo,
        is_verified,
        is_premium,
        created_at,
        is_deleted,
        deleted_at

        FROM users

        WHERE COALESCE(is_deleted,0)=0

        ORDER BY id DESC

        """

    )

    users=cursor.fetchall()

    return{

        "status":True,

        "total":len(users),

        "users":[dict(i) for i in users]

    }

@app.get("/admin/blocked-users")
def admin_blocked_users():

    cursor.execute("""

    SELECT

    blocks.id,
    u1.name,
    u2.name,
    blocks.reason,
    blocks.created_at,
    COALESCE(u1.is_deleted,0),
    COALESCE(u2.is_deleted,0)

    FROM blocks

    JOIN users u1
    ON blocks.user_id = u1.id

    JOIN users u2
    ON blocks.blocked_user = u2.id

    ORDER BY blocks.id DESC

    """)

    rows = cursor.fetchall()

    blocks = []

    for row in rows:

        blocks.append({

            "id": row[0],
            "user": row[1],
            "blocked": row[2],
            "reason": row[3] if row[3] else "-",
            "date": row[4],
            "user_deleted": bool(row[5]),
            "blocked_deleted": bool(row[6])

        })

    return {

        "status": True,
        "total_blocked": len(blocks),
        "blocks": blocks

    }

@app.get("/admin/premium-users")
def admin_premium_users():

    cursor.execute("""
        SELECT
        id,
        name,
        mobile,
        city,
        photo
        FROM users
        WHERE is_premium=1
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    users = [dict(row) for row in rows]

    return {
        "status": True,
        "users": users
    }

# =====================================================
# MAKE USER PREMIUM (ADMIN)
# =====================================================

@app.post("/admin/make-premium/{user_id}")
def make_premium(user_id: int):

    cursor.execute(
        """
        UPDATE users
        SET is_premium=1
        WHERE id=?
        """,
        (user_id,)
    )

    conn.commit()

    return {
        "status": True,
        "message": "User is now Premium 👑"
    }

# =====================================================
# SEND NOTIFICATION TO ALL USERS
# =====================================================

@app.post("/admin/send-notification")

def send_notification(data: NotificationModel):

    cursor.execute(

        """

        SELECT id

        FROM users

        """

    )

    users = cursor.fetchall()

    for user in users:

        cursor.execute(

            """

            INSERT INTO notifications(

            user_id,

            title,

            message

            )

            VALUES(

            ?,?,?

            )

            """,

            (

                user["id"],

                data.title,

                data.message

            )

        )

    conn.commit()

    return{

        "status":True,

        "message":"Notification Sent Successfully"

    }

# =====================================================
# DELETE USER
# =====================================================

@app.delete("/admin/delete-user/{user_id}")
def delete_user(user_id:int):
    cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user=cursor.fetchone()
    if user is None:
        return {"status":False,"message":"User not found"}

    mobile=user["mobile"]
    # Mark the account disabled first so an already-open app cannot keep using it.
    cursor.execute("UPDATE users SET is_deleted=1, deleted_at=CURRENT_TIMESTAMP WHERE id=?", (user_id,))

    # Remove relationships/content tied to the disabled profile.
    for sql,args in [
        ("DELETE FROM interests WHERE sender_id=? OR receiver_id=?", (user_id,user_id)),
        ("DELETE FROM favorites WHERE user_id=? OR favorite_user=?", (user_id,user_id)),
        ("DELETE FROM notifications WHERE user_id=?", (user_id,)),
        ("DELETE FROM profile_views WHERE viewer_id=? OR profile_id=?", (user_id,user_id)),
        ("DELETE FROM blocks WHERE user_id=? OR blocked_user=?", (user_id,user_id)),
        ("DELETE FROM reports WHERE reporter_id=? OR reported_id=?", (user_id,user_id)),
        ("DELETE FROM premium WHERE user_id=?", (user_id,)),
    ]:
        cursor.execute(sql,args)

    # Delete uploaded media belonging to this profile/chat before removing message rows.
    cursor.execute("SELECT media_url FROM messages WHERE sender_id=? OR receiver_id=?", (user_id,user_id))
    for row in cursor.fetchall():
        delete_stored_media(row["media_url"], CHAT_UPLOADS_DIR)
    cursor.execute("SELECT photo FROM profile_photos WHERE user_id=?", (user_id,))
    for row in cursor.fetchall():
        delete_stored_media(row["photo"], PROFILE_UPLOADS_DIR)
    if user["photo"]:
        delete_stored_media(user["photo"], PROFILE_UPLOADS_DIR)

    cursor.execute("DELETE FROM messages WHERE sender_id=? OR receiver_id=?", (user_id,user_id))
    cursor.execute("DELETE FROM profile_photos WHERE user_id=?", (user_id,))
    # Keep the disabled user row as an audit/cloud record; it is invisible to the app.
    cursor.execute("UPDATE users SET photo='' WHERE id=?", (user_id,))
    conn.commit()

    if FIREBASE_ENABLED:
        try:
            fuser=firebase_auth.get_user_by_email(firebase_email_for_mobile(mobile))
            firebase_auth.update_user(fuser.uid, disabled=True)
        except Exception as e:
            print("Firebase disable warning:", e)
        firebase_sync_async()

    return {"status":True,"message":"User permanently disabled and removed from the active app."}


# =====================================================
# ADMIN ALL MATCHES
# =====================================================

@app.get("/admin/matches")
def admin_matches():

    cursor.execute("""

    SELECT

    interests.id,

    CASE
        WHEN u1.gender = 'Male' THEN u1.name
        ELSE u2.name
    END AS male,

    CASE
        WHEN u1.gender = 'Female' THEN u1.name
        ELSE u2.name
    END AS female,

    interests.created_at

    FROM interests

    JOIN users u1
    ON interests.sender_id = u1.id

    JOIN users u2
    ON interests.receiver_id = u2.id

    WHERE interests.status = 'Accepted'

    ORDER BY interests.id DESC

    """)

    data = cursor.fetchall()

    return {
        "status": True,
        "matches": [dict(i) for i in data]
    }

# =====================================================
# SMART MATCHES
# =====================================================

@app.get("/smart-matches/{user_id}")
def smart_matches(user_id:int):
    cursor.execute("SELECT * FROM users WHERE id=?",(user_id,))
    me=cursor.fetchone()
    if me is None:
        return {"status":False,"message":"User not found","matches":[]}

    cursor.execute("""
        SELECT id,name,age,height,religion,caste,education,occupation,city,state,country,photo,is_verified
        FROM users
        WHERE id!=?
        ORDER BY id DESC
        LIMIT 60
    """,(user_id,))
    rows=cursor.fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        score=40
        reasons=[]
        if me["religion"] and d["religion"] and me["religion"].strip().lower()==d["religion"].strip().lower(): score+=15;reasons.append("Same religion")
        if me["caste"] and d["caste"] and me["caste"].strip().lower()==d["caste"].strip().lower(): score+=10;reasons.append("Same caste")
        if me["city"] and d["city"] and me["city"].strip().lower()==d["city"].strip().lower(): score+=10;reasons.append("Same city")
        if me["education"] and d["education"] and me["education"].strip().lower()==d["education"].strip().lower(): score+=8;reasons.append("Similar education")
        if me["occupation"] and d["occupation"] and me["occupation"].strip().lower()==d["occupation"].strip().lower(): score+=5;reasons.append("Similar career")
        d["match_percent"]=min(score,99)
        d["match_reasons"]=reasons
        d["mutual_match"]=False
        out.append(d)
    out.sort(key=lambda x:(-x["match_percent"], -int(x["id"])))
    return {"status":True,"matches":out[:30]}


@app.post("/respond-interest")
def respond_interest(data:dict):
    user_id=int(data.get("user_id",0))
    other_id=int(data.get("other_id",0))
    status=str(data.get("status","")).strip()
    if status not in {"Accepted","Rejected"}:
        return {"status":False,"message":"Invalid status"}
    cursor.execute("""
        SELECT id FROM interests
        WHERE sender_id=? AND receiver_id=?
        ORDER BY id DESC LIMIT 1
    """,(other_id,user_id))
    row=cursor.fetchone()
    if row is None:
        return {"status":False,"message":"Interest not found"}
    cursor.execute("UPDATE interests SET status=? WHERE id=?",(status,row["id"]))
    conn.commit()
    if status=="Accepted":
        create_notification(other_id,"Interest Accepted ❤️","Your interest was accepted.")
    return {"status":True,"message":"Interest "+status+" successfully ❤️"}


# =====================================================
# MY MATCHES
# =====================================================

@app.get("/matches/{user_id}")

def my_matches(user_id:int):

    if not ensure_active_user(user_id): return {"status":False,"message":"This profile has been disabled by admin.","matches":[]}

    cursor.execute(

        """

        SELECT

        users.id,

        users.name,

        users.age,

        users.height,

        users.religion,

        users.caste,

        users.education,

        users.occupation,

        users.city,

        users.photo

        FROM interests

        INNER JOIN users

        ON users.id = interests.sender_id

        WHERE interests.receiver_id=?

        AND interests.status='Accepted'

        UNION

        SELECT

        users.id,

        users.name,

        users.age,

        users.height,

        users.religion,

        users.caste,

        users.education,

        users.occupation,

        users.city,

        users.photo

        FROM interests

        INNER JOIN users

        ON users.id = interests.receiver_id

        WHERE interests.sender_id=?

        AND interests.status='Accepted'

        LIMIT 100
        """,

        (

            user_id,

            user_id

        )

    )

    data=cursor.fetchall()

    return{

        "status":True,

        "total":len(data),

        "matches":[dict(row) for row in data]

    }



# =====================================================
# FIREBASE STATUS / MANUAL FULL SYNC
# =====================================================
@app.get("/firebase-status")
def firebase_status():
    return {
        "status": True,
        "firebase_enabled": FIREBASE_ENABLED,
        "firestore": bool(firebase_db),
        "storage": bool(firebase_bucket) if FIREBASE_ENABLED else False,
        "message": "Real Firebase connected" if FIREBASE_ENABLED else "Firebase service account is not configured"
    }

@app.post("/admin/firebase-sync")
def admin_firebase_sync():
    if not FIREBASE_ENABLED:
        return {
            "status": False,
            "message": "Firebase service account is not configured on Render"
        }
    firebase_sync_all()
    return {
        "status": True,
        "message": "All Prem Milan data synced to Firebase successfully ❤️"
    }
