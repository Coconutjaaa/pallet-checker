from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

# ตั้งค่า JWT
SECRET_KEY = os.environ.get("SECRET_KEY", "login101")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 600 # อายุ token (10 ชั่วโมง)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

router = APIRouter(prefix="/auth", tags=["Authentication"])

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
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. เช็คว่ามี username นี้ซ้ำหรือไม่
    cursor.execute("SELECT username FROM users WHERE username = %s", (user.username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username นี้มีอยู่ในระบบแล้ว")
    
    # 2. เข้ารหัสผ่าน (Hash) ก่อนบันทึก
    hashed_pwd = pwd_context.hash(user.password)
    
    # 3. บันทึกลงตาราง users
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, first_name, last_name) VALUES (%s, %s, %s, %s, %s)",
            (user.username, hashed_pwd, user.role.upper(), user.first_name, user.last_name)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        conn.close()
        
    return {"message": f"สร้างผู้ใช้ {user.username} สิทธิ์ {user.role.upper()} สำเร็จ!"}

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
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None:
            raise HTTPException(status_code=401, detail="ไม่ผ่านการตรวจสอบ")
        return {"username": username, "role": role}
    except JWTError:
        raise HTTPException(status_code=401, detail="Token หมดอายุหรือไม่ถูกต้อง")

@router.get("/users/pending")
async def get_pending_users(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึง")
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT id, username, role FROM users WHERE is_approved = FALSE")
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