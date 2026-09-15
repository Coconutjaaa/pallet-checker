from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
import os
import re
from dotenv import load_dotenv

load_dotenv()

# ตั้งค่า JWT
# ห้ามมีค่า default เด็ดขาด ถ้าลืมตั้ง SECRET_KEY แล้วแอปยังสตาร์ทได้ด้วยคีย์สำรองที่เดาง่าย
# ใครก็ตามที่รู้คีย์นั้นจะปลอม token เป็น ADMIN ได้ทันทีโดยไม่ต้องมีบัญชี จึงให้พังตั้งแต่ตอนสตาร์ทแทน
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if len(SECRET_KEY) < 32:
    raise RuntimeError(
        "\n"
        "============================================================\n"
        " ไม่พบ SECRET_KEY ที่ใช้งานได้ (ต้องยาวอย่างน้อย 32 ตัวอักษร)\n"
        "------------------------------------------------------------\n"
        " สร้างคีย์ใหม่ด้วยคำสั่ง:\n"
        '   python -c "import secrets; print(secrets.token_hex(32))"\n'
        " แล้วใส่ในไฟล์ .env เป็น  SECRET_KEY=<ค่าที่ได้>\n"
        "============================================================"
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 600 # อายุ token (10 ชั่วโมง)

# สิทธิ์ที่ยอมให้สมัครเองผ่านหน้าเว็บได้ ต้องตรงกับตัวเลือกใน dropdown หน้า register
# ADMIN ไม่อยู่ในลิสต์โดยตั้งใจ เพราะเป็นสิทธิ์ที่เห็นข้อมูลทั้งโรงงานและจัดการผู้ใช้คนอื่นได้
SELF_REGISTER_ROLES = {"CHECKER", "SCALE"}

# รหัสพนักงานที่ยอมรับ กันอักขระแปลกปลอมตั้งแต่ประตูแรก
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9._-]{3,50}$')

PASSWORD_MIN_LENGTH = 8
# bcrypt อ่านรหัสผ่านได้สูงสุด 72 ไบต์ ส่วนที่เกินจะถูกตัดทิ้งเงียบๆ
# ภาษาไทย 1 ตัว = 3 ไบต์ แปลว่าไทย 24 ตัวก็ชนเพดานแล้ว จึงต้องบอกผู้ใช้แทนที่จะปล่อยให้ถูกตัด
PASSWORD_MAX_BYTES = 72

# รหัสผ่านที่ถูกเดาเป็นอันดับต้นๆ ต่อให้หน้าตาผ่านกฎก็ถือว่าอ่อนมาก (ชุดเดียวกับฝั่งหน้าเว็บ)
PASSWORD_COMMON = {
    'password', 'passw0rd', '12345678', '123456789', '1234567890', 'qwerty', 'qwertyui',
    'abc123', 'admin', 'admin123', 'welcome', 'iloveyou', 'letmein', 'monkey', 'dragon',
    'sunshine', 'princess', 'football', 'baseball', 'trustno1', 'p@ssw0rd', 'scg1234'
}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

router = APIRouter(prefix="/auth", tags=["Authentication"])


def validate_password(password: str, *personal_info: str) -> list:
    """ตรวจรหัสผ่านตามกฎเดียวกับที่แสดงในหน้าเว็บ คืนลิสต์ข้อความที่ยังไม่ผ่าน (ว่าง = ผ่าน)

    กฎฝั่งหน้าเว็บกันได้แค่คนที่กรอกผ่านฟอร์ม คนที่ยิง API ตรงข้ามไปได้ทั้งหมด
    จึงต้องตรวจซ้ำที่ฝั่งเซิร์ฟเวอร์ซึ่งเป็นด่านที่ข้ามไม่ได้
    """
    errors = []

    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"ต้องมีอย่างน้อย {PASSWORD_MIN_LENGTH} ตัวอักษร")
    if not re.search(r'[A-Za-z]', password):
        errors.append("ต้องมีตัวอักษรภาษาอังกฤษอย่างน้อย 1 ตัว")
    if not re.search(r'\d', password):
        errors.append("ต้องมีตัวเลขอย่างน้อย 1 ตัว")
    if not re.search(r'[^A-Za-z0-9]', password):
        errors.append("ต้องมีอักขระพิเศษอย่างน้อย 1 ตัว")
    if len(password.encode('utf-8')) > PASSWORD_MAX_BYTES:
        errors.append(
            f"ยาวเกินไป (ระบบเข้ารหัสได้สูงสุด {PASSWORD_MAX_BYTES} ไบต์ "
            "ภาษาอังกฤษ 72 ตัว หรือภาษาไทย 24 ตัว)"
        )

    lower = password.lower()
    if lower in PASSWORD_COMMON:
        errors.append("เป็นรหัสผ่านที่ถูกเดาบ่อยที่สุด ห้ามใช้")
    else:
        for common in PASSWORD_COMMON:
            if len(common) >= 5 and common in lower:
                errors.append(f"มีคำที่เดาง่ายอยู่ในรหัสผ่าน ('{common}')")
                break

    # รหัสที่มีชื่อหรือรหัสพนักงานตัวเองอยู่ ถือว่าเดาง่ายมากสำหรับคนใกล้ตัว
    for info in personal_info:
        info = (info or "").strip()
        if len(info) >= 3 and info.lower() in lower:
            errors.append("ห้ามมีรหัสพนักงานหรือชื่อของตัวเองอยู่ในรหัสผ่าน")
            break

    return errors

def get_db_connection():
    DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
    return psycopg2.connect(DATABASE_URL)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Model สำหรับสร้าง User
class UserCreate(BaseModel):
    username: str
    password: str
    role: str
    first_name: str
    last_name: str

@router.post("/register")
async def register(user: UserCreate):
    # 1. ตรวจความถูกต้องของข้อมูลทั้งหมดก่อนแตะฐานข้อมูล
    username = (user.username or "").strip()
    first_name = (user.first_name or "").strip()
    last_name = (user.last_name or "").strip()
    role = (user.role or "").strip().upper()

    if not USERNAME_PATTERN.match(username):
        raise HTTPException(
            status_code=400,
            detail="รหัสพนักงานต้องยาว 3-50 ตัว และใช้ได้เฉพาะ A-Z 0-9 จุด ขีดกลาง ขีดล่าง"
        )
    if not first_name or not last_name:
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อและนามสกุล")
    if len(first_name) > 100 or len(last_name) > 100:
        raise HTTPException(status_code=400, detail="ชื่อหรือนามสกุลยาวเกินไป")

    # สิทธิ์ต้องอยู่ในลิสต์ที่อนุญาตเท่านั้น ห้ามเชื่อค่าที่ส่งมาจากฝั่ง client
    # (dropdown ในหน้าเว็บกันได้แค่คนที่กรอกผ่านฟอร์ม คนที่ยิง API ตรงเลือกอะไรก็ได้)
    if role not in SELF_REGISTER_ROLES:
        raise HTTPException(
            status_code=400,
            detail="ตำแหน่งไม่ถูกต้อง เลือกได้เฉพาะ พนักงานตรวจพาเลท (CHECKER) หรือพนักงานห้องชั่ง (SCALE)"
        )

    password_errors = validate_password(user.password or "", username, first_name, last_name)
    if password_errors:
        raise HTTPException(
            status_code=400,
            detail="รหัสผ่านไม่ผ่านเกณฑ์: " + " / ".join(password_errors)
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 2. เช็คว่ามี username นี้ซ้ำหรือไม่
        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="Username นี้มีอยู่ในระบบแล้ว")

        # 3. เข้ารหัสผ่าน (Hash) ก่อนบันทึก
        hashed_pwd = pwd_context.hash(user.password)

        # 4. บันทึกลงตาราง users (is_approved เป็น FALSE ตาม default รอ Admin อนุมัติ)
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, first_name, last_name) VALUES (%s, %s, %s, %s, %s)",
            (username, hashed_pwd, role, first_name, last_name)
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        # ไม่ส่งข้อความ error จากฐานข้อมูลกลับไปให้ client เพราะมันบอกโครงสร้างตารางและเวอร์ชัน DB
        # ซึ่งเป็นข้อมูลตั้งต้นชั้นดีให้คนที่กำลังหาช่องเจาะระบบ เก็บไว้ดูใน log ฝั่งเซิร์ฟเวอร์พอ
        print(f"❌ Register DB Error: {e}")
        raise HTTPException(status_code=500, detail="ไม่สามารถสร้างผู้ใช้งานได้ กรุณาลองใหม่อีกครั้ง")
    finally:
        conn.close()

    return {"message": f"สร้างผู้ใช้ {username} สิทธิ์ {role} สำเร็จ!"}

@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # ค้นหา user จากฐานข้อมูล
    cursor.execute("SELECT username, password_hash, role, is_approved FROM users WHERE username = %s", (form_data.username,))
    user = cursor.fetchone()
    conn.close()

    # ตรวจสอบ user และรหัสผ่าน
    if not user or not verify_password(form_data.password, user[1]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="รหัสพนักงานหรือรหัสผ่านไม่ถูกต้อง",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user[3]:
        raise HTTPException(status_code=403, detail="บัญชีของคุณกำลังรอ Admin อนุมัติการเข้าใช้งาน")
    
    # สร้าง Token
    access_token = create_access_token(data={"sub": user[0], "role": user[2]})
    return {"access_token": access_token, "token_type": "bearer", "role": user[2]}

async def get_current_user(token: str = Depends(oauth2_scheme)):
    invalid_token = HTTPException(
        status_code=401,
        detail="Token หมดอายุหรือไม่ถูกต้อง",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise invalid_token

    username = payload.get("sub")
    if not username:
        raise invalid_token

    # ต้องกลับมาถามฐานข้อมูลทุกครั้ง ไม่ใช้ค่าที่ฝังมาใน token
    # เพราะ token มีอายุ 10 ชั่วโมง ถ้าเชื่อค่าใน token อย่างเดียว คนที่ถูกระงับสิทธิ์หรือถูกลบ
    # จะยังดึงข้อมูลได้ต่อไปจนกว่า token จะหมดอายุเอง เท่ากับปุ่มระงับสิทธิ์ไม่มีผลจริง
    # สิทธิ์ (role) ก็ดึงจาก DB ด้วย เพื่อให้การเปลี่ยนสิทธิ์มีผลทันทีเช่นกัน
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, role, is_approved FROM users WHERE username = %s",
            (username,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=401, detail="บัญชีนี้ไม่มีอยู่ในระบบแล้ว กรุณาเข้าสู่ระบบใหม่")
    if not row[2]:
        raise HTTPException(status_code=403, detail="บัญชีของคุณถูกระงับสิทธิ์ กรุณาติดต่อผู้ดูแลระบบ")

    return {"username": row[0], "role": row[1]}

@router.get("/users/pending")
async def get_pending_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT id, username, first_name, last_name, role FROM users WHERE is_approved = FALSE")
    users = cursor.fetchall()
    conn.close()
    return {"success": True, "data": [dict(u) for u in users]}

@router.get("/users/approved")
async def get_approved_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # ดึงเฉพาะคนที่ถูกอนุมัติแล้ว และไม่ดึง Admin คนที่กำลัง Login อยู่ (ป้องกันการเผลอระงับสิทธิ์ตัวเอง)
    cursor.execute("""
        SELECT id, username, first_name, last_name, role 
        FROM users 
        WHERE is_approved = true AND username != %s
    """, (current_user["username"],))
    users = cursor.fetchall()
    conn.close()
    
    return {"success": True, "data": [dict(u) for u in users]}

@router.put("/users/approve/{user_id}")
async def approve_user(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_approved = TRUE WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "อนุมัติผู้ใช้งานเรียบร้อยแล้ว"}


# ==========================================
# 2. API ระงับสิทธิ์การใช้งาน (เปลี่ยนเป็นรออนุมัติใหม่)
# ==========================================
@router.put("/users/suspend/{user_id}")
async def suspend_user(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    # อัปเดตสถานะกลับเป็น FALSE 
    cursor.execute("UPDATE users SET is_approved = FALSE WHERE id = %s", (user_id,))
    row_count = cursor.rowcount
    conn.commit()
    conn.close()
    
    if row_count == 0:
        return {"success": False, "detail": "ไม่พบผู้ใช้งานนี้ในระบบ"}
        
    return {"success": True, "message": "ระงับสิทธิ์ผู้ใช้งานเรียบร้อยแล้ว"}


# ==========================================
# 3. API ปฏิเสธคำขอ / ลบผู้ใช้งานทิ้ง (สำหรับปุ่ม "ปฏิเสธ" ในแท็บรออนุมัติ)
# ==========================================
@router.delete("/users/{user_id}")
async def delete_user(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    row_count = cursor.rowcount
    conn.commit()
    conn.close()
    
    if row_count == 0:
        return {"success": False, "detail": "ไม่พบข้อมูลผู้ใช้งานนี้"}
        
    return {"success": True, "message": "ลบข้อมูลผู้ใช้งานออกจากระบบเรียบร้อยแล้ว"}