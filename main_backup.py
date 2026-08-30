from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse
from PIL import Image
import json
import os
import shutil
import sqlite3
import google.generativeai as genai
from dotenv import load_dotenv


# --- ส่วนฐานข้อมูล ---
DB_NAME = "database/receipts.db"

# ปรับฟังก์ชันสร้างตารางใน main.py ให้รองรับช่องเก็บ JSON ของพาเลท
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.JSON if hasattr(sqlite3, 'JSON') else conn.cursor() # หรือเก็บเป็น TEXT แล้ว parse JSON เอา
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS receipt_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            document_no TEXT,
            date TEXT,
            customer_name TEXT,
            customer_code TEXT,
            license_plate TEXT,
            driver_name TEXT,
            pallet_details TEXT  -- เพิ่มคอลัมน์นี้เพื่อเก็บรายการพาเลททั้งหมดในรูปแบบ JSON string
        )
    ''')
    conn.commit()
    conn.close()

def save_to_db(filename: str, data: dict):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # ใช้ .get(..., {}) เพื่อป้องกัน Error ในกรณีที่ AI ดึงบางฟิลด์ไม่เจอ
    customer = data.get("customer", {})
    logistics = data.get("logistics", {})
    delivered = data.get("pallets_delivered", {})
    returned = data.get("pallets_returned", {})
    
    cursor.execute('''
        INSERT INTO receipt_data (
            filename, document_no, date, 
            customer_name, customer_code, 
            license_plate, driver_name, 
            delivered_manufacturer, delivered_code, delivered_quantity, 
            returned_manufacturer, returned_code, returned_quantity_actual
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        filename,
        data.get("document_no", ""),
        data.get("date", ""),
        
        customer.get("name", ""),
        customer.get("code", ""),
        
        logistics.get("license_plate", ""),
        logistics.get("driver_name", ""),
        
        delivered.get("manufacturer", ""),
        delivered.get("code", ""),
        delivered.get("quantity", 0),
        
        returned.get("manufacturer", ""),
        returned.get("code", ""),
        returned.get("quantity_actual", 0)
    ))
    conn.commit()
    conn.close()

init_db()

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=API_KEY)

app = FastAPI()

def extract_data_from_image(image_path: str) -> dict:
    try:
        img = Image.open(image_path)
    except FileNotFoundError:
        print(f"not found image path")
        exit()

    model = genai.GenerativeModel('gemini-3.5-flash-lite')

    prompt = """
    คุณคือระบบ Data Entry หน้าที่ของคุณคือการอ่านข้อมูลจากภาพ 'ใบส่งกระบะ' นี้ 
    และแปลงเป็นรูปแบบ JSON เท่านั้น ห้ามมีคำอธิบายอื่นปนมาเด็ดขาด 
    ข้อควรระวัง: จำนวนกระบะที่รับคืนจะเป็น 'ลายมือเขียน' ให้สังเกตให้ดี

    โครงสร้าง JSON ที่ต้องการ:
    {
    "document_no": "เลขที่เอกสาร (ดูที่มุมขวาบน เช่น PTW...)",
    "date": "วันที่ (เช่น 05/06/2026)",
    "customer": {
        "name": "ชื่อลูกค้า (เช่น บ.เวสเทอร์น...)",
        "code": "รหัสลูกค้า (ตัวเลขใต้ชื่อบริษัท)"
    },
    "logistics": {
        "license_plate": "รถขนส่งทะเบียน",
        "driver_name": "ชื่อพนักงานขับรถ"
    },
    "pallets_delivered": {
        "manufacturer": "ยี่ห้อกระบะ (เช่น Chep)",
        "code": "รหัสกระบะ (เช่น CW)",
        "quantity": จำนวนกระบะที่ส่ง (ตัวเลขในตารางส่วนที่ 1)
    },
    "pallets_returned": {
        "manufacturer": "ยี่ห้อกระบะ (เช่น Chep)",
        "code": "รหัสกระบะ (เช่น CW)",
        "quantity_actual": จำนวนกระบะที่รับคืนกลับมาจริงๆ (ตัวเลขที่เป็นลายมือเขียน ในส่วนที่ 2 และ 3),
    }
    }
    """

    print(f"กำลังส่งรูปภาพ {image_path} ไปให้ AI ประมวลผล...")

    try:
        response = model.generate_content([prompt, img])
        raw_text = response.text.replace('```json', '').replace('```', '').strip()
        
        # แปลง String เป็น Dictionary ของ Python
        extracted_data = json.loads(raw_text)

        # ดึงข้อมูลการใช้ token จาก api
        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count
        output_tokens = usage.candidates_token_count
        total_tokens = usage.total_token_count

        # ==========================================
        # 2. ตั้งค่าเรทราคาของ Gemini 3.5 Flash Lite
        # (อัปเดตตัวเลขตรงนี้หากบริษัทมีเรทราคาเฉพาะ)
        # ==========================================
        # สมมติเรทราคา (USD ต่อ 1 ล้าน Token)
        PRICE_PER_1M_INPUT = 0.30  # เรท Input ของรุ่น Lite จะถูกมาก
        PRICE_PER_1M_OUTPUT = 2.5  # เรท Output 

        input_cost_usd = (input_tokens / 1000000) * PRICE_PER_1M_INPUT
        output_cost_usd = (output_tokens / 1000000) * PRICE_PER_1M_OUTPUT
        total_cost_usd = input_cost_usd + output_cost_usd
        
        # แปลงเป็นเงินบาท (อิงเรท 35 บาท/USD)
        EXCHANGE_RATE = 35 
        total_cost_thb = total_cost_usd * EXCHANGE_RATE
       
    except json.JSONDecodeError:
        print("AI ไม่ได้ตอบกลับมาเป็น JSON ที่ถูกต้อง ลองดูข้อความดิบที่ได้:")
        print(response.text)
    except Exception as e:
        print(f"เกิดข้อผิดพลาด: {e}")
    return {
        "success": True,
        "data": extracted_data,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(total_cost_usd, 6),
            "cost_thb": round(total_cost_thb, 5)
        }
    }

# ==========================================
# 3. สร้าง API Routes
# ==========================================

# 3.1 Route สำหรับเปิดหน้าเว็บ index.html
@app.get("/")
async def serve_homepage():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    # เซฟไฟล์รูปลงเครื่องชั่วคราว
    temp_file_path = f"temp_{file.filename}"
    with open(temp_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    try:
        # ส่งรูปไปให้ AI อ่าน
        result = extract_data_from_image(temp_file_path)
        if not result.get("success"):
            return {"success": False, "message": "AI ไม่สามารถประมวลผลภาพได้"}
        
        actual_data = result.get("data", {})
        save_to_db(file.filename, actual_data)
        return {
            "success": True,
            "filename": file.filename,
            "data": actual_data,
            "usage": result.get("usage", {})
        }
    
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        # ลบไฟล์รูปชั่วคราวทิ้งเพื่อไม่ให้รกเครื่อง
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

# Route สำหรับดึงประวัติทั้งหมดจาก SQLite ส่งให้หน้าเว็บ
@app.get("/api/records")
async def get_records():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # ให้ดึงข้อมูลออกมาเป็นคีย์-ค่า (Dictionary) ได้ง่าย
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM receipt_data ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    records = []
    for row in rows:
        records.append({
            "id": row["id"],
            "documentNumber": row["document_no"],
            "date": row["date"],
            "customer": {
                "name": row["customer_name"],
                "code": row["customer_code"]
            },
            "logistics": {
                "license_plate": row["license_plate"],
                "driver_name": row["driver_name"]
            },
            "pallets_delivered": {
                "manufacturer": row["delivered_manufacturer"],
                "code": row["delivered_code"],
                "quantity": row["delivered_quantity"]
            },
            "pallets_returned": {
                "manufacturer": row["returned_manufacturer"],
                "code": row["returned_code"],
                "quantity_actual": row["returned_quantity_actual"]
            }
        })
    return {"success": True, "data": records}
        