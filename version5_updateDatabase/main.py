from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, Form, Depends, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import List, Optional
from PIL import Image, ImageOps
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from google import genai
from google.genai import types
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

def order_quad_points(pts):
    """เรียง 4 จุดใหม่จากตำแหน่งจริงบนภาพ ให้ได้ลำดับ บนซ้าย → บนขวา → ล่างขวา → ล่างซ้าย

    ไม่สนใจว่าลำดับที่ส่งเข้ามาเป็นอย่างไร เพราะผู้ใช้ลากจุดสลับตำแหน่งกันได้อิสระ
    ถ้าเชื่อลำดับเดิม (จุดที่ 1 ต้องเป็นมุมบนซ้ายเสมอ) พอผู้ใช้ลากจุดบนซ้ายไปไว้มุมล่างขวา
    กรอบจะไขว้กันเป็นโบว์ ภาพที่ได้จะบิดจนอ่านไม่ออก

    วิธี: เรียงจุดตามมุมรอบจุดศูนย์กลาง จะได้รูปสี่เหลี่ยมที่เส้นไม่ตัดกันเสมอ
    แล้วหมุนลำดับให้เริ่มที่จุดที่ใกล้มุมบนซ้ายที่สุด (ค่า x+y น้อยสุด)
    """
    coords = [(float(p['x']), float(p['y'])) for p in pts]

    cx = sum(c[0] for c in coords) / len(coords)
    cy = sum(c[1] for c in coords) / len(coords)

    # แกน y ของภาพชี้ลง มุมที่เพิ่มขึ้นจึงไล่ตามเข็มนาฬิกาบนภาพ: ขวา → ล่าง → ซ้าย → บน
    coords.sort(key=lambda c: math.atan2(c[1] - cy, c[0] - cx))

    start = min(range(len(coords)), key=lambda i: coords[i][0] + coords[i][1])
    coords = coords[start:] + coords[:start]

    return coords


def four_point_transform(image, pts):
    rect = np.array(order_quad_points(pts), dtype="float32")

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
    # [จุดที่ 1] เปลี่ยนชื่อ Parameter ที่รับจาก Client
    plant_ticketcode: Optional[str] = None
    license_plate: Optional[str] = None

class TruckImageCreate(BaseModel):
    license_plate: str
    image_base64: str
    operator_name: Optional[str] = "SCALE"
    driver_name: Optional[str] = None
    pallet_quantity: Optional[int] = None

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
            plant_ticketcode TEXT,
            calculated_weight NUMERIC DEFAULT 0
        )
    ''')
    
    # [จุดที่ 2] เปลี่ยน schema ของ truck_scale_images ให้ใช้ plant_ticketcode ด้วย
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS truck_scale_images (
            id SERIAL PRIMARY KEY,
            license_plate TEXT,
            image_base64 TEXT,
            truck_weighing_key TEXT,
            operator_name TEXT,
            plant_ticketcode TEXT,
            driver_name TEXT,
            pallet_quantity INTEGER
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
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    try:
        cursor.execute("ALTER TABLE truck_scale_images ADD COLUMN IF NOT EXISTS driver_name TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    try:
        cursor.execute("ALTER TABLE truck_scale_images ADD COLUMN IF NOT EXISTS pallet_quantity INTEGER")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    # [จุดที่ 3] จัดการอัปเดตชื่อคอลัมน์จากของเดิม (ถ้ามี) ให้เปลี่ยนเป็นแบบใหม่ทั้งหมด
    try:
        cursor.execute("ALTER TABLE receipt_data RENAME COLUMN plant_short_name TO plant_ticketcode")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    try:
        cursor.execute("ALTER TABLE receipt_data ADD COLUMN IF NOT EXISTS calculated_weight NUMERIC DEFAULT 0")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    # ทะเบียนรถจาก OCR และเที่ยวชั่งที่ล็อกไว้ตอนบันทึก ใช้จับคู่เอกสารกับรอบชั่งเพื่อ cross check น้ำหนัก
    try:
        cursor.execute("ALTER TABLE receipt_data ADD COLUMN IF NOT EXISTS license_plate TEXT")
        cursor.execute("ALTER TABLE receipt_data ADD COLUMN IF NOT EXISTS truck_weighing_key TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_receipt_doc ON receipt_data(document_no);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_receipt_date ON receipt_data(date);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);')
    
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
            elif filename in ["MasterData_Pallet.xlsx", "MasterData_Plant.xlsx"]:
                time.sleep(1)
                load_master_data()

API_KEY = os.getenv("GEMINI_API_KEY")

# ใช้ SDK ใหม่ (google-genai) เพราะตัวเก่า google-generativeai ประกาศหยุดซัพพอร์ตแล้ว
# และที่สำคัญกว่าคือตัวเก่าสั่งระดับการ "คิดก่อนตอบ" ไม่ได้ ซึ่งเป็นตัวแปรหลักของความเร็ว
_gemini_client = genai.Client(api_key=API_KEY) if API_KEY else None

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# ==========================================
# ระบบ OCR แบบหลายชั้น (fallback chain)
# ==========================================
# ถ้าชั้นบนล่ม/โควต้าเต็ม/ค้าง ให้ไล่ลงชั้นถัดไปอัตโนมัติ ถ้าหมดทุกชั้นค่อยให้ผู้ใช้กรอกมือ
# ทุกชั้นต้องอ่านลายมือได้ จึงต้องเป็น vision model ไม่ใช่ OCR แบบเดิม
#
# ตอนนี้เรามีสิทธิ์เฉพาะ Gemini API ทุกชั้นจึงเป็นโมเดล Google ที่ใช้ API key เดิมได้
# rate limit ของ Gemini แยกตามโมเดล ชนโควต้ารุ่นหนึ่งแล้วอีกรุ่นยังใช้ได้
# (วัดแล้วการโดนปฏิเสธเพราะโควต้าใช้เวลาแค่ ~0.4 วินาที ตกชั้นถัดไปจึงแทบไม่กินเวลาผู้ใช้)
#
# ลำดับชั้นมาจากผลวัดจริงด้วย prompt ชุดนี้กับรูปใบส่งกระบะ 4 ใบ:
#   ชั้น 1  gemini-3.6-flash      อ่านเลขที่ชั่งรถถูก 18/18 ครั้ง เร็วสุดเมื่อสั่ง thinking=low (p50 ~5 วินาที)
#   ชั้น 2  gemini-3.5-flash      แม่นใกล้เคียงกัน แต่ช้ากว่าและชนโควต้าง่ายกว่า
#   ชั้น 3  gemini-3.5-flash-lite เร็วสุด (~2 วินาที) แต่แม่นน้อยสุด มักหยิบเลข D/P หรือรหัสลูกค้า
#           มาใส่ช่องเลขที่ชั่งรถ ใช้เป็นทางเลือกสุดท้ายก่อนให้กรอกมือ เพราะได้มาบางส่วนดีกว่าไม่ได้เลย
#   ชั้น 4  Claude เปิดใช้อัตโนมัติเมื่อมี ANTHROPIC_API_KEY เท่านั้น
#           คงโค้ดไว้เพื่อสลับไปใช้ผ่าน Vertex AI / Anthropic ของบริษัทภายหลัง
#
# โมเดลที่ทดสอบแล้วใช้ไม่ได้ อย่าเพิ่งกลับไปใส่โดยไม่ทดสอบใหม่:
#   gemini-2.5-*            404 no longer available to new users
#   gemini-3.1-pro-preview  429 โควต้าไม่พอกับ key ปัจจุบัน
#   gemini-3.7 / 3.8-flash  ช้า 60-90 วินาที เกินเวลาที่ผู้ใช้รอไหว
#   gemini-3.1-flash-lite   อ่านเลขที่ชั่งรถถูกแค่ 2/8 ครั้ง

OCR_MODEL_TIER1 = os.environ.get("OCR_MODEL_TIER1", "gemini-3.6-flash")
OCR_MODEL_TIER2 = os.environ.get("OCR_MODEL_TIER2", "gemini-3.5-flash")
OCR_MODEL_TIER3 = os.environ.get("OCR_MODEL_TIER3", "gemini-3.5-flash-lite")
OCR_MODEL_CLAUDE = os.environ.get("OCR_MODEL_CLAUDE", "claude-sonnet-5")

# ระดับการคิดก่อนตอบ ("low" / "high")
# วัดแล้ว low เร็วกว่าเกือบ 3 เท่า (9.4 -> 3.3 วินาที) โดยความแม่นเท่าเดิม
# เพราะงานนี้คืออ่านค่าจากฟอร์มที่มีโครงสร้างตายตัว ไม่ต้องใช้การให้เหตุผลยาวๆ
OCR_THINKING_LEVEL = os.environ.get("OCR_THINKING_LEVEL", "low")

# ==========================================
# งบเวลา อิงงานวิจัยเรื่องเวลารอของมนุษย์
# ==========================================
# 10 วินาที คือเพดานที่คนยังจดจ่อกับงานตรงหน้าได้ (Miller 1968 / Nielsen)
# เกิน ~12 วินาที ความพึงพอใจตกและเริ่มมีการละทิ้งงาน
# การมีตัวบอกความคืบหน้าช่วยยืดความอดทนได้ราวเท่าตัว (มัธยฐาน 22.6 เทียบกับ 9 วินาที)
# พนักงานหน้างานมีคิวรถรออยู่ข้างหลัง จึงควร "ยอมแพ้แล้วให้กรอกมือ" เร็วกว่าปล่อยให้รอยาว
# เพราะการกรอกมือใช้เวลาแน่นอนกว่าการนั่งรอ AI ที่ไม่รู้ว่าจะได้คำตอบเมื่อไหร่
#
# ชั้นเดียวรอได้ไม่เกินเท่าไหร่ (ปกติชั้น 1 ตอบใน ~5 วินาที ค่านี้เผื่อไว้กันค้าง)
OCR_TIER_TIMEOUT_SECONDS = int(os.environ.get("OCR_TIER_TIMEOUT_SECONDS", "12"))
# ทั้งเชนรวมกันต้องไม่เกินเท่าไหร่ ครบแล้วตัดจบทันทีแล้วให้กรอกมือ
OCR_TOTAL_BUDGET_SECONDS = int(os.environ.get("OCR_TOTAL_BUDGET_SECONDS", "20"))
# เหลือเวลาน้อยกว่านี้ไม่ต้องเริ่มชั้นใหม่ เริ่มไปก็ไม่ทันตอบ เสียเวลาผู้ใช้เปล่าๆ
OCR_MIN_TIER_SECONDS = int(os.environ.get("OCR_MIN_TIER_SECONDS", "4"))

# ล้มติดกันกี่ครั้งถึงจะข้ามชั้นนั้นชั่วคราว กันไม่ให้ทุกคนต้องรอ timeout ซ้ำๆ ตอนผู้ให้บริการล่ม
OCR_CIRCUIT_FAIL_THRESHOLD = int(os.environ.get("OCR_CIRCUIT_FAIL_THRESHOLD", "3"))
OCR_CIRCUIT_COOLDOWN_SECONDS = int(os.environ.get("OCR_CIRCUIT_COOLDOWN_SECONDS", "300"))

# ราคาต่อ 1 ล้าน token (USD) ใช้คิดต้นทุนต่อใบ (ยังไม่ได้ยืนยันกับหน้า pricing ทางการ)
OCR_PRICING = {
    "gemini-3.6-flash":      (0.30, 2.5),
    "gemini-3.5-flash":      (0.30, 2.5),
    "gemini-3.5-flash-lite": (0.30, 2.5),
    "claude-sonnet-5":       (3.00, 15.0),
}

_ocr_circuit = {}


class OcrProviderError(Exception):
    """ผู้ให้บริการชั้นนี้ใช้ไม่ได้ตอนนี้ (โควต้าเต็ม ล่ม ค้าง หรือตอบผิดรูปแบบ) ให้ข้ามไปชั้นถัดไป"""


def _circuit_is_open(name):
    state = _ocr_circuit.get(name)
    return bool(state and state.get("open_until", 0) > time.time())


def _circuit_record(name, ok):
    state = _ocr_circuit.setdefault(name, {"fails": 0, "open_until": 0})
    if ok:
        state["fails"] = 0
        state["open_until"] = 0
        return
    state["fails"] += 1
    if state["fails"] >= OCR_CIRCUIT_FAIL_THRESHOLD:
        state["open_until"] = time.time() + OCR_CIRCUIT_COOLDOWN_SECONDS
        print(f"warn OCR: พัก '{name}' ไว้ {OCR_CIRCUIT_COOLDOWN_SECONDS} วินาที (ล้มติดกัน {state['fails']} ครั้ง)")


def _strip_json_fence(text):
    text = (text or "").strip()
    if text.startswith("```json"):
        return text[7:-3].strip()
    if text.startswith("```"):
        return text[3:-3].strip()
    return text


def _image_to_jpeg_bytes(img):
    # JPEG ไม่รองรับชั้นโปร่งใส ถ้ารูปเป็น RGBA/P จะ save ไม่ผ่านและทำให้ทุกชั้นล้มพร้อมกัน
    if img.mode != "RGB":
        img = img.convert("RGB")
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return buffered.getvalue()


def _call_gemini(model_name, prompt, img, timeout):
    if _gemini_client is None:
        raise OcrProviderError("ยังไม่ได้ตั้งค่า GEMINI_API_KEY")
    try:
        # ต้องกำหนด timeout เสมอ ไม่งั้นถ้าฝั่งโน้นค้าง จะรอตลอดกาลและไม่มีวันได้ใช้ชั้นถัดไป
        response = _gemini_client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(data=_image_to_jpeg_bytes(img), mime_type="image/jpeg"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_level=OCR_THINKING_LEVEL),
                http_options=types.HttpOptions(timeout=int(timeout * 1000)),
            ),
        )
        usage = response.usage_metadata
        # token ที่ใช้คิดก่อนตอบคิดเงินเหมือน output ต้องนับรวม ไม่งั้นต้นทุนที่โชว์จะต่ำกว่าจริง
        in_tokens = usage.prompt_token_count or 0
        out_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return _strip_json_fence(response.text), {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        }
    except Exception as e:
        raise OcrProviderError(f"{type(e).__name__}: {e}") from e


def _call_claude(model_name, prompt, img, timeout):
    if not ANTHROPIC_API_KEY:
        raise OcrProviderError("ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY")
    try:
        import anthropic
    except ImportError as e:
        raise OcrProviderError("ยังไม่ได้ติดตั้งแพ็กเกจ anthropic") from e

    try:
        img_b64 = base64.b64encode(_image_to_jpeg_bytes(img)).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=float(timeout))
        message = client.messages.create(
            model=model_name,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": img_b64
                    }},
                    # Claude ไม่มีโหมดบังคับ JSON เหมือน Gemini จึงต้องย้ำในคำสั่ง
                    {"type": "text", "text": prompt + "\n\nตอบกลับเป็น JSON ล้วนเท่านั้น ห้ามมีคำอธิบายหรือ markdown ใดๆ"}
                ]
            }]
        )
        usage = getattr(message, "usage", None)
        return _strip_json_fence(message.content[0].text), {
            "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
            "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            "total_tokens": (getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)) if usage else 0,
        }
    except OcrProviderError:
        raise
    except Exception as e:
        raise OcrProviderError(f"{type(e).__name__}: {e}") from e


def _gemini_tier(model_name):
    """ผูกชื่อโมเดลไว้กับชั้นนั้นๆ (อย่าใช้ lambda ตรงๆ ใน loop เพราะจะอ้างตัวแปรตัวสุดท้ายหมด)"""
    return lambda prompt, img, timeout: _call_gemini(model_name, prompt, img, timeout)


# เรียงจากแม่นสุดลงไปหาเร็วแต่แม่นน้อยกว่า เพราะกรอกข้อมูลผิดเสียหายกว่ารออีกไม่กี่วินาที
OCR_CHAIN = [
    (OCR_MODEL_TIER1, _gemini_tier(OCR_MODEL_TIER1)),
    (OCR_MODEL_TIER2, _gemini_tier(OCR_MODEL_TIER2)),
    (OCR_MODEL_TIER3, _gemini_tier(OCR_MODEL_TIER3)),
]

# ต่อท้ายชั้น Claude เฉพาะตอนที่มี key จริง ไม่งั้นจะเสียเวลาไล่ชั้นที่ล้มแน่ๆ ทุกครั้ง
if ANTHROPIC_API_KEY:
    OCR_CHAIN.append((OCR_MODEL_CLAUDE, lambda p, i, t: _call_claude(OCR_MODEL_CLAUDE, p, i, t)))


def run_ocr_chain(prompt, img):
    """ไล่เรียกผู้ให้บริการ OCR ทีละชั้นจนกว่าจะได้ JSON ที่ใช้ได้

    คืนค่า (ข้อมูลที่แยกได้, usage, ชื่อชั้นที่ตอบ, รายการความล้มเหลวของแต่ละชั้น)
    ถ้าหมดทุกชั้นแล้วยังไม่ได้ จะคืน extracted เป็น None ให้ผู้ใช้กรอกมือแทน
    """
    failures = []
    chain_started = time.time()

    for name, call in OCR_CHAIN:
        if _circuit_is_open(name):
            failures.append(f"{name}: ถูกพักชั่วคราวเพราะล้มติดกันหลายครั้ง")
            continue

        # เหลือเวลาในงบเท่าไหร่ ถ้าน้อยเกินกว่าจะได้คำตอบทัน ให้เลิกแล้วส่งไปกรอกมือเลย
        # ดีกว่าปล่อยให้คนยืนรอต่อทั้งที่รู้อยู่แล้วว่าไม่ทัน
        remaining = OCR_TOTAL_BUDGET_SECONDS - (time.time() - chain_started)
        if remaining < OCR_MIN_TIER_SECONDS:
            failures.append(f"{name}: ข้ามเพราะใช้เวลารวมเกิน {OCR_TOTAL_BUDGET_SECONDS} วินาทีแล้ว")
            continue
        tier_timeout = min(OCR_TIER_TIMEOUT_SECONDS, remaining)

        started = time.time()
        try:
            raw_text, usage = call(prompt, img, tier_timeout)
            extracted = json.loads(raw_text)
            if not isinstance(extracted, dict):
                raise OcrProviderError("ตอบกลับมาไม่ใช่ JSON object")

            _circuit_record(name, True)
            print(f"✅ OCR สำเร็จด้วย '{name}' ใช้เวลา {time.time() - started:.1f} วินาที")
            return extracted, usage, name, failures

        except (OcrProviderError, json.JSONDecodeError, ValueError, IndexError, AttributeError) as e:
            _circuit_record(name, False)
            # ข้อความ error ของฝั่งผู้ให้บริการยาวเป็นสิบบรรทัด (เช่น 429 ที่แนบ quota ทั้งก้อนมา)
            # ตัดให้สั้นก่อนส่งออก ไม่งั้น log และ response ที่ส่งไปหน้าเว็บจะรกจนอ่านไม่รู้เรื่อง
            detail = " ".join(str(e).split())
            if len(detail) > 200:
                detail = detail[:200] + "..."
            reason = f"{name}: {detail}"
            failures.append(reason)
            print(f"⚠️ OCR ชั้น '{name}' ไม่ผ่าน ({time.time() - started:.1f} วินาที) -> {detail}")

    return None, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, None, failures


app = FastAPI()

# Mount auth router เข้ากับแอปหลัก
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
def startup_event():
    global loop
    loop = asyncio.get_running_loop() 

    if DATABASE_URL:
        init_db()
        # โหลดข้อมูลล่าสุดจากไฟล์ Excel ทับข้อมูลเก่าที่มาจาก backup.sql ทุกครั้งที่แอปสตาร์ท
        load_master_data()
        load_transaction_data()

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
        # ต้องครบ 4 มุมเท่านั้นถึงจะดัดภาพได้ ถ้าไม่ครบให้ใช้ภาพเต็มไปตามเดิมดีกว่าพัง
        if not isinstance(pts, list) or len(pts) != 4:
            pts = None
    else:
        pts = None

    if pts:
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

    # [แก้ไข] เติมลูกน้ำ (,) ต่อท้ายบรรทัด plant_ticketcode
    prompt = """
    คุณคือระบบ Data Entry หน้าที่ของคุณคือการอ่านข้อมูลจากภาพ 'ใบส่งกระบะ' 
    ให้แยกข้อมูลเป็น JSON โดยมีเงื่อนไขรัดกุมดังนี้:
    1. ค้นหา "ตัวเลขลายมือเขียน" ที่ระบุ "จำนวนกระบะที่ส่งคืน" (เน้นพิจารณาส่วนที่ 2 และ 3 หลังคำว่า "ส่งคืน" หรือ "ได้รับกระบะไว้จำนวน ... กระบะ")
    2. ห้ามดึงพิกัดบริเวณ "ลงชื่อ", "ผู้รับ/คืนกระบะ", "ลายเซ็น" หรือ "วันที่" ด้านล่างของกระดาษโดยเด็ดขาด ให้โฟกัสแค่จำนวนกระบะเท่านั้น
    3. ให้คืนค่าพิกัดตำแหน่ง (Bounding Box) ของตัวเลขนั้น ในรูปแบบ [ymin, xmin, ymax, xmax] สเกล 0-1000

    โครงสร้าง JSON ที่ต้องการ (ให้ตอบกลับมาเป็น JSON โครงสร้างนี้เท่านั้น ห้ามตัดคีย์ใดทิ้ง):
    {
    "document_no": "เลขที่เอกสาร (ดูที่มุมขวาบน)",
    "date": "วันที่ออกเอกสาร (อยู่ด้านล่างของเลขที่เอกสาร)",
    "plant_ticketcode": "เลขที่ชั่งรถ (อยู่บริเวณส่วนที่ 1 ถัดจากทะเบียนรถขนส่ง)",
    "license_plate": "ทะเบียนรถขนส่ง (อยู่บริเวณส่วนที่ 1 ติดกับเลขที่ชั่งรถ) ให้ตอบเฉพาะเลขทะเบียน เช่น 70-1234 หรือ 2ฒส-4614 ห้ามใส่ชื่อจังหวัดหรือคำว่าทะเบียน",
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
    }
    }
    """
    
    total_cost_usd, total_cost_thb = 0, 0
    input_tokens, output_tokens, total_tokens = 0, 0, 0
    extracted_data = {}
    crop_img_b64 = ""
    ocr_provider = None

    result_data, usage, ocr_provider, failures = run_ocr_chain(prompt, img)

    if result_data is None:
        # หมดทุกชั้นแล้วยังอ่านไม่ได้ ให้ฝั่งหน้าเว็บเปิดโหมดกรอกมือแทน
        return {
            "success": False,
            "message": "ระบบอ่านข้อมูลจากรูปไม่สำเร็จ กรุณากรอกข้อมูลเอง",
            "allow_manual": True,
            "ocr_provider": None,
            "ocr_failures": failures,
            "data": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0, "cost_thb": 0},
            "images": {"full": full_img_b64, "crop": ""}
        }

    try:
        extracted_data = result_data

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

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        # คิดราคาตามชั้นที่ตอบจริง เพราะแต่ละชั้นราคาไม่เท่ากัน
        price_in, price_out = OCR_PRICING.get(ocr_provider, (0.30, 2.5))
        total_cost_usd = (input_tokens / 1000000) * price_in + (output_tokens / 1000000) * price_out
        total_cost_thb = total_cost_usd * 35

    except Exception as e:
        # อ่านข้อมูลมาได้แล้ว แค่ตอนตัดรูป/คิดราคามีปัญหา ยังใช้ข้อมูลต่อได้
        print(f"❌ OCR post-processing error: {e}")

    print(extracted_data)
    return {
        "success": True if extracted_data else False,
        "message": error_message,
        "ocr_provider": ocr_provider,
        "ocr_failures": failures,
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

    # ต่อท้าย ?v= ด้วยเวลาแก้ไขไฟล์ล่าสุด ไม่งั้นเบราว์เซอร์จะ cache ของเก่าไว้
    # จนแก้โค้ดแล้วหน้าเว็บไม่เปลี่ยนตาม ต้องคอยกด Ctrl+F5 เองทุกครั้ง
    def stamp_asset(match):
        path = match.group(1)
        try:
            version = int(os.path.getmtime(os.path.join("static", os.path.basename(path))))
        except OSError:
            return match.group(0)
        return f"{path}?v={version}"

    html_content = re.sub(r'(/static/[\w.\-]+\.(?:js|css))(?:\?v=[^"\']*)?', stamp_asset, html_content)
    return HTMLResponse(content=html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/upload")
async def upload_image(
    file: UploadFile = File(...), 
    points: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    # ชื่อไฟล์ต้องไม่ซ้ำกันเด็ดขาด ของเดิมใช้ชื่อที่ client ส่งมาตรงๆ ซึ่งซ้ำกันทุกครั้งที่หมุนรูป
    # ถ้ามีคนสแกนพร้อมกัน ไฟล์จะทับกันและคนแรกลบไฟล์ทิ้งขณะที่คนที่สองยังอ่านอยู่
    temp_file_path = os.path.join(tempfile.gettempdir(), f"upload_{uuid.uuid4().hex}.img")
    with open(temp_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        # ต้องรันใน thread แยก ห้ามเรียกตรงๆ ใน async def
        # เพราะการเรียก OCR เป็น blocking call ถ้าค้างจะยึด event loop ไว้ทั้งตัว
        # ทำให้ทุกคนทั้งระบบใช้เว็บไม่ได้ ไม่ใช่แค่คนที่กำลังสแกน
        result = await run_in_threadpool(extract_data_from_image, temp_file_path, points)

        if not result.get("success"):
            return {
                "success": False,
                "message": result.get("message") or "AI ไม่สามารถประมวลผลภาพหรือแยก JSON ได้",
                # บอกหน้าเว็บว่าให้เปิดโหมดกรอกมือแทนการให้ผู้ใช้ติดตาย
                "allow_manual": result.get("allow_manual", False),
                "images": result.get("images", {})
            }

        return {
            "success": True,
            "filename": file.filename,
            "data": result.get("data", {}),
            "images": result.get("images", {}),
            "ocr_provider": result.get("ocr_provider")
        }
    except Exception as e:
        print(f"❌ API Upload Error: {e}")
        return {
            "success": False,
            "message": "เกิดข้อผิดพลาดในระบบ กรุณากรอกข้อมูลเอง",
            "allow_manual": True
        }
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

@app.post("/api/save")
async def save_record(
    record: RecordCreate,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        pallet_details_json = json.dumps([p.dict() for p in record.palletDetails], ensure_ascii=False)

        prefix = record.plant_ticketcode[:2] if record.plant_ticketcode else ""

        total_calculated_weight = 0
        for pallet in record.palletDetails:
            # หาค่าน้ำหนักจากตาราง master_pallet
            cursor.execute('''
                SELECT pallet."weight" 
                FROM master_pallet AS pallet
                INNER JOIN master_plant AS plant ON pallet."plantId" = plant."row_key"
                WHERE TRIM(pallet."name" || ' - ' || pallet."code") = %s
                  AND TRIM(plant."autoWeightTicketPrefix") = %s
                LIMIT 1
            ''', (pallet.name, prefix))
            weight_row = cursor.fetchone()
            
            if weight_row and weight_row[0] is not None:
                try:
                    unit_weight = float(weight_row[0])
                    total_calculated_weight += (unit_weight * pallet.qty)
                except ValueError:
                    pass

        # ล็อกเที่ยวชั่งตั้งแต่ตอนบันทึก เพราะรถคันเดิมเข้าซ้ำหลายเที่ยวต่อวัน (กว่าครึ่ง)
        # ถ้ามาจับคู่ทีหลังด้วยทะเบียน+วันที่จะแยกไม่ออกว่าเป็นเที่ยวไหน
        # ตอน checker บันทึก รถยังอยู่ในโรงงาน (ชั่งเข้าแล้วยังไม่ชั่งออก) จึงมีเที่ยวที่ค้างอยู่เที่ยวเดียว
        clean_plate = (record.license_plate or "").strip().replace(" ", "")
        truck_weighing_key = None
        if clean_plate:
            cursor.execute('''
                SELECT TRIM(SPLIT_PART(CAST("row_key" AS TEXT), '.', 1))
                FROM truck_weighing
                WHERE REPLACE(CAST("carRegister" AS TEXT), ' ', '') = %s
                  AND "weightIn" IS NOT NULL AND "weightIn" > 0
                ORDER BY ("weightOut" IS NOT NULL AND "weightOut" > 0), "weightInTime" DESC
                LIMIT 1
            ''', (clean_plate,))
            key_row = cursor.fetchone()
            if key_row:
                truck_weighing_key = key_row[0]

        # [จุดที่ 4] อัปเดต SQL คำสั่ง UPDATE และ INSERT ให้เป็น plant_ticketcode
        if record.id:
            cursor.execute('''
                UPDATE receipt_data
                SET document_no=%s, date=%s, customer_name=%s, customer_code=%s,
                    expected_qty=%s, actual_qty=%s, checker_name=%s, pallet_details=%s, image_base64=%s, plant_ticketcode=%s, calculated_weight=%s,
                    license_plate=%s, truck_weighing_key=COALESCE(%s, truck_weighing_key)
                WHERE id=%s
            ''', (record.documentNumber, record.date, record.customer_name, record.customer_code, record.expectedQty, record.actualQty, record.checkerName, pallet_details_json, record.imageBase64, record.plant_ticketcode, total_calculated_weight, clean_plate or None, truck_weighing_key, record.id))
        else:
            cursor.execute('''
                INSERT INTO receipt_data (document_no, date, customer_name, customer_code, expected_qty, actual_qty, checker_name, pallet_details, image_base64, plant_ticketcode, calculated_weight, license_plate, truck_weighing_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (record.documentNumber, record.date, record.customer_name, record.customer_code, record.expectedQty, record.actualQty, record.checkerName, pallet_details_json, record.imageBase64, record.plant_ticketcode, total_calculated_weight, clean_plate or None, truck_weighing_key))
        
        conn.commit()
        conn.close()
        await manager.broadcast("UPDATE")
        return {"success": True, "message": "บันทึกข้อมูลสำเร็จ"}
    except Exception as e:
        print(f"❌ API Save Truck Image Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

# ระบบตาชั่งใส่ค่าอัตโนมัติไว้ในช่องชื่อคนขับหลายแบบ ต้องกรองทิ้งให้เหลือเฉพาะชื่อคนจริง
# ไม่งั้นหน้าจอจะขึ้นคำว่า "System Generated" หรือ "nan" ให้ admin อ่านแทนชื่อคนขับ
_JUNK_DRIVER_NAMES = {
    "", "nan", "none", "null", "n/a", "na",
    "system generated", "systemgenerated", "auto-gen", "autogen", "auto gen",
}


def _clean_driver_name(value):
    name = ("" if value is None else str(value)).strip()
    if name.lower() in _JUNK_DRIVER_NAMES:
        return ""
    # เหลือแต่ขีด จุด หรือตัวเลขล้วน ก็ไม่ใช่ชื่อคน
    if re.fullmatch(r"[-._\s]+", name) or re.fullmatch(r"[0-9]+", name):
        return ""
    return name


@app.get("/api/driver-names")
async def get_driver_names(
    license_plate: str = Query(""),
    current_user: dict = Depends(get_current_user)
):
    try:
        clean_plate = license_plate.strip().replace(" ", "")
        if not clean_plate:
            return {"success": True, "data": []}

        conn = get_db_connection()
        cursor = conn.cursor()
        # ตัดค่าที่ระบบตาชั่งใส่มาเอง (System Generated, AUTO-GEN, ขีด, ตัวเลขล้วน) ออก
        # ให้เหลือเฉพาะชื่อคนขับจริง ชื่อเล่นสั้นๆ อย่าง 'สี' 'นพ' ต้องไม่โดนตัด
        cursor.execute('''
            SELECT DISTINCT TRIM(CAST("driverName" AS TEXT)) as driver_name
            FROM truck_weighing
            WHERE REPLACE(CAST("carRegister" AS TEXT), ' ', '') = %s
              AND "driverName" IS NOT NULL
              AND LOWER(TRIM(CAST("driverName" AS TEXT))) NOT IN (
                    '', 'nan', 'none', 'null', 'n/a', 'na',
                    'system generated', 'systemgenerated', 'auto-gen', 'autogen', 'auto gen'
              )
              AND TRIM(CAST("driverName" AS TEXT)) !~ '^[-._[:space:]]+$'
              AND TRIM(CAST("driverName" AS TEXT)) !~ '^[0-9]+$'
            ORDER BY driver_name ASC
        ''', (clean_plate,))
        rows = cursor.fetchall()
        conn.close()
        return {"success": True, "data": [row[0] for row in rows]}
    except Exception as e:
        print(f"❌ API Driver Names Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

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
        driver_name = (payload.driver_name or '').strip() or '-'

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
            INSERT INTO truck_scale_images (license_plate, image_base64, truck_weighing_key, operator_name, created_at, driver_name, pallet_quantity)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (clean_plate, payload.image_base64, truck_row_key, payload.operator_name, created_at, driver_name, payload.pallet_quantity))

        conn.commit()
        conn.close()
        await manager.broadcast("UPDATE")

        return {
            "success": True,
            "message": f"✅ จับคู่และบันทึกรูปรถทะเบียน '{payload.license_plate}' ลงในรอบการชั่งปัจจุบันสำเร็จ!",
            "driver_name": driver_name
        }
    except Exception as e:
        print(f"❌ API Save Truck Image Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

@app.get("/api/truck-scale-history")
async def get_truck_scale_history(
    license_plate: str = Query(""),
    date: str = Query(""),
    status: str = Query(""),
    current_user: dict = Depends(get_current_user)
):
    try:
        clean_plate = license_plate.strip().replace(" ", "")
        clean_date = date.strip()
        clean_status = status.strip()

        # ตรวจรูปแบบก่อนส่งเข้า query แม้ทุก query จะใช้ parameterized อยู่แล้ว
        # เหตุผลคือค่าที่กรอกมั่วๆ จะทำให้ Postgres โยน error ออกมา ซึ่งเป็นวิธีที่คนหาช่องโหว่
        # ใช้สำรวจโครงสร้างฐานข้อมูล การปัดตกตั้งแต่ต้นทางจึงปิดทั้งช่องรั่วและกันหน้าเว็บพังไปด้วย
        if len(clean_plate) > 20 or (clean_plate and not re.match(r'^[A-Za-z0-9ก-๙\-]+$', clean_plate)):
            return {"success": False, "message": "รูปแบบทะเบียนรถไม่ถูกต้อง"}
        if clean_date:
            try:
                datetime.strptime(clean_date, "%Y-%m-%d")
            except ValueError:
                return {"success": False, "message": "รูปแบบวันที่ไม่ถูกต้อง (ต้องเป็น YYYY-MM-DD)"}

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        sql = '''
            SELECT
                truck."weightInTicketCode" as document_ref,
                truck."carRegister" as license_plate,
                truck."weightInTime" as weight_in_time,
                truck."weightIn" as weight_in,
                truck."weightOutTime" as weight_out_time,
                truck."weightOut" as weight_out,
                truck."driverName" as truck_driver_name,
                TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1)) as truck_weighing_key,
                timg.id as image_id,
                timg.driver_name as scale_driver_name,
                -- เอกสารที่ผู้ตรวจนับผูกไว้กับเที่ยวชั่งนี้ ใช้เป็นทางลัดกลับไปดูฝั่งเอกสาร
                doc.document_numbers
            FROM truck_weighing truck
            LEFT JOIN (
                SELECT DISTINCT ON (truck_weighing_key) truck_weighing_key, id, driver_name
                FROM truck_scale_images
                WHERE truck_weighing_key IS NOT NULL
                ORDER BY truck_weighing_key, id DESC
            ) timg ON TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1)) = timg.truck_weighing_key
            LEFT JOIN (
                SELECT TRIM(truck_weighing_key) as truck_weighing_key,
                       ARRAY_AGG(DISTINCT document_no) as document_numbers
                FROM receipt_data
                WHERE NULLIF(TRIM(truck_weighing_key), '') IS NOT NULL
                  AND NULLIF(TRIM(document_no), '') IS NOT NULL
                GROUP BY TRIM(truck_weighing_key)
            ) doc ON doc.truck_weighing_key = TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1))
        '''
        # เงื่อนไขสถานะ ต้องตรงกับตอนคำนวณ status ด้านล่าง เพื่อให้ตัวกรองกับ badge ตรงกันเสมอ
        has_weight_out = 'truck."weightOut" IS NOT NULL AND truck."weightOut" > 0'
        status_condition = {
            "return": f'({has_weight_out} AND truck."weightIn" > truck."weightOut")',
            "pending": f'NOT ({has_weight_out})',
            "pickup": f'({has_weight_out} AND truck."weightIn" <= truck."weightOut")',
        }

        # ตัดแถวที่ระบบตาชั่งสร้างขึ้นเอง ไม่ใช่รถจริง: ทะเบียน AUTO-GEN และแถวที่ไม่มีน้ำหนักชั่งเข้า
        conditions = [
            'TRIM(CAST(truck."carRegister" AS TEXT)) <> \'AUTO-GEN\'',
            'truck."weightIn" IS NOT NULL AND truck."weightIn" > 0',
        ]
        params = []

        # เลือกสถานะมา = กรองตามสถานะนั้นตรงๆ
        # ไม่ได้กรองอะไรเลย = โชว์เฉพาะรายการคืนพาเลท (ชั่งออกแล้วน้ำหนักลดลง หรือถ่ายรูปในระบบแล้วแต่ยังไม่ชั่งออก)
        # ค้นหาทะเบียน/วันที่ = โชว์ทุกเที่ยวของรถคันนั้น เพื่อให้ตรวจสอบย้อนหลังได้ครบ
        if clean_status in status_condition:
            conditions.append(status_condition[clean_status])
        elif not (clean_plate or clean_date):
            conditions.append(
                f'({status_condition["return"]} OR timg.truck_weighing_key IS NOT NULL)'
            )

        if clean_plate:
            conditions.append('REPLACE(CAST(truck."carRegister" AS TEXT), \' \', \'\') = %s')
            params.append(clean_plate)
        if clean_date:
            conditions.append('CAST(truck."weightInTime" AS DATE) = %s')
            params.append(clean_date)

        sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY truck."weightInTime" DESC LIMIT 100'

        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        conn.close()

        records = []
        for row in rows:
            weight_in = float(row["weight_in"]) if row["weight_in"] is not None else 0
            has_weight_out = row["weight_out"] is not None and float(row["weight_out"]) > 0
            weight_out = float(row["weight_out"]) if has_weight_out else 0

            if not has_weight_out:
                status = "pending"      # ถ่ายรูปแล้ว รถยังอยู่ในโรงงาน รอชั่งออก
            elif weight_in > weight_out:
                status = "return"       # ออกเบากว่าเข้า = คืนพาเลท
            else:
                status = "pickup"       # ออกหนักกว่าเข้า = มารับของ ไม่ใช่คืนพาเลท

            records.append({
                "documentRef": row["document_ref"] or "-",
                "licensePlate": row["license_plate"] or "-",
                "weightInTime": str(row["weight_in_time"]) if row["weight_in_time"] else "-",
                "weightIn": weight_in,
                "weightOutTime": str(row["weight_out_time"]) if has_weight_out and row["weight_out_time"] else "รอรถออก",
                "weightOut": weight_out if has_weight_out else "รอรถออก",
                # ใช้ค่าสัมบูรณ์ ให้ badge สถานะเป็นตัวบอกความหมายแทนเครื่องหมายลบ
                "netWeight": abs(weight_in - weight_out) if has_weight_out else "รอรถออก",
                "status": status,
                # ส่งแค่ id ไม่ส่ง base64 มาทั้งก้อน ไม่งั้น 100 แถวจะหนักหลาย MB
                "imageId": row["image_id"],
                "driverName": _clean_driver_name(row["scale_driver_name"])
                              or _clean_driver_name(row["truck_driver_name"]) or "-",
                "truckWeighingKey": row["truck_weighing_key"] or None,
                "documentNumbers": list(row["document_numbers"] or []),
            })
        return {"success": True, "data": records}
    except Exception as e:
        print(f"❌ API Truck Scale History Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

@app.get("/api/linked-trip/{truck_weighing_key}")
async def get_linked_trip(
    truck_weighing_key: str,
    current_user: dict = Depends(get_current_user)
):
    """รวมข้อมูลสองฝั่งของเที่ยวชั่งเดียวกันไว้ที่เดียว

    ใช้ตอน admin เจอใบที่จำนวนพาเลทไม่ตรง แล้วอยากรู้ว่ามาจากรถคันไหน คนขับเป็นใคร เข้า-ออกกี่โมง
    และในทางกลับกัน ห้องชั่งเห็นเที่ยวชั่งแล้วอยากรู้ว่าผูกกับใบส่งกระบะใบไหน
    """
    # ข้อมูลฝั่งโรงงาน ให้เห็นเฉพาะ admin กับห้องชั่ง เหมือนผลเทียบน้ำหนัก
    if current_user.get("role") not in ("ADMIN", "SCALE"):
        return {"success": False, "message": "ไม่มีสิทธิ์ดูข้อมูลส่วนนี้"}

    key = (truck_weighing_key or "").strip()
    # คีย์เป็น uuid ที่มาจากระบบตาชั่ง ปัดค่าที่ผิดรูปแบบตั้งแต่ต้นทาง
    # กันไม่ให้ค่ามั่วๆ ไปทำให้ Postgres โยน error ซึ่งเป็นช่องให้คนสำรวจโครงสร้างฐานข้อมูล
    if not key or len(key) > 64 or not re.match(r'^[A-Za-z0-9\-]+$', key):
        return {"success": False, "message": "รหัสเที่ยวชั่งไม่ถูกต้อง"}

    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cursor.execute('''
            SELECT truck."carRegister" as license_plate,
                   truck."driverName" as truck_driver_name,
                   truck."weightInTicketCode" as weight_in_ticket,
                   truck."weightInTime" as weight_in_time,
                   truck."weightIn" as weight_in,
                   truck."weightInStation" as weight_in_station,
                   truck."weightOutTicketCode" as weight_out_ticket,
                   truck."weightOutTime" as weight_out_time,
                   truck."weightOut" as weight_out,
                   plant."name" as plant_name
            FROM truck_weighing truck
            LEFT JOIN master_plant plant
                   ON LOWER(TRIM(CAST(plant."row_key" AS TEXT))) = LOWER(TRIM(CAST(truck."weightInPlant" AS TEXT)))
            WHERE TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1)) = %s
            LIMIT 1
        ''', (key,))
        truck = cursor.fetchone()

        cursor.execute('''
            SELECT id, driver_name, operator_name, pallet_quantity, created_at
            FROM truck_scale_images
            WHERE TRIM(truck_weighing_key) = %s
            ORDER BY id DESC
            LIMIT 1
        ''', (key,))
        scale_image = cursor.fetchone()

        # ปกติหนึ่งเที่ยวชั่งผูกกับใบเดียว แต่เปิดรับหลายใบไว้ เพราะข้อมูลจริงเคยมีใบซ้ำเลขเดียวกัน
        cursor.execute('''
            SELECT DISTINCT ON (document_no)
                   id, document_no, date, customer_name, customer_code,
                   expected_qty, actual_qty, checker_name, calculated_weight,
                   (image_base64 IS NOT NULL AND image_base64 <> '') as has_image
            FROM receipt_data
            WHERE TRIM(truck_weighing_key) = %s
            ORDER BY document_no, id DESC
        ''', (key,))
        documents = cursor.fetchall()
        conn.close()

        if not truck and not documents:
            return {"success": False, "message": "ไม่พบข้อมูลเที่ยวชั่งนี้"}

        truck_info = None
        if truck:
            weight_in = float(truck["weight_in"]) if truck["weight_in"] is not None else 0
            has_weight_out = truck["weight_out"] is not None and float(truck["weight_out"]) > 0
            weight_out = float(truck["weight_out"]) if has_weight_out else 0
            truck_info = {
                "licensePlate": truck["license_plate"] or "-",
                "driverName": _clean_driver_name(scale_image["driver_name"] if scale_image else None)
                              or _clean_driver_name(truck["truck_driver_name"]) or "-",
                "plantName": truck["plant_name"] or "-",
                "weightInStation": truck["weight_in_station"] or "-",
                "weightInTicket": truck["weight_in_ticket"] or "-",
                "weightInTime": str(truck["weight_in_time"]) if truck["weight_in_time"] else "-",
                "weightIn": weight_in,
                "weightOutTicket": truck["weight_out_ticket"] or "-",
                "weightOutTime": str(truck["weight_out_time"]) if has_weight_out and truck["weight_out_time"] else "รอรถออก",
                "weightOut": weight_out if has_weight_out else None,
                # น้ำหนักพาเลทที่คืน = ส่วนที่หายไปตอนชั่งออก คิดได้ต่อเมื่อชั่งออกแล้วเท่านั้น
                "netWeight": (weight_in - weight_out) if has_weight_out else None,
            }

        return {
            "success": True,
            "truckWeighingKey": key,
            "truck": truck_info,
            "scaleImage": {
                "imageId": scale_image["id"],
                "driverName": _clean_driver_name(scale_image["driver_name"]) or "-",
                "operatorName": scale_image["operator_name"] or "-",
                "palletQuantity": scale_image["pallet_quantity"],
                "createdAt": scale_image["created_at"] or "-",
            } if scale_image else None,
            "documents": [{
                "id": d["id"],
                "documentNumber": d["document_no"],
                "date": d["date"],
                "customerName": d["customer_name"],
                "customerCode": d["customer_code"],
                "expectedQty": d["expected_qty"],
                "actualQty": d["actual_qty"],
                "checkerName": d["checker_name"],
                "calculatedWeight": float(d["calculated_weight"]) if d["calculated_weight"] is not None else 0,
                "hasImage": d["has_image"],
            } for d in documents],
        }
    except Exception as e:
        print(f"❌ API Linked Trip Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}


@app.get("/api/receipt-image/{record_id}")
async def get_receipt_image(
    record_id: int,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT image_base64 FROM receipt_data WHERE id = %s', (record_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return {"success": False, "message": "ไม่พบรูปภาพ"}
        return {"success": True, "image_base64": row[0]}
    except Exception as e:
        print(f"❌ API Receipt Image Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

@app.get("/api/truck-scale-image/{image_id}")
async def get_truck_scale_image(
    image_id: int,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT image_base64 FROM truck_scale_images WHERE id = %s', (image_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {"success": False, "message": "ไม่พบรูปภาพ"}
        return {"success": True, "image_base64": row[0]}
    except Exception as e:
        print(f"❌ API Truck Scale Image Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

@app.get("/api/records")
async def get_records(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        cursor.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (r.document_no)
                       r.id, r.document_no, r.date, r.customer_name, r.customer_code,
                       r.expected_qty, r.actual_qty, r.checker_name, r.pallet_details,
                       r.plant_ticketcode, r.calculated_weight,
                       -- ไม่ดึง r.image_base64 มาทั้งก้อน ส่งแค่ว่ามีรูปไหม แล้วค่อยโหลดตอนกดดู
                       (r.image_base64 IS NOT NULL AND r.image_base64 <> '') as has_image,
                       r.license_plate as receipt_license_plate,
                       r.truck_weighing_key,
                       truck."carRegister" as license_plate,
                       truck."driverName" as truck_driver_name,
                       -- ชื่อคนขับที่พนักงานตาชั่งพิมพ์ตอนถ่ายรูป มักครบกว่าที่ระบบตาชั่งบันทึกไว้เอง
                       timg.driver_name as scale_driver_name,
                       timg.id as scale_image_id,
                       truck."weightInTime" as weight_in_time,
                       truck."weightIn" as weight_in,
                       truck."weightOutTime" as weight_out_time,
                       truck."weightOut" as weight_out,
                       plant.plant_name as plant_name,
                       -- น้ำหนักสุทธิฝั่งตาชั่ง คิดเฉพาะเที่ยวที่ชั่งออกแล้วเท่านั้น ถ้ายังไม่ชั่งออกให้เป็น NULL
                       CASE WHEN truck."weightIn" IS NOT NULL AND truck."weightIn" > 0
                                 AND truck."weightOut" IS NOT NULL AND truck."weightOut" > 0
                            THEN truck."weightIn" - truck."weightOut"
                       END as scale_net_weight,
                       COALESCE(CAST(NULLIF(CAST(truck."weightIn" AS TEXT), '') AS NUMERIC), 0) - COALESCE(CAST(NULLIF(CAST(truck."weightOut" AS TEXT), '') AS NUMERIC), 0) as pallet_weight
                FROM receipt_data r

                -- แปลงรหัสที่ได้จาก OCR เป็นชื่อโรงงานจริง โดยเทียบ 2 ตัวแรกกับ autoWeightTicketPrefix
                -- ใช้ DISTINCT ON เพราะ master_plant มี prefix ซ้ำ (เช่น SK) ถ้า join ตรงๆ แถวจะบานปลาย
                LEFT JOIN (
                    SELECT DISTINCT ON (UPPER(TRIM("autoWeightTicketPrefix")))
                           UPPER(TRIM("autoWeightTicketPrefix")) as prefix,
                           "name" as plant_name
                    FROM master_plant
                    WHERE "autoWeightTicketPrefix" IS NOT NULL
                      AND TRIM("autoWeightTicketPrefix") <> ''
                    ORDER BY UPPER(TRIM("autoWeightTicketPrefix")), "row_key"
                ) plant ON plant.prefix = UPPER(LEFT(TRIM(CAST(r.plant_ticketcode AS TEXT)), 2))

                -- จับคู่ด้วยเที่ยวชั่งที่ล็อกไว้ตอนบันทึกเท่านั้น (มาจากทะเบียนรถที่ OCR อ่านได้)
                -- ไม่ใช้เลขที่เอกสารไป map ผ่าน pallet_delivery เพราะเอกสารกับรอบชั่งไม่ได้ผูกกันตรงๆ
                LEFT JOIN truck_weighing truck
                       ON TRIM(SPLIT_PART(CAST(truck."row_key" AS TEXT), '.', 1)) = NULLIF(TRIM(r.truck_weighing_key), '')
                      AND LOWER(CAST(truck."row_key" AS TEXT)) NOT IN ('', 'nan', 'none', 'null')

                -- รูปรถที่ห้องชั่งถ่ายไว้ในเที่ยวเดียวกัน ใช้เป็นหลักฐานประกอบตอน admin ตรวจย้อนหลัง
                LEFT JOIN (
                    SELECT DISTINCT ON (truck_weighing_key) truck_weighing_key, id, driver_name
                    FROM truck_scale_images
                    WHERE truck_weighing_key IS NOT NULL
                    ORDER BY truck_weighing_key, id DESC
                ) timg ON timg.truck_weighing_key = NULLIF(TRIM(r.truck_weighing_key), '')

                ORDER BY r.document_no, r.id DESC
            ) AS unique_records
            ORDER BY id DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(DISTINCT document_no) FROM receipt_data")
        total_count = cursor.fetchone()[0]
        conn.close()

        # ผลเทียบน้ำหนักเป็นข้อมูลของฝั่งโรงงาน ให้เห็นเฉพาะ admin กับห้องชั่ง
        can_see_cross_check = current_user.get("role") in ("ADMIN", "SCALE")

        records = []
        for row in rows:
            record = {
                "id": row["id"],
                "documentNumber": row["document_no"],
                "date": row["date"],
                "customer_name": row["customer_name"],
                "customer_code": row["customer_code"],
                "expectedQty": row["expected_qty"],
                "actualQty": row["actual_qty"],
                "checkerName": row["checker_name"],
                "palletDetails": json.loads(row["pallet_details"]) if row["pallet_details"] else [],
                # ส่งแค่ธงบอกว่ามีรูป รูปจริงโหลดผ่าน /api/receipt-image/{id} ตอนกดดู
                "hasImage": row["has_image"],
                # [จุดที่ 5] ส่งค่า plant_ticketcode คืนให้ Frontend แทนของเดิม
                "plant_ticketcode": row["plant_ticketcode"],
                # ชื่อโรงงานจริงที่แปลงมาจาก prefix 2 ตัวแรกของรหัส ถ้าเทียบไม่เจอค่อยโชว์รหัสดิบแทน
                "plant_name": row["plant_name"] or row["plant_ticketcode"] or "-",
                "calculated_weight": row["calculated_weight"] or 0,
                "truckDetail": {
                    # ทะเบียนจากใบตาชั่ง ถ้ายังจับคู่เที่ยวชั่งไม่ได้ให้ใช้ทะเบียนที่ OCR อ่านจากเอกสารแทน
                    "license_plate": row["license_plate"] or row["receipt_license_plate"] or "-",
                    "driver_name": _clean_driver_name(row["scale_driver_name"])
                                   or _clean_driver_name(row["truck_driver_name"]) or "-",
                    "weight_in_time": row["weight_in_time"] or "-",
                    "weight_in": row["weight_in"] or 0,
                    "weight_out_time": row["weight_out_time"] or "รอรถออก",
                    "weight_out": row["weight_out"] or 0,
                    "pallet_weight": row["pallet_weight"] or 0
                }
            }

            if can_see_cross_check:
                # คีย์สำหรับเปิดดูรายละเอียดเที่ยวชั่งที่ผูกกับเอกสารใบนี้
                # ส่งเฉพาะ admin/ห้องชั่ง เพราะเป็นข้อมูลฝั่งโรงงาน คนตรวจนับหน้างานไม่ต้องเห็น
                record["truckLink"] = {
                    "truckWeighingKey": (row["truck_weighing_key"] or "").strip() or None,
                    "scaleImageId": row["scale_image_id"],
                }

                scale_net = row["scale_net_weight"]
                calculated = row["calculated_weight"]
                record["crossCheck"] = {
                    "scaleNetWeight": float(scale_net) if scale_net is not None else None,
                    "calculatedWeight": float(calculated) if calculated is not None else 0,
                    # ผลต่างคำนวณได้ต่อเมื่อรถชั่งออกแล้วเท่านั้น ก่อนหน้านั้นยังไม่มีน้ำหนักฝั่งตาชั่งให้เทียบ
                    "difference": float(scale_net) - float(calculated or 0) if scale_net is not None else None
                }

            records.append(record)
        return {"success": True, "data": records, "total": total_count}
    except Exception as e:
        print(f"❌ API Records Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}
        
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
        print(f"❌ API Clear Database Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

# [จุดที่ 6] อัปเดต Endpoint และพารามิเตอร์เป็น plant_ticketcode
@app.get("/api/pallets/{plant_ticketcode}")
async def get_pallets_by_plant(
    plant_ticketcode: str,
    current_user: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        prefix = plant_ticketcode[:2]
        
        sql = """
            SELECT pallet."name" || ' - ' || pallet."code" 
            FROM master_pallet as pallet
            INNER JOIN master_plant as plant ON pallet."plantId" = plant."row_key"
            WHERE TRIM(plant."autoWeightTicketPrefix") = %s
        """
        cursor.execute(sql, (prefix,))
        rows = cursor.fetchall()
        conn.close()
        
        if rows:
            pallets = [str(row[0]).strip() for row in rows if row[0] is not None and str(row[0]).strip() != ""]
            return {"success": True, "pallets": pallets}
        else:
            return {"success": True, "pallets": []}
            
    except Exception as e:
        print(f"❌ API Pallets Error: {e}") 
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}

@app.get("/api/manual-reload")
async def manual_reload(current_user: dict = Depends(get_current_user)):
    try:
        load_master_data()
        load_transaction_data()
        return {"success": True, "message": "อัปเดตข้อมูลจาก Excel ลง PostgreSQL สำเร็จแล้ว!"}
    except Exception as e:
        print(f"❌ API Pallet Master Error: {e}")
        return {"success": False, "message": "เกิดข้อผิดพลาดในระบบ กรุณาลองใหม่อีกครั้ง"}