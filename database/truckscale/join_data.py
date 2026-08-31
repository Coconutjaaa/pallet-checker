import pandas as pd
import os
import sqlite3

excel_files = [
    "MasterData_Pallet.xlsx",
    "MasterData_Plant.xlsx",
    "PalletControl_PalletDelivery.xlsx",
    "Weighing_TruckWeighing.xlsx"
]

for file in excel_files:
    if os.path.exists(file):
        # อ่านข้อมูลบรรทัดแรกๆ มาเพื่อดูโครงสร้าง (ใช้ nrows=0 เพื่อความรวดเร็ว โหลดแค่ชื่อคอลัมน์)
        df = pd.read_excel(file, nrows=0)
        
        print(f"=== คอลัมน์ในไฟล์: {file} ===")
        # ปริ้นต์ชื่อคอลัมน์ออกมาทีละบรรทัดเพื่อให้ดูง่าย
        for col in df.columns:
            print(f" - {col}")
        print("-" * 40 + "\n")
    else:
        print(f"ไม่พบไฟล์: {file}\n")

print("กำลังโหลดไฟล์ Excel...")
df_pallet = pd.read_excel("MasterData_Pallet.xlsx")
df_plant = pd.read_excel("MasterData_Plant.xlsx")
df_delivery = pd.read_excel("PalletControl_PalletDelivery.xlsx")
df_weighing = pd.read_excel("Weighing_TruckWeighing.xlsx")

# 2. สร้างฐานข้อมูล SQLite จำลองในหน่วยความจำ (ไม่ต้องสร้างไฟล์ฐานข้อมูลจริง)
conn = sqlite3.connect(':memory:')

# 3. นำ DataFrame ทั้ง 4 ไปสร้างเป็น Table ใน SQL
print("กำลังสร้างตารางใน SQL...")
df_pallet.to_sql('master_pallet', conn, index=False)
df_plant.to_sql('master_plant', conn, index=False)
df_delivery.to_sql('pallet_delivery', conn, index=False)
df_weighing.to_sql('truck_weighing', conn, index=False)

# 4. >>> เขียนคำสั่ง SQL ของคุณตรงนี้ <<<
sql_query1 = """
    SELECT 
        pallet.code,            
        pallet.name,
        pallet.type,
        pallet.weight,
        plant.shortName,
        plant.name

    FROM master_pallet as pallet

    LEFT JOIN master_plant as plant
        ON pallet.plantId = plant.row_key
"""

# 5. รันคำสั่ง SQL และดึงผลลัพธ์ออกมา
print("กำลังรันคำสั่ง SQL...")
result_df = pd.read_sql_query(sql_query1, conn)

# 6. บันทึกผลลัพธ์เป็นไฟล์ Excel ไฟล์ใหม่ พร้อมเปิดใช้งาน Filter ที่หัวตาราง
output_filename = "Joined_Result_With_Filter.xlsx"

# ใช้ ExcelWriter เพื่อให้สามารถเข้าถึงฟีเจอร์ต่างๆ ของ Excel ได้
with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
    # นำข้อมูลลงไปเขียนใน Sheet ที่ชื่อว่า 'Result'
    result_df.to_excel(writer, index=False, sheet_name='Result')
    
    # ดึงออบเจ็กต์ของ Worksheet นั้นมา
    worksheet = writer.sheets['Result']
    
    # สั่งเปิดใช้งาน AutoFilter โดยให้ครอบคลุมพื้นที่ที่มีข้อมูลทั้งหมด (worksheet.dimensions)
    worksheet.auto_filter.ref = worksheet.dimensions

print(f"เสร็จสิ้น! บันทึกไฟล์ใหม่และใส่ Filter เรียบร้อย ชื่อไฟล์: {output_filename}")


sql_query2 = """
    SELECT 
        delivery.receiptNumber,
        truck.carRegister,
        truck.driverName,
        truck.weightInTime,
        truck.weightIn,
        truck.weightOutTime,
        truck.weightOut

    FROM pallet_delivery as delivery

    LEFT JOIN truck_weighing as truck
        ON delivery.truckWeighingKey = truck.row_key
"""
# 5. รันคำสั่ง SQL และดึงผลลัพธ์ออกมา
print("กำลังรันคำสั่ง SQL...")
result_df = pd.read_sql_query(sql_query2, conn)

# 6. บันทึกผลลัพธ์เป็นไฟล์ Excel ไฟล์ใหม่ พร้อมเปิดใช้งาน Filter ที่หัวตาราง
output_filename = "Joined_Result_With_Filter_sql2.xlsx"

# ใช้ ExcelWriter เพื่อให้สามารถเข้าถึงฟีเจอร์ต่างๆ ของ Excel ได้
with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
    # นำข้อมูลลงไปเขียนใน Sheet ที่ชื่อว่า 'Result'
    result_df.to_excel(writer, index=False, sheet_name='Result')
    
    # ดึงออบเจ็กต์ของ Worksheet นั้นมา
    worksheet = writer.sheets['Result']
    
    # สั่งเปิดใช้งาน AutoFilter โดยให้ครอบคลุมพื้นที่ที่มีข้อมูลทั้งหมด (worksheet.dimensions)
    worksheet.auto_filter.ref = worksheet.dimensions

print(f"เสร็จสิ้น! บันทึกไฟล์ใหม่และใส่ Filter เรียบร้อย ชื่อไฟล์: {output_filename}")