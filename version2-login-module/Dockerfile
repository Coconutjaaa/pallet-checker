# ใช้ Python เวอร์ชันที่เป็นทางการ
FROM python:3.11-slim

# กำหนดโฟลเดอร์ทำงานหลักข้างใน Container
WORKDIR /app

# คัดลอกไฟล์ requirements.txt เข้าไปก่อน เพื่อให้ Docker ทำ Cache เลเยอร์ได้ดีขึ้น
COPY requirements.txt .

# ติดตั้งไลบรารีทั้งหมดที่ระบุใน requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอกโค้ดและโฟลเดอร์อื่นๆ ทั้งหมดในโปรเจกต์เข้าไปใน Container
COPY . .

# เปิดพอร์ต 8000 สำหรับ FastAPI
EXPOSE 8000

# คำสั่งสำหรับรันเว็บแอปพลิเคชันเมื่อ Container เริ่มทำงาน
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]