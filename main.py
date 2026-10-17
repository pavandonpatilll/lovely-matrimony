from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
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
import time
import datetime
import secrets
import urllib.request
import urllib.error
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
    title="Prem Milan API",
    version="1.1"
)

# Admin API sessions are short-lived and issued only after /admin-login succeeds.
_ADMIN_SESSIONS = {}
_ADMIN_SESSION_LOCK = threading.Lock()
_ADMIN_SESSION_TTL = 12 * 60 * 60

@app.middleware("http")
async def protect_admin_api(request: Request, call_next):
    path = request.url.path
    if path.startswith("/admin/"):
        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        now = time.time()
        valid = False
        if token:
            with _ADMIN_SESSION_LOCK:
                expires = _ADMIN_SESSIONS.get(token, 0)
                if expires > now:
                    valid = True
                else:
                    _ADMIN_SESSIONS.pop(token, None)
        if not valid:
            return JSONResponse(status_code=401, content={"status": False, "message": "Admin login required. Please sign in again."})
    return await call_next(request)

# Google Play Billing production configuration.
# Set GOOGLE_PLAY_SERVICE_ACCOUNT_JSON in Render to the JSON key of a service account
# granted access to the app in Play Console. Never put this key in the frontend.
GOOGLE_PLAY_PACKAGE_NAME = os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "com.mycompany.matrimony").strip()
GOOGLE_PLAY_PRODUCT_ID = "prem_milan_premium"
GOOGLE_PLAY_BASE_PLAN_ID = "monthly-15-inr"
PREMIUM_AMOUNT_INR = 15
PREMIUM_PLAN_LABEL = "Prem Milan Premium Monthly"
PRIVATE_DATING_PRODUCT_ID = "private_dating_59"
PRIVATE_DATING_BASE_PLAN_ID = "monthly-59-inr"
PRIVATE_DATING_AMOUNT_INR = 59
PRIVATE_DATING_PLAN_LABEL = "Prem Milan Private Dating Monthly"


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
        "/verify-private-dating-subscription", "/delete-account"
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
    timeout=20,
    check_same_thread=False
)
# WAL improves read/write coexistence; busy_timeout reduces transient locked-db errors.
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
conn.execute("PRAGMA busy_timeout=20000")
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
    cursor.execute("ALTER TABLE premium ADD COLUMN subscription_id TEXT")
except Exception:
    pass
# Google Play subscription lifecycle fields (safe additive migrations).
for _column_sql in [
    "ALTER TABLE premium ADD COLUMN play_state TEXT",
    "ALTER TABLE premium ADD COLUMN auto_renewing INTEGER",
    "ALTER TABLE premium ADD COLUMN cancel_time TEXT",
    "ALTER TABLE premium ADD COLUMN latest_order_id TEXT",
    "ALTER TABLE premium ADD COLUMN last_checked_at TEXT",
    "ALTER TABLE premium ADD COLUMN acknowledgement_state TEXT",
    "ALTER TABLE premium ADD COLUMN start_time TEXT",
]:
    try:
        cursor.execute(_column_sql)
    except Exception:
        pass

try:
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_premium_subscription_id ON premium(subscription_id)")
except Exception:
    pass
try:
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_premium_user_status_expiry ON premium(user_id,status,end_date)")
except Exception:
    pass

# Private Dating entitlements are kept separate from the existing ₹15 Premium plan.
cursor.execute("""CREATE TABLE IF NOT EXISTS private_dating_subscriptions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id TEXT NOT NULL,
    base_plan_id TEXT NOT NULL, amount REAL NOT NULL, purchase_token TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL, start_date TEXT, end_date TEXT, play_state TEXT, auto_renewing INTEGER,
    cancel_time TEXT, latest_order_id TEXT, last_checked_at TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()

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


def user_has_premium(user_id):
    """Return Premium only for this exact user and a currently valid active subscription.

    Use a request-local SQLite connection: the old global cursor is shared by
    concurrent FastAPI requests and could interleave SELECT/fetch operations,
    causing one user's Premium lookup to read another request's rows.
    """
    db = None
    try:
        uid = int(user_id)
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT status, end_date FROM premium WHERE user_id=? ORDER BY id DESC",
            (uid,),
        ).fetchall()
        now = datetime.datetime.now(datetime.timezone.utc)
        for row in rows:
            if str(row["status"] or "").strip().lower() != "active":
                continue
            end_date = row["end_date"]
            # Never grant access indefinitely if the subscription has no expiry.
            if not end_date:
                continue
            try:
                end_dt = datetime.datetime.fromisoformat(
                    str(end_date).strip().replace("Z", "+00:00")
                )
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
                if end_dt.astimezone(datetime.timezone.utc) > now:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False
    except Exception as premium_error:
        print("Premium access check warning:", premium_error)
        return False
    finally:
        if db is not None:
            db.close()


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

    # Mature profiles (women 30+, men 35+) are contactable only through
    # the separate Private Dating plan, never through ordinary Premium.
    db = None
    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.row_factory = sqlite3.Row
        a = db.execute("SELECT id,gender,age FROM users WHERE id=? AND COALESCE(is_deleted,0)=0", (int(user_a),)).fetchone()
        b = db.execute("SELECT id,gender,age FROM users WHERE id=? AND COALESCE(is_deleted,0)=0", (int(user_b),)).fetchone()
        if not a or not b:
            return False, "This profile is unavailable."
        a_mature = is_private_dating_profile(a)
        b_mature = is_private_dating_profile(b)
        if a_mature or b_mature:
            # Subscriber must be the non-mature member and match the intended audience:
            # male member -> mature women; female member -> mature men.
            subscriber_id = int(user_b) if a_mature and not b_mature else int(user_a) if b_mature and not a_mature else 0
            subscriber = b if subscriber_id == int(user_b) else a if subscriber_id == int(user_a) else None
            if not subscriber or is_private_dating_profile(subscriber) or not private_dating_active(subscriber_id):
                return False, "Private Dating ₹59/month is required to contact these profiles."
            sg = str(subscriber["gender"] or "").strip().lower()
            target = a if a_mature else b
            tg = str(target["gender"] or "").strip().lower()
            allowed = (sg in ("male", "m") and tg in ("female", "f") and int(target["age"] or 0) >= 30) or (sg in ("female", "f") and tg in ("male", "m") and int(target["age"] or 0) >= 35)
            if not allowed:
                return False, "This Private Dating profile is not available for your account."
    finally:
        if db is not None:
            db.close()
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
      AND NOT (
        (lower(trim(coalesce(gender,''))) IN ('female','f') AND CAST(coalesce(age,0) AS INTEGER) >= 30)
        OR (lower(trim(coalesce(gender,''))) IN ('male','m') AND CAST(coalesce(age,0) AS INTEGER) >= 35)
      )

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
    premium_access = user_has_premium(data.user_id)
    total_available = len(users)
    if not premium_access:
        users = users[:3]

    return {
        "status": True,
        "premium": premium_access,
        "total": total_available,
        "limited": not premium_access and total_available > len(users),
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

    cursor.execute("SELECT name FROM users WHERE id=?", (data.sender_id,))
    sender_row = cursor.fetchone()
    sender_name = (sender_row["name"] if sender_row else "A member")
    create_notification(
        data.receiver_id,
        "New Interest ❤️",
        f"{sender_name} sent you an interest. Open Received Interests to Accept or Reject."
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
    # Existing ₹15 Premium still unlocks regular chat. Private Dating independently
    # unlocks chat with eligible mature profiles, without forcing a second subscription.
    if not user_has_premium(data.sender_id):
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
        try:
            receiver = db.execute("SELECT gender,age FROM users WHERE id=?", (int(data.receiver_id),)).fetchone()
            sender = db.execute("SELECT gender,age FROM users WHERE id=?", (int(data.sender_id),)).fetchone()
            private_chat = bool(receiver and sender and (is_private_dating_profile(receiver) or is_private_dating_profile(sender)) and private_dating_active(data.sender_id))
        finally:
            db.close()
        if not private_chat:
            return {"status":False,"premium_required":True,"message":"Regular chat ke liye Premium ₹15/month chahiye. Mature profiles ke liye Private Dating ₹59/month chahiye."}

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

    cursor.execute("SELECT name FROM users WHERE id=?", (data.sender_id,))
    sender_row = cursor.fetchone()
    sender_name = (sender_row["name"] if sender_row else "A member")
    preview = (data.message or "").strip().replace("\n", " ")[:90]
    create_notification(
        data.receiver_id,
        f"New Message from {sender_name} 💬",
        f"{sender_name}: {preview or 'sent you a message.'}"
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

    if not user_has_premium(sender_id):
        return {"status":False,"premium_required":True,"message":"Chat photo sharing Premium members ke liye hai."}
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
    if not user_has_premium(sender_id):
        db = sqlite3.connect(DB_PATH, timeout=10); db.row_factory = sqlite3.Row
        try:
            receiver = db.execute("SELECT gender,age FROM users WHERE id=?", (int(receiver_id),)).fetchone()
            sender = db.execute("SELECT gender,age FROM users WHERE id=?", (int(sender_id),)).fetchone()
            private_chat = bool(receiver and sender and (is_private_dating_profile(receiver) or is_private_dating_profile(sender)) and private_dating_active(sender_id))
        finally:
            db.close()
        if not private_chat:
            return {"status":False,"premium_required":True,"message":"Regular chat ke liye Premium ₹15/month chahiye. Mature profiles ke liye Private Dating ₹59/month chahiye.","messages":[]}

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
    if not user_has_premium(user_id): return {"status":True,"premium_required":True,"message":"Chat access ke liye Premium activate karein ❤️","conversations":[]}
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
# GOOGLE PLAY BILLING SUBSCRIPTIONS
# =====================================================

class GooglePlaySubscriptionVerifyModel(BaseModel):
    user_id: int
    product_id: str
    purchase_token: str


def google_play_subscription_details(purchase_token: str):
    """Fetch authoritative subscription state from the Google Play Developer API."""
    raw_service_account = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_service_account:
        raise RuntimeError(
            "Google Play verification is not configured yet. Add GOOGLE_PLAY_SERVICE_ACCOUNT_JSON in Render."
        )
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except Exception as exc:
        raise RuntimeError(
            "Google authentication libraries are unavailable. Ensure firebase-admin/google-auth is installed."
        ) from exc

    try:
        service_info = json.loads(raw_service_account)
        credentials_obj = service_account.Credentials.from_service_account_info(
            service_info,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        credentials_obj.refresh(GoogleAuthRequest())
        token = credentials_obj.token
        if not token:
            raise RuntimeError("Google Play API authentication failed.")
    except RuntimeError:
        raise
    except Exception as exc:
        print("Google Play service account authentication error:", str(exc))
        raise RuntimeError("Google Play API authentication failed. Check the service-account configuration.") from exc

    encoded_token = __import__("urllib.parse", fromlist=["quote"]).quote(purchase_token, safe="")
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + __import__("urllib.parse", fromlist=["quote"]).quote(GOOGLE_PLAY_PACKAGE_NAME, safe="")
        + "/purchases/subscriptionsv2/tokens/" + encoded_token
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8"))
            api_message = details.get("error", {}).get("message", "Google Play rejected the purchase token.")
        except Exception:
            api_message = "Google Play rejected the purchase token."
        print("Google Play subscription lookup failed:", exc.code, api_message)
        if exc.code in (400, 401, 403, 404):
            raise ValueError("Google Play could not verify this purchase. Check the Play Console API access and purchase token.")
        raise RuntimeError("Google Play verification is temporarily unavailable.")
    except Exception as exc:
        print("Google Play API request error:", str(exc))
        raise RuntimeError("Could not reach Google Play to verify the subscription.")


def google_play_lifecycle_fields(details, matching_items=None):
    """Normalize subscription renewal/cancellation metadata returned by Google Play."""
    items = matching_items if matching_items is not None else (details.get("lineItems") or [])
    item = items[0] if items else {}
    state = str(details.get("subscriptionState", "") or "")
    auto_renew = (item.get("autoRenewingPlan") or {}).get("autoRenewEnabled")
    cancel_context = details.get("canceledStateContext") or {}
    user_cancel = cancel_context.get("userInitiatedCancellation") or {}
    return {
        "play_state": state,
        "auto_renewing": None if auto_renew is None else int(bool(auto_renew)),
        "cancel_time": user_cancel.get("cancelTime"),
        "latest_order_id": item.get("latestSuccessfulOrderId") or details.get("latestOrderId"),
        "acknowledgement_state": details.get("acknowledgementState"),
        "start_time": details.get("startTime"),
    }


@app.post("/verify-google-play-subscription")
def verify_google_play_subscription(data: GooglePlaySubscriptionVerifyModel):
    """Activate Premium only after the Google Play Developer API verifies the purchase."""
    if not ensure_active_user(data.user_id):
        raise HTTPException(status_code=404, detail="Account not found or disabled.")
    product_id = str(data.product_id or "").strip()
    purchase_token = str(data.purchase_token or "").strip()
    if product_id != GOOGLE_PLAY_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="This Google Play product is not the Prem Milan Premium subscription.")
    if not purchase_token or len(purchase_token) > 4096:
        raise HTTPException(status_code=400, detail="A valid Google Play purchase token is required.")

    try:
        details = google_play_subscription_details(purchase_token)
        state = str(details.get("subscriptionState", ""))
        line_items = details.get("lineItems") or []
        matching_items = [
            item for item in line_items
            if str(item.get("productId", "")) == GOOGLE_PLAY_PRODUCT_ID
            and str((item.get("offerDetails") or {}).get("basePlanId", "")) == GOOGLE_PLAY_BASE_PLAN_ID
        ]
        if not matching_items:
            return {"status": False, "premium": False, "message": "Google Play purchase does not match the configured Prem Milan ₹15 monthly plan."}

        expiry_values = [str(item.get("expiryTime", "")) for item in matching_items if item.get("expiryTime")]
        if not expiry_values:
            return {"status": False, "premium": False, "message": "Google Play did not return a valid subscription expiry. Premium was not activated."}
        expiry_text = max(expiry_values)
        try:
            expiry_dt = datetime.datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
            expiry_dt = expiry_dt.astimezone(datetime.timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return {"status": False, "premium": False, "message": "Google Play returned an invalid expiry date."}

        now = datetime.datetime.now(datetime.timezone.utc)
        # A canceled subscription remains entitled until its paid-through expiry.
        allowed_states = {
            "SUBSCRIPTION_STATE_ACTIVE",
            "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
            "SUBSCRIPTION_STATE_CANCELED",
        }
        if state not in allowed_states or expiry_dt <= now:
            return {"status": False, "premium": False, "message": "This Google Play subscription is not currently entitled to Premium."}

        with sqlite3.connect(DB_PATH, timeout=10) as verify_db:
            verify_db.row_factory = sqlite3.Row
            existing = verify_db.execute(
                "SELECT id,user_id FROM premium WHERE subscription_id=? ORDER BY id DESC LIMIT 1",
                (purchase_token,),
            ).fetchone()
            if existing and int(existing["user_id"]) != int(data.user_id):
                return {"status": False, "premium": False, "message": "This Google Play purchase is already linked to another Prem Milan account."}

            lifecycle = google_play_lifecycle_fields(details, matching_items)
            lifecycle_values = (
                lifecycle["play_state"], lifecycle["auto_renewing"], lifecycle["cancel_time"],
                lifecycle["latest_order_id"], now.isoformat(), lifecycle["acknowledgement_state"],
                lifecycle["start_time"],
            )
            if existing:
                verify_db.execute(
                    """UPDATE premium SET plan=?,amount=?,payment_id=?,payment_status=?,start_date=COALESCE(start_date,?),
                       end_date=?,status=?,play_state=?,auto_renewing=?,cancel_time=?,latest_order_id=?,
                       last_checked_at=?,acknowledgement_state=?,start_time=? WHERE id=?""",
                    (
                        PREMIUM_PLAN_LABEL, PREMIUM_AMOUNT_INR, "google-play:" + product_id,
                        state, details.get("startTime") or now.isoformat(), expiry_dt.isoformat(),
                        "active", *lifecycle_values, int(existing["id"]),
                    ),
                )
            else:
                verify_db.execute(
                    """INSERT INTO premium
                       (user_id,plan,amount,payment_id,payment_status,start_date,end_date,status,subscription_id,
                        play_state,auto_renewing,cancel_time,latest_order_id,last_checked_at,acknowledgement_state,start_time)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(data.user_id), PREMIUM_PLAN_LABEL, PREMIUM_AMOUNT_INR,
                        "google-play:" + product_id, state, details.get("startTime") or now.isoformat(),
                        expiry_dt.isoformat(), "active", purchase_token, *lifecycle_values,
                    ),
                )
            verify_db.execute("UPDATE users SET is_premium=1 WHERE id=?", (int(data.user_id),))
            verify_db.commit()

        firebase_sync_async()
        return {
            "status": True,
            "premium": True,
            "product_id": GOOGLE_PLAY_PRODUCT_ID,
            "expiry_time": expiry_dt.isoformat(),
            "message": "Google Play subscription verified. Prem Milan Premium activated! ❤️",
        }
    except ValueError as exc:
        return {"status": False, "premium": False, "message": str(exc)}
    except RuntimeError as exc:
        # Fail closed: missing API access must never grant membership.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        print("Google Play subscription verification error:", str(exc))
        raise HTTPException(status_code=500, detail="Could not verify the Google Play subscription. Premium was not activated.")


class PrivateDatingVerifyModel(BaseModel):
    user_id: int
    product_id: str
    purchase_token: str


def is_private_dating_profile(row):
    gender = str(row["gender"] or "").strip().lower()
    age = int(row["age"] or 0)
    return (gender in ("female", "f") and age >= 30) or (gender in ("male", "m") and age >= 35)


def private_dating_active(user_id: int) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        row = db.execute("SELECT end_date,play_state,status FROM private_dating_subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1", (int(user_id),)).fetchone()
    if not row or not row[0]:
        return False
    try:
        expiry = datetime.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        return str(row[2]).lower() == "active" and str(row[1]) in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"} and expiry.astimezone(datetime.timezone.utc) > now
    except Exception:
        return False


@app.get("/private-dating/status/{user_id}")
def private_dating_status(user_id: int):
    if not ensure_active_user(user_id):
        raise HTTPException(status_code=404, detail="Account not found or disabled.")
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.row_factory = sqlite3.Row
        me = db.execute("SELECT gender,age FROM users WHERE id=?", (int(user_id),)).fetchone()
    if not me: raise HTTPException(status_code=404, detail="Account not found.")
    gender = str(me["gender"] or "").strip().lower()
    audience = "old_aunty" if gender in ("male", "m") else "old_uncle" if gender in ("female", "f") else "unknown"
    return {"status": True, "active": private_dating_active(user_id), "eligible_category": is_private_dating_profile(me), "audience": audience, "product_id": PRIVATE_DATING_PRODUCT_ID, "base_plan_id": PRIVATE_DATING_BASE_PLAN_ID, "amount": PRIVATE_DATING_AMOUNT_INR}


@app.get("/private-dating/profiles/{user_id}")
def private_dating_profiles(user_id: int):
    if not ensure_active_user(user_id):
        raise HTTPException(status_code=404, detail="Account not found or disabled.")
    if not private_dating_active(user_id):
        return {"status": True, "active": False, "profiles": [], "message": "Subscribe to Private Dating for ₹59/month to unlock these profiles."}
    with sqlite3.connect(DB_PATH, timeout=10) as db:
        db.row_factory = sqlite3.Row
        me = db.execute("SELECT gender FROM users WHERE id=?", (int(user_id),)).fetchone()
        gender = str(me["gender"] or "").strip().lower() if me else ""
        wanted = "female" if gender in ("male", "m") else "male" if gender in ("female", "f") else ""
        if not wanted: return {"status": True, "active": True, "profiles": []}
        rows = db.execute("SELECT id,name,gender,age,height,religion,caste,education,occupation,city,photo FROM users WHERE id!=? AND COALESCE(is_deleted,0)=0 AND lower(trim(coalesce(gender,'')))=? AND (((lower(trim(coalesce(gender,''))) IN ('female','f') AND CAST(coalesce(age,0) AS INTEGER)>=30) OR (lower(trim(coalesce(gender,''))) IN ('male','m') AND CAST(coalesce(age,0) AS INTEGER)>=35))) ORDER BY id DESC LIMIT 100", (int(user_id), wanted)).fetchall()
    return {"status": True, "active": True, "profiles": [dict(r) for r in rows]}


@app.post("/verify-private-dating-subscription")
def verify_private_dating_subscription(data: PrivateDatingVerifyModel):
    if not ensure_active_user(data.user_id): raise HTTPException(status_code=404, detail="Account not found or disabled.")
    product_id = str(data.product_id or "").strip()
    purchase_token = str(data.purchase_token or "").strip()
    if product_id != PRIVATE_DATING_PRODUCT_ID: raise HTTPException(status_code=400, detail="This is not the Private Dating subscription product.")
    if not purchase_token or len(purchase_token)>4096: raise HTTPException(status_code=400, detail="A valid Google Play purchase token is required.")
    try:
        details = google_play_subscription_details(purchase_token)
        state = str(details.get("subscriptionState", ""))
        items = [i for i in (details.get("lineItems") or []) if str(i.get("productId", "")) == PRIVATE_DATING_PRODUCT_ID and str((i.get("offerDetails") or {}).get("basePlanId", "")) == PRIVATE_DATING_BASE_PLAN_ID]
        if not items: return {"status": False, "active": False, "message": "Google Play purchase does not match Private Dating ₹59/month."}
        expiry_values = [str(i.get("expiryTime", "")) for i in items if i.get("expiryTime")]
        if not expiry_values: return {"status": False, "active": False, "message": "Google Play did not return subscription expiry."}
        expiry = datetime.datetime.fromisoformat(max(expiry_values).replace("Z", "+00:00"))
        if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        expiry = expiry.astimezone(datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        if state not in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"} or expiry <= now:
            return {"status": False, "active": False, "message": "Private Dating subscription is not currently active."}
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            existing = db.execute("SELECT user_id FROM private_dating_subscriptions WHERE purchase_token=?", (purchase_token,)).fetchone()
            if existing and int(existing[0]) != int(data.user_id): return {"status": False, "active": False, "message": "This purchase is linked to another account."}
            life = google_play_lifecycle_fields(details, items)
            db.execute("""INSERT INTO private_dating_subscriptions(user_id,product_id,base_plan_id,amount,purchase_token,status,start_date,end_date,play_state,auto_renewing,cancel_time,latest_order_id,last_checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(purchase_token) DO UPDATE SET status=excluded.status,end_date=excluded.end_date,play_state=excluded.play_state,auto_renewing=excluded.auto_renewing,cancel_time=excluded.cancel_time,latest_order_id=excluded.latest_order_id,last_checked_at=excluded.last_checked_at""", (int(data.user_id), product_id, PRIVATE_DATING_BASE_PLAN_ID, PRIVATE_DATING_AMOUNT_INR, purchase_token, "active", details.get("startTime") or now.isoformat(), expiry.isoformat(), state, life["auto_renewing"], life["cancel_time"], life["latest_order_id"], now.isoformat()))
            db.commit()
        firebase_sync_async()
        return {"status": True, "active": True, "product_id": product_id, "expiry_time": expiry.isoformat(), "message": "Private Dating ₹59/month verified and activated."}
    except ValueError as exc: return {"status": False, "active": False, "message": str(exc)}
    except RuntimeError as exc: raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        print("Private Dating verification error:", str(exc))
        raise HTTPException(status_code=500, detail="Could not verify Private Dating subscription.")


@app.get("/premium-status/{user_id}")
def premium_status(user_id: int):
    if not ensure_active_user(user_id):
        return {"status": False, "message": "This profile has been disabled by admin."}
    with sqlite3.connect(DB_PATH, timeout=10) as status_db:
        status_db.row_factory = sqlite3.Row
        user = status_db.execute("SELECT id FROM users WHERE id=?", (int(user_id),)).fetchone()
        if user is None:
            return {"status": False, "message": "User Not Found"}
        sub = status_db.execute(
            "SELECT plan,status,end_date FROM premium WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        premium_access = user_has_premium(user_id)
        if not premium_access:
            status_db.execute("UPDATE users SET is_premium=0 WHERE id=?", (int(user_id),))
    return {
        "status": True,
        "premium": premium_access,
        "subscription": dict(sub) if sub else None,
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

    token = secrets.token_urlsafe(32)
    with _ADMIN_SESSION_LOCK:
        _ADMIN_SESSIONS[token] = time.time() + _ADMIN_SESSION_TTL

    firebase_sync_async()

    return{

        "status":True,
        "token": token,
        "expires_in": _ADMIN_SESSION_TTL,

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

    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM premium WHERE lower(status)='active' AND end_date IS NOT NULL AND julianday(end_date) > julianday('now')")
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
        CASE WHEN EXISTS (
            SELECT 1 FROM premium p
            WHERE p.user_id=users.id AND lower(p.status)='active'
              AND p.end_date IS NOT NULL AND julianday(p.end_date) > julianday('now')
        ) THEN 1 ELSE 0 END AS is_premium,
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

@app.get("/admin/subscriptions")
def admin_subscriptions():
    """Admin-only subscription ledger; refresh Google Play state before returning rows."""
    with sqlite3.connect(DB_PATH, timeout=20) as sub_db:
        sub_db.row_factory = sqlite3.Row
        rows = sub_db.execute("""
            SELECT p.*, u.name, u.mobile, u.city
            FROM premium p LEFT JOIN users u ON u.id=p.user_id
            ORDER BY p.id DESC
        """).fetchall()

    # Refresh Google Play-backed rows so cancel/renewal/expiry changes made in Play
    # Console are reflected on the next dashboard refresh. Never expose purchase tokens.
    for row in rows:
        token = row["subscription_id"]
        if not token:
            continue
        try:
            details = google_play_subscription_details(str(token))
            items = [
                item for item in (details.get("lineItems") or [])
                if str(item.get("productId", "")) == GOOGLE_PLAY_PRODUCT_ID
                and str((item.get("offerDetails") or {}).get("basePlanId", "")) == GOOGLE_PLAY_BASE_PLAN_ID
            ]
            if not items:
                continue
            expiry_text = max([str(x.get("expiryTime", "")) for x in items if x.get("expiryTime")] or [""])
            expiry_dt = datetime.datetime.fromisoformat(expiry_text.replace("Z", "+00:00")) if expiry_text else None
            if expiry_dt and expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
            if expiry_dt:
                expiry_dt = expiry_dt.astimezone(datetime.timezone.utc)
            state = str(details.get("subscriptionState", "") or "")
            life = google_play_lifecycle_fields(details, items)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            active = state in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"} and expiry_dt and expiry_dt > now_utc
            status_value = "active" if active else ("expired" if expiry_dt and expiry_dt <= now_utc else state.replace("SUBSCRIPTION_STATE_", "").lower() or "unknown")
            with sqlite3.connect(DB_PATH, timeout=10) as update_db:
                update_db.execute("""
                    UPDATE premium SET payment_status=?,play_state=?,auto_renewing=?,cancel_time=?,
                    latest_order_id=?,last_checked_at=?,acknowledgement_state=?,start_time=?,
                    end_date=COALESCE(?,end_date),status=? WHERE id=?
                """, (
                    state, life["play_state"], life["auto_renewing"], life["cancel_time"],
                    life["latest_order_id"], now_utc.isoformat(), life["acknowledgement_state"],
                    life["start_time"], expiry_dt.isoformat() if expiry_dt else None,
                    status_value, int(row["id"])
                ))
                update_db.execute("UPDATE users SET is_premium=? WHERE id=?",
                                  (1 if active else 0, int(row["user_id"])))
                update_db.commit()
        except Exception as exc:
            print("Admin subscription refresh warning:", str(exc))
            # Keep the last known record visible if Play API is temporarily unavailable.

    with sqlite3.connect(DB_PATH, timeout=10) as sub_db:
        sub_db.row_factory = sqlite3.Row
        records = sub_db.execute("""
            SELECT p.id,p.user_id,u.name,u.mobile,u.city,p.plan,p.amount,p.payment_id,
                   p.payment_status,p.start_date,p.end_date,p.status,p.created_at,
                   p.play_state,p.auto_renewing,p.cancel_time,p.latest_order_id,
                   p.last_checked_at,p.acknowledgement_state,p.start_time,
                   CASE WHEN p.subscription_id IS NULL OR p.subscription_id='' THEN 0 ELSE 1 END AS google_play_linked
            FROM premium p LEFT JOIN users u ON u.id=p.user_id
            ORDER BY p.id DESC
        """).fetchall()
    return {"status": True, "subscriptions": [dict(r) for r in records], "total": len(records)}


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
        WHERE EXISTS (
            SELECT 1 FROM premium p
            WHERE p.user_id=users.id AND lower(p.status)='active'
              AND p.end_date IS NOT NULL AND julianday(p.end_date) > julianday('now')
        )
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

    if not ensure_active_user(user_id):
        return {"status": False, "message": "User not found or disabled."}
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(days=30)
    cursor.execute(
        """INSERT INTO premium(user_id,plan,amount,payment_id,payment_status,start_date,end_date,status)
           VALUES(?,?,?,?,?,?,?,?)""",
        (user_id, "Admin complimentary Premium", 0, "ADMIN-GRANT", "Comped",
         now.isoformat(timespec="seconds").replace("+00:00", "Z"),
         end.isoformat(timespec="seconds").replace("+00:00", "Z"), "Active")
    )
    cursor.execute("UPDATE users SET is_premium=1 WHERE id=?", (user_id,))
    conn.commit()
    firebase_sync_async()
    return {"status": True, "message": "Premium granted to this user for 30 days 👑"}

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

    my_gender=str(me["gender"] or "").strip().lower()
    wanted_gender = "female" if my_gender == "male" else "male" if my_gender == "female" else ""
    if not wanted_gender:
        return {"status":True,"matches":[],"message":"Please complete your gender in profile to get suitable matches."}
    cursor.execute("""
        SELECT id,name,gender,age,height,religion,caste,education,occupation,city,state,country,photo,is_verified
        FROM users
        WHERE id!=? AND lower(trim(coalesce(gender,'')))=?
          AND NOT ((lower(trim(coalesce(gender,''))) IN ('female','f') AND CAST(coalesce(age,0) AS INTEGER)>=30)
                   OR (lower(trim(coalesce(gender,''))) IN ('male','m') AND CAST(coalesce(age,0) AS INTEGER)>=35)
                   OR COALESCE(is_deleted,0)!=0)
        ORDER BY id DESC
        LIMIT 60
    """,(user_id,wanted_gender))
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
    try:
        user_id=int(data.get("user_id",0))
        other_id=int(data.get("other_id",0))
    except (TypeError, ValueError):
        return {"status":False,"message":"Invalid user"}
    status=str(data.get("status","")).strip().title()
    if status not in {"Accepted","Rejected"}:
        return {"status":False,"message":"Choose Accept or Reject."}
    if user_id == other_id or not ensure_active_user(user_id) or not ensure_active_user(other_id):
        return {"status":False,"message":"This profile is unavailable."}
    cursor.execute("""
        SELECT id FROM interests
        WHERE sender_id=? AND receiver_id=?
        ORDER BY id DESC LIMIT 1
    """,(other_id,user_id))
    row=cursor.fetchone()
    if row is None:
        return {"status":False,"message":"Interest not found."}
    cursor.execute("UPDATE interests SET status=? WHERE id=?",(status,row["id"]))
    conn.commit()
    cursor.execute("SELECT name FROM users WHERE id=?", (user_id,))
    receiver_row=cursor.fetchone()
    receiver_name=receiver_row["name"] if receiver_row else "A member"
    if status == "Accepted":
        create_notification(other_id,"Interest Accepted ❤️",f"{receiver_name} accepted your interest.")
    else:
        create_notification(other_id,"Interest Update",f"{receiver_name} declined your interest.")
    return {"status":True,"message":"Interest "+status+" successfully ❤️"}


# =====================================================
# MY MATCHES
# =====================================================

@app.get("/matches/{user_id}")
def my_matches(user_id:int):
    if not ensure_active_user(user_id):
        return {"status":False,"message":"This profile has been disabled by admin.","matches":[]}

    cursor.execute("SELECT gender FROM users WHERE id=?", (user_id,))
    me = cursor.fetchone()
    my_gender = str(me["gender"] or "").strip().lower() if me else ""
    wanted_gender = "female" if my_gender == "male" else "male" if my_gender == "female" else ""
    if not wanted_gender:
        return {"status":True,"premium":user_has_premium(user_id),"total":0,"limited":False,"matches":[],"message":"Please complete your gender in profile to get suitable matches."}

    cursor.execute("""
        SELECT DISTINCT users.id, users.name, users.gender, users.age, users.height,
               users.religion, users.caste, users.education, users.occupation,
               users.city, users.photo
        FROM interests
        INNER JOIN users ON users.id = interests.sender_id
        WHERE interests.receiver_id=? AND interests.status='Accepted'
          AND lower(trim(coalesce(users.gender,'')))=?
          AND NOT ((lower(trim(coalesce(users.gender,''))) IN ('female','f') AND CAST(coalesce(users.age,0) AS INTEGER)>=30) OR (lower(trim(coalesce(users.gender,''))) IN ('male','m') AND CAST(coalesce(users.age,0) AS INTEGER)>=35))
        UNION
        SELECT DISTINCT users.id, users.name, users.gender, users.age, users.height,
               users.religion, users.caste, users.education, users.occupation,
               users.city, users.photo
        FROM interests
        INNER JOIN users ON users.id = interests.receiver_id
        WHERE interests.sender_id=? AND interests.status='Accepted'
          AND lower(trim(coalesce(users.gender,'')))=?
          AND NOT ((lower(trim(coalesce(users.gender,''))) IN ('female','f') AND CAST(coalesce(users.age,0) AS INTEGER)>=30) OR (lower(trim(coalesce(users.gender,''))) IN ('male','m') AND CAST(coalesce(users.age,0) AS INTEGER)>=35))
        ORDER BY id DESC
        LIMIT 100
    """, (user_id, wanted_gender, user_id, wanted_gender))

    data = cursor.fetchall()
    premium_access = user_has_premium(user_id)
    total_available = len(data)
    if not premium_access:
        data = data[:3]
    return {
        "status": True,
        "premium": premium_access,
        "total": total_available,
        "limited": not premium_access and total_available > len(data),
        "matches": [dict(row) for row in data]
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
