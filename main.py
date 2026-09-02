from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, Form, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from PIL import Image, ImageOps
import json
import os
import shutil
import google.generativeai as genai
from dotenv import load_dotenv
import pandas as pd
import time
import asyncio
from datetime import datetime, timezone, timedelta
import numpy as np
import cv2
import io
import base64
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- เพิ่ม Library สำหรับ PostgreSQL ---
import psycopg2
import psycopg2.extras
from sqlalchemy import create_engine
import gc

# นำเข้า Router และระบบตรวจสอบผู้ใช้งาน
from auth import router as auth_router, get_current_user

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLALCHEMY_DB_URL = DATABASE_URL.replace("postgres://", "postgresql://")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def four_point_transform(image, pts):
    rect = np.array([
        [pts[0]['x'], pts[0]['y']],
        [pts[1]['x'], pts[1]['y']],
        [pts[2]['x'], pts[2]['y']],
        [pts[3]['x'], pts[3]['y']]
    ], dtype="float32")

    (tl, tr, br, bl) = rect
    widthA = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
    widthB = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
    maxWidth = max(int(widthA), int(widthB))

    heightA = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
    heightB = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
    maxHeight = max(int(heightA), int(heightB))

    dst = np.array([
        [0, 0],
        [maxWidth - 1, 0],
        [maxWidth - 1, maxHeight - 1],
        [0, maxHeight - 1]
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, M, (maxWidth, maxHeight))
    return warped

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()
loop = None 

class PalletDetail(BaseModel):
    name: str
    qty: int

class RecordCreate(BaseModel):
    id: Optional[int] = None 
    documentNumber: str
    date: str
    customer_name: str
    customer_code: str
    expectedQty: int
    actualQty: int
    checkerName: str
    palletDetails: List[PalletDetail]
    imageBase64: Optional[str] = None
    plant_short_name: Optional[str] = None

class TruckImageCreate(BaseModel):
    license_plate: str
    image_base64: str
    operator_name: Optional[str] = "SCALE"

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS receipt_data (
            id SERIAL PRIMARY KEY,
            document_no TEXT,
            date TEXT,
            customer_name TEXT,
            customer_code TEXT,
            expected_qty INTEGER,
            actual_qty INTEGER,
            checker_name TEXT,
            pallet_details TEXT,
            image_base64 TEXT,
            plant_short_name TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS truck_scale_images (
            id SERIAL PRIMARY KEY,
            license_plate TEXT,
            image_base64 TEXT,
            truck_weighing_key TEXT, 
            operator_name TEXT,
            created_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            is_approved BOOLEAN DEFAULT FALSE
        )
    ''')
    
    try:
        cursor.execute("ALTER TABLE truck_scale_images ADD COLUMN IF NOT EXISTS truck_weighing_key TEXT")
    except Exception:
        conn.rollback()
        pass 
    
    conn.commit()
    conn.close()

def load_master_data():
    pallet_file = "database/truckscale/MasterData_Pallet.xlsx"
    plant_file = "database/truckscale/MasterData_Plant.xlsx"
    
    engine = create_engine(SQLALCHEMY_DB_URL)
    
    if os.path.exists(pallet_file):
        df_pallet = pd.read_excel(pallet_file)
        df_pallet.to_sql('master_pallet', engine, if_exists='replace', index=False, chunksize=1000)
        del df_pallet 
        gc.collect()  
        
    if os.path.exists(plant_file):
        df_plant = pd.read_excel(plant_file)
        df_plant.to_sql('master_plant', engine, if_exists='replace', index=False, chunksize=1000)
        del df_plant
        gc.collect()
        
    engine.dispose()
    print("✅ โหลด Master Data (Plant, Pallet) เสร็จสมบูรณ์")

def load_transaction_data():
    delivery_file = "database/truckscale/PalletControl_PalletDelivery.xlsx"
    weighing_file = "database/truckscale/Weighing_TruckWeighing.xlsx"
    
    engine = create_engine(SQLALCHEMY_DB_URL)
    
    try:
        if os.path.exists(delivery_file):
            df_delivery = pd.read_excel(delivery_file)
            df_delivery.to_sql('pallet_delivery', engine, if_exists='replace', index=False, chunksize=1000)
            del df_delivery
            gc.collect()

        if os.path.exists(weighing_file):
            df_weighing = pd.read_excel(weighing_file)
            df_weighing.to_sql('truck_weighing', engine, if_exists='replace', index=False, chunksize=1000)
            del df_weighing
            gc.collect()
            
        print("🔄 [อัปเดตอัตโนมัติ] ข้อมูลรถบรรทุกล่าสุดถูกโหลดเข้า Database แล้ว!")
        
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(manager.broadcast("UPDATE"), loop)
            
    except Exception as e:
        print(f"⚠️ เกิดข้อผิดพลาดในการโหลดข้อมูลรถ: {e}")
    finally:
        engine.dispose()

class ExcelFileHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith((".xlsx", ".xls")):
            filename = os.path.basename(event.src_path)
            if filename in ["PalletControl_PalletDelivery.xlsx", "Weighing_TruckWeighing.xlsx"]:
                time.sleep(1) 
                load_transaction_data()

API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

app = FastAPI()

# Mount auth router เข้ากับแอปหลัก
app.include_router(auth_router)

@app.on_event("startup")
def startup_event():
    global loop
    loop = asyncio.get_running_loop() 

    if DATABASE_URL:
        init_db()
    
    folder_to_watch = os.path.join(os.getcwd(), "database", "truckscale")
    if os.path.exists(folder_to_watch):
        event_handler = ExcelFileHandler()
        observer = Observer()
        observer.schedule(event_handler, path=folder_to_watch, recursive=False)
        observer.start()

def extract_data_from_image(image_path: str, points_str: str = None) -> dict:
    error_message = None
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        orig_width, orig_height = img.size
        img.thumbnail((1200, 1200))
        new_width, new_height = img.size
        ratio_x = new_width / orig_width
        ratio_y = new_height / orig_height
    except FileNotFoundError:
        return {"success": False, "message": "ไม่พบไฟล์ภาพ"}

    if points_str:
        pts = json.loads(points_str)
        for pt in pts:
            pt['x'] = int(pt['x'] * ratio_x)
            pt['y'] = int(pt['y'] * ratio_y)
        cv_img = np.array(img)
        
        warped_cv = four_point_transform(cv_img, pts)
        gray = cv2.cvtColor(warped_cv, cv2.COLOR_RGB2GRAY)
        
        bg = cv2.medianBlur(gray, 31)
        scanned_cv = cv2.divide(gray, bg, scale=255)
        scanned_cv = cv2.normalize(scanned_cv, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        
        warped_cv = cv2.cvtColor(scanned_cv, cv2.COLOR_GRAY2RGB)
        img = Image.fromarray(warped_cv)

    width, height = img.size

    def image_to_base64_str(pil_img):
        buffered = io.BytesIO()
        pil_img.save(buffered, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(buffered.getvalue()).decode('utf-8')

    full_img_b64 = image_to_base64_str(img)

    model = genai.GenerativeModel(
        'gemini-3.5-flash-lite',
        generation_config={"response_mime_type": "application/json"}
    )
    
    prompt = """
    คุณคือระบบ Data Entry หน้าที่ของคุณคือการอ่านข้อมูลจากภาพ 'ใบส่งกระบะ' 
    ให้แยกข้อมูลเป็น JSON โดยมีเงื่อนไขรัดกุมดังนี้:
    1. ค้นหา "ตัวเลขลายมือเขียน" ที่ระบุ "จำนวนกระบะที่ส่งคืน" (เน้นพิจารณาส่วนที่ 2 หลังคำว่า "ส่งคืน" หรือ "ได้รับกระบะไว้จำนวน ... กระบะ")
    2. ห้ามดึงพิกัดบริเวณ "ลงชื่อ", "ผู้รับ/คืนกระบะ", "ลายเซ็น" หรือ "วันที่" ด้านล่างของกระดาษโดยเด็ดขาด ให้โฟกัสแค่จำนวนกระบะเท่านั้น
    3. ให้คืนค่าพิกัดตำแหน่ง (Bounding Box) ของตัวเลขนั้น ในรูปแบบ [ymin, xmin, ymax, xmax] สเกล 0-1000

    โครงสร้าง JSON ที่ต้องการ (ให้ตอบกลับมาเป็น JSON โครงสร้างนี้เท่านั้น ห้ามตัดคีย์ใดทิ้ง):
    {
    "document_no": "เลขที่เอกสาร (ดูที่มุมขวาบน)",
    "date": "วันที่ออกเอกสาร (อยู่ด้านล่างของเลขที่เอกสาร)",
    "customer": {
        "name": "ชื่อลูกค้า (เช่น บ.เวสเทอร์น แอพไพลแอนซ์)",
        "code": "รหัสลูกค้า (อยู่ติดกับชื่อลูกค้า -> เป็นตัวเลข)"
    },
    "pallets_delivered": {
        "manufacturer": "ยี่ห้อกระบะ (เช่น Chep)",
        "code": "รหัสกระบะ (เช่น CW)",
        "quantity": "จำนวนกระบะที่ส่ง (ตัวเลขในตารางส่วนที่ 1)"
    },
    "pallets_returned": {
        "manufacturer": "ยี่ห้อกระบะ (เช่น Chep)",
        "code": "รหัสกระบะ (เช่น CW)",
        "quantity_actual": "จำนวนกระบะที่รับคืนกลับมาจริงๆ (ตัวเลขที่เป็นลายมือเขียน)",
        "quantity_bbox": [0, 0, 0, 0]
    },
    "plant_short_name": "ชื่อโรงงานที่เป็นตราประทับอยู่ด้านล่างของกระดาษ"
    }
    """
    
    total_cost_usd, total_cost_thb = 0, 0
    input_tokens, output_tokens, total_tokens = 0, 0, 0
    extracted_data = {}
    crop_img_b64 = ""

    try:
        response = model.generate_content([prompt, img])
        
        response_text = response.text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:-3].strip()
        elif response_text.startswith("```"):
            response_text = response_text[3:-3].strip()
            
        extracted_data = json.loads(response_text)

        if "pallets_returned" in extracted_data and "quantity_bbox" in extracted_data["pallets_returned"]:
            bbox = extracted_data["pallets_returned"]["quantity_bbox"]
            
            if isinstance(bbox, str):
                try: 
                    bbox = json.loads(bbox)
                except: 
                    pass

            if isinstance(bbox, list) and len(bbox) == 4 and sum(bbox) > 0:
                ymin, xmin, ymax, xmax = bbox
                
                pad_x = int(width * 0.04)
                pad_y = int(height * 0.04)

                real_xmin = max(0, int((xmin / 1000) * width) - pad_x)
                real_ymin = max(0, int((ymin / 1000) * height) - pad_y)
                real_xmax = min(width, int((xmax / 1000) * width) + pad_x)
                real_ymax = min(height, int((ymax / 1000) * height) + pad_y)

                if real_xmax > real_xmin and real_ymax > real_ymin:
                    cropped_img = img.crop((real_xmin, real_ymin, real_xmax, real_ymax))
                    crop_img_b64 = image_to_base64_str(cropped_img)

        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            input_tokens = getattr(usage, 'prompt_token_count', 0)
            output_tokens = getattr(usage, 'candidates_token_count', 0)
            total_tokens = getattr(usage, 'total_token_count', 0)

            PRICE_PER_1M_INPUT = 0.30
            PRICE_PER_1M_OUTPUT = 2.5 
            input_cost_usd = (input_tokens / 1000000) * PRICE_PER_1M_INPUT
            output_cost_usd = (output_tokens / 1000000) * PRICE_PER_1M_OUTPUT
            total_cost_usd = input_cost_usd + output_cost_usd
            total_cost_thb = total_cost_usd * 35 
       
    except json.JSONDecodeError as e:
        error_message = f"Gemini ตอบกลับผิดรูปแบบ (ไม่ใช่ JSON): {str(e)}"
    except Exception as e:
        error_message = f"AI ไม่สามารถดึงข้อมูลได้: {str(e)}"

    return {
        "success": True if extracted_data else False,
        "message": error_message,
        "data": extracted_data,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
            "cost_usd": round(total_cost_usd, 6), "cost_thb": round(total_cost_thb, 5)
        },
        "images": {                  
            "full": full_img_b64,
            "crop": crop_img_b64
        }
    }

@app.get("/")
async def serve_homepage():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# [🔒 PROTECTED] อัปโหลดภาพ (สงวนไว้ให้ผู้ใช้ที่ Login เท่านั้น)
@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...), 
    points: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    temp_file_path = f"temp_{file.filename}"
    with open(temp_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    try:
        result = extract_data_from_image(temp_file_path, points)
        if not result.get("success"):
            err_msg = result.get("message") or "AI ไม่สามารถประมวลผลภาพหรือแยก JSON ได้"
            return {"success": False, "message": err_msg}
        
        return {
            "success": True, 
            "filename": file.filename, 
            "data": result.get("data", {}),
            "images": result.get("images", {})
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

# [🔒 PROTECTED] บันทึกข้อมูลใบนำส่ง
@app.post("/api/save")
async def save_record(
    record: RecordCreate,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        pallet_details_json = json.dumps([p.dict() for p in record.palletDetails], ensure_ascii=False)
        
        if record.id:
            cursor.execute('''
                UPDATE receipt_data 
                SET document_no=%s, date=%s, customer_name=%s, customer_code=%s, 
                    expected_qty=%s, actual_qty=%s, checker_name=%s, pallet_details=%s, image_base64=%s, plant_short_name=%s
                WHERE id=%s
            ''', (record.documentNumber, record.date, record.customer_name, record.customer_code, record.expectedQty, record.actualQty, record.checkerName, pallet_details_json, record.imageBase64, record.plant_short_name, record.id))
        else:
            cursor.execute('''
                INSERT INTO receipt_data (document_no, date, customer_name, customer_code, expected_qty, actual_qty, checker_name, pallet_details, image_base64, plant_short_name) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (record.documentNumber, record.date, record.customer_name, record.customer_code, record.expectedQty, record.actualQty, record.checkerName, pallet_details_json, record.imageBase64, record.plant_short_name))
        
        conn.commit()
        conn.close()
        await manager.broadcast("UPDATE")
        return {"success": True, "message": "บันทึกข้อมูลสำเร็จ"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# [🔒 PROTECTED] บันทึกภาพรถบรรทุก
@app.post("/api/save-truck-image")
async def save_truck_image(
    payload: TruckImageCreate,
    current_user: dict = Depends(get_current_user)
):
    try:
        clean_plate = payload.license_plate.strip().replace(" ", "")
        if not clean_plate:
            return {"success": False, "message": "กรุณาระบุทะเบียนรถ"}
        if not payload.image_base64:
            return {"success": False, "message": "ไม่พบข้อมูลรูปภาพ"}

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        cursor.execute('''
            SELECT "row_key" 
            FROM truck_weighing 
            WHERE REPLACE(CAST("carRegister" AS TEXT), ' ', '') = %s
              AND ("weightOut" IS NULL OR CAST("weightOut" AS TEXT) = '0' OR CAST("weightOut" AS TEXT) = '' OR "weightOutTime" IS NULL OR CAST("weightOutTime" AS TEXT) = '' OR CAST("weightOutTime" AS TEXT) = '-')
            ORDER BY "row_key" DESC 
            LIMIT 1
        ''', (clean_plate,))
        
        active_truck = cursor.fetchone()
        
        if not active_truck:
            conn.close()
            return {
                "success": False, 
                "message": f"❌ ไม่พบรถทะเบียน '{payload.license_plate}' ที่กำลังชั่งอยู่ในระบบ\n(รถอาจชั่งออกไปแล้ว หรือทะเบียนไม่ตรงกับใบตาชั่ง)"
            }
            
        truck_row_key = active_truck['row_key']

        # ตรวจสอบว่าสำหรับรอบการชั่งครั้งนี้ของรถคันนี้ถูกบันทึกภาพไปแล้วหรือยัง -> ป้องกันไม่ให้บันทึกซ้ำ
        cursor.execute('''
            SELECT id FROM truck_scale_images
            WHERE truck_weighing_key = %s
        ''', (truck_row_key,)
        )
        existing_image = cursor.fetchone()
        if existing_image:
            conn.close()
            return {
                "success": False,
                "message": f"⚠️ รถทะเบียน '{payload.license_plate}' คันนี้ถูกถ่ายรูปไปแล้ว!\n(รถยังอยู่ในโรงงาน ไม่อนุญาตให้บันทึกซ้ำเพื่อป้องกันข้อมูลทับซ้อน)"
            }
        
        tz_th = timezone(timedelta(hours=7))
        created_at = datetime.now(tz_th).strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute('''
            INSERT INTO truck_scale_images (license_plate, image_base64, truck_weighing_key, operator_name, created_at)
            VALUES (%s, %s, %s, %s, %s)
        ''', (clean_plate, payload.image_base64, truck_row_key, payload.operator_name, created_at))

        conn.commit()
        conn.close()
        await manager.broadcast("UPDATE")

        return {"success": True, "message": f"✅ จับคู่และบันทึกรูปรถทะเบียน '{payload.license_plate}' ลงในรอบการชั่งปัจจุบันสำเร็จ!"}
    except Exception as e:
        print(f"❌ API Save Truck Image Error: {e}")
        return {"success": False, "message": str(e)}

# [🔒 PROTECTED] ดึงประวัติข้อมูลทั้งหมด
@app.get("/api/records")
async def get_records(current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # ปรับปรุง SQL ครั้งที่ 3: 
        # แก้ปัญหาบิลจับคู่รถผิดคัน (มั่ว) ที่เกิดจากข้อมูลว่าง (NaN, Null) จาก Excel วิ่งไปตรงกันเอง
        cursor.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (r.document_no) 
                       r.*, 
                       truck."carRegister" as license_plate, 
                       truck."weightInTime" as weight_in_time, 
                       truck."weightIn" as weight_in, 
                       truck."weightOutTime" as weight_out_time, 
                       truck."weightOut" as weight_out,
                       timg.image_base64 as truck_image_base64,
                       COALESCE(CAST(NULLIF(CAST(truck."weightIn" AS TEXT), '') AS NUMERIC), 0) - COALESCE(CAST(NULLIF(CAST(truck."weightOut" AS TEXT), '') AS NUMERIC), 0) as pallet_weight
                FROM receipt_data r
                
                -- 1. ดึง Key ของรถจาก pallet_delivery โดยจัดกลุ่มและกรองค่า NaN หรือ Null ทิ้งให้หมด
                LEFT JOIN (
                    SELECT 
                        REPLACE(TRIM(CAST("receiptNumber" AS TEXT)), ' ', '') as clean_receipt_no, 
                        MAX(CAST("truckWeighingKey" AS TEXT)) as truck_key
                    FROM pallet_delivery
                    WHERE "truckWeighingKey" IS NOT NULL 
                      AND LOWER(CAST("truckWeighingKey" AS TEXT)) NOT IN ('', 'nan', 'none', 'null')
                    GROUP BY REPLACE(TRIM(CAST("receiptNumber" AS TEXT)), ' ', '')
                ) delivery ON REPLACE(TRIM(CAST(r.document_no AS TEXT)), ' ', '') = delivery.clean_receipt_no
                
                -- 2. นำ Key ไปค้นหารถในตาราง truck_weighing ป้องกันการจับคู่มั่วจากค่าว่าง
                LEFT JOIN truck_weighing truck 
                       ON delivery.truck_key IS NOT NULL 
                      AND TRIM(SPLIT_PART(delivery.truck_key, '.', 1)) = TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1))
                      AND LOWER(CAST(truck."row_key" AS TEXT)) NOT IN ('', 'nan', 'none', 'null')
                
                -- 3. หาภาพถ่ายรถ
                LEFT JOIN (
                    SELECT DISTINCT ON (truck_weighing_key) truck_weighing_key, image_base64
                    FROM truck_scale_images
                    WHERE truck_weighing_key IS NOT NULL
                    ORDER BY truck_weighing_key, id DESC
                ) timg ON TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1)) = timg.truck_weighing_key
                
                ORDER BY r.document_no, r.id DESC
            ) AS unique_records
            ORDER BY id DESC
        """)
        rows = cursor.fetchall()
        conn.close()

        records = []
        for row in rows:
            records.append({
                "id": row["id"],
                "documentNumber": row["document_no"],
                "date": row["date"],
                "customer_name": row["customer_name"],
                "customer_code": row["customer_code"],
                "expectedQty": row["expected_qty"],
                "actualQty": row["actual_qty"],
                "checkerName": row["checker_name"],
                "palletDetails": json.loads(row["pallet_details"]) if row["pallet_details"] else [],
                "imageBase64": row["image_base64"],
                "plant_short_name": row["plant_short_name"],
                "truckDetail": {
                    "license_plate": row["license_plate"] or "-",
                    "weight_in_time": row["weight_in_time"] or "-",
                    "weight_in": row["weight_in"] or 0,
                    "weight_out_time": row["weight_out_time"] or "รอรถออก",
                    "weight_out": row["weight_out"] or 0,
                    "pallet_weight": row["pallet_weight"] or 0,
                    "truck_image_base64": row["truck_image_base64"] or None
                }
            })
        return {"success": True, "data": records}
    except Exception as e:
        print(f"❌ API Records Error: {e}")
        return {"success": False, "message": str(e)}
        
# [🔒 PROTECTED] ล้างข้อมูล (เฉพาะ Admin เท่านั้น)
@app.delete("/api/clear")
async def clear_db(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "ADMIN":
        return {"success": False, "message": "เฉพาะ Admin เท่านั้นที่มีสิทธิ์ล้างข้อมูล"}
        
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM receipt_data")
        cursor.execute("DELETE FROM truck_scale_images")
        conn.commit()
        conn.close()
        await manager.broadcast("UPDATE")
        return {"success": True, "message": "ล้างข้อมูลทั้งหมดแล้ว"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# [🔒 PROTECTED] ดึงรายชื่อพาเลท
@app.get("/api/pallets/{plant_short_name}")
async def get_pallets_by_plant(
    plant_short_name: str,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        sql = """
            SELECT pallet."name" || ' - ' || pallet."code" 
            FROM master_pallet as pallet
            INNER JOIN master_plant as plant ON pallet."plantId" = plant."row_key"
            WHERE TRIM(plant."shortName") = %s
        """
        cursor.execute(sql, (plant_short_name,))
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            pallets = [str(row[0]).strip() for row in rows if row[0] is not None and str(row[0]).strip() != ""]
            return {"success": True, "pallets": pallets}
        else:
            return {"success": True, "pallets": []}
            
    except Exception as e:
        print(f"❌ API Pallets Error: {e}") 
        return {"success": False, "message": str(e)}

# [🔒 PROTECTED] รีโหลด Master Data
@app.get("/api/manual-reload")
async def manual_reload(current_user: dict = Depends(get_current_user)):
    try:
        load_master_data()
        load_transaction_data()
        return {"success": True, "message": "อัปเดตข้อมูลจาก Excel ลง PostgreSQL สำเร็จแล้ว!"}
    except Exception as e:
        return {"success": False, "message": str(e)}