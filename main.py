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
# FOLDERS
# =====================================================

os.makedirs("uploads/profile", exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory="uploads"),
    name="uploads"
)

# =====================================================
# DATABASE
# =====================================================

conn = sqlite3.connect(
    "database.db",
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

# =====================================================
# HOME
# =====================================================

@app.get("/")
def home():

    return {
        "status": True,
        "message": "JeevanSaathi Backend Running ❤️"
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

    return {

        "status": True,
        "message": "Registration Successful"

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

    if data["password"] != hash_password(user.password):

        return {
            "status": False,
            "message": "Wrong Password"
        }

    return {

        "status": True,
        "message": "Login Successful",

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

        "profile":dict(user)

    }

# =====================================================
# UPLOAD PROFILE PHOTO
# =====================================================

@app.post("/upload-profile-photo/{user_id}")

def upload_profile_photo(
    user_id: int,
    photo: UploadFile = File(...)
):

    cursor.execute(

        """

        SELECT photo

        FROM users

        WHERE id=?

        """,

        (

            user_id,

        )

    )

    user = cursor.fetchone()

    if user is None:

        return{

            "status":False,

            "message":"User Not Found"

        }

    # Delete old profile photo

    if user["photo"]:

        old_path = user["photo"].replace("/uploads/", "uploads/")

        if os.path.exists(old_path):

            os.remove(old_path)

    # Generate unique filename

    extension = photo.filename.split(".")[-1]

    filename = str(uuid.uuid4()) + "." + extension

    filepath = os.path.join("uploads", "profile", filename)

    # Save new photo

    with open(filepath, "wb") as buffer:

        shutil.copyfileobj(photo.file, buffer)

    photo_url = "/uploads/profile/" + filename

    # Update database

    cursor.execute(

        """

        UPDATE users

        SET photo=?

        WHERE id=?

        """,

        (

            photo_url,

            user_id

        )

    )

    conn.commit()

    return{

        "status":True,

        "message":"Profile Photo Uploaded Successfully",

        "photo":photo_url

    }

# =====================================================
# UPDATE PROFILE
# =====================================================

@app.post("/update-profile")

def update_profile(user:UpdateProfileModel):

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

    WHERE id != ?

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

    query += " ORDER BY id DESC"

    cursor.execute(query, tuple(params))

    users = cursor.fetchall()

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
# CHAT HISTORY
# =====================================================

@app.get("/chat/{sender_id}/{receiver_id}")

def get_chat(sender_id:int,receiver_id:int):

    cursor.execute(

        """

        SELECT *

        FROM messages

        WHERE

        (sender_id=? AND receiver_id=?)

        OR

        (sender_id=? AND receiver_id=?)

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

        "messages":[dict(row) for row in chats]

    }


# =====================================================
# CONVERSATION LIST
# =====================================================

@app.get("/conversations/{user_id}")

def conversations(user_id:int):

    cursor.execute(

        """

        SELECT DISTINCT

        CASE

        WHEN sender_id=?

        THEN receiver_id

        ELSE sender_id

        END AS partner_id

        FROM messages

        WHERE sender_id=?

        OR receiver_id=?

        """,

        (

            user_id,

            user_id,

            user_id

        )

    )

    ids=cursor.fetchall()

    users=[]

    for i in ids:

        cursor.execute(

            """

            SELECT

            id,

            name,

            photo,

            city

            FROM users

            WHERE id=?

            """,

            (

                i["partner_id"],

            )

        )

        u=cursor.fetchone()

        if u:

            users.append(dict(u))

    return{

        "status":True,

        "total":len(users),

        "conversations":users

    }


# =====================================================
# UNREAD MESSAGE COUNT
# =====================================================

@app.get("/unread-count/{user_id}")

def unread_count(user_id:int):

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

        blocked_user

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

        "message":"User Blocked"

    }

# =====================================================
# REPORT USER
# =====================================================

@app.post("/report-user")

def report_user(data: InterestModel):

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

            "Reported"

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
# NOTIFICATIONS
# =====================================================

@app.get("/notifications/{user_id}")

def notifications(user_id:int):

    cursor.execute(

        """

        SELECT *

        FROM notifications

        WHERE user_id=?

        ORDER BY id DESC

        """,

        (

            user_id,

        )

    )

    data=cursor.fetchall()

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

    return{

        "status":True,

        "message":"Admin Login Successful"

    }


# =====================================================
# ADMIN DASHBOARD
# =====================================================

@app.get("/admin/dashboard")

def admin_dashboard():

    cursor.execute("SELECT COUNT(*) FROM users")
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
        is_verified,
        is_premium,
        created_at

        FROM users

        ORDER BY id DESC

        """

    )

    users=cursor.fetchall()

    return{

        "status":True,

        "total":len(users),

        "users":[dict(i) for i in users]

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
# DELETE USER
# =====================================================

@app.delete("/admin/delete-user/{user_id}")

def delete_user(user_id:int):

    cursor.execute(
        "DELETE FROM interests WHERE sender_id=? OR receiver_id=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM messages WHERE sender_id=? OR receiver_id=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM favorites WHERE user_id=? OR favorite_user=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM notifications WHERE user_id=?",
        (
            user_id,
        )
    )

    cursor.execute(
        "DELETE FROM profile_views WHERE viewer_id=? OR profile_id=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM blocks WHERE user_id=? OR blocked_user=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM reports WHERE reporter_id=? OR reported_id=?",
        (
            user_id,
            user_id
        )
    )

    cursor.execute(
        "DELETE FROM premium WHERE user_id=?",
        (
            user_id,
        )
    )

    cursor.execute(
        "DELETE FROM users WHERE id=?",
        (
            user_id,
        )
    )

    conn.commit()

    return{

        "status":True,

        "message":"User Deleted Successfully"

    }


# =====================================================
# MY MATCHES
# =====================================================

@app.get("/matches/{user_id}")

def my_matches(user_id:int):

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

