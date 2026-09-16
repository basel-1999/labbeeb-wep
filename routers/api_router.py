from fastapi import APIRouter, UploadFile, File, HTTPException, Form
import requests
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

@router.post("/api/upload-to-cloudinary")
async def upload_to_cloudinary(file: UploadFile = File(...), folder: str = Form(...)):
    """رفع الملفات (الشهادات، الهويات) إلى Cloudinary باستخدام Unsigned Upload"""
    try:
        cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "f4t8ayoq")
        upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET", "lzgw58tq")
        
        file_contents = await file.read()
        
        # استخدام auto ليتعرف على نوع الملف تلقائياً (صورة أو PDF)
        url = f"https://api.cloudinary.com/v1_1/{cloud_name}/auto/upload"
        
        files = {
            'file': (file.filename, file_contents, file.content_type)
        }
        data = {
            'upload_preset': upload_preset,
        }
        if folder:
            data['folder'] = folder
            
        response = requests.post(url, files=files, data=data)
        
        if response.status_code == 200:
            result = response.json()
            return {"url": result.get("secure_url")}
        else:
            print("Cloudinary Error Response:", response.text)
            raise HTTPException(status_code=500, detail="فشل رفع الملف إلى Cloudinary.")
            
    except Exception as e:
        print("Upload Exception:", str(e))
        raise HTTPException(status_code=500, detail=f"Cloudinary upload exception: {str(e)}")


# ==========================================
# 💳 مسار الموافقة على طلب الشحن (Fix 404 Error)
# ==========================================
@router.post("/api/admin/approve-recharge/{request_id}")
async def approve_recharge(request_id: str):
    """
    الموافقة على طلب الشحن وتحديث رصيد الطالب في قاعدة البيانات
    """
    try:
        # 1. اكتب منطق تحديث قاعدة البيانات هنا (Firebase أو Database الخاصة بك)
        # مثال:
        # update_recharge_status(request_id, status="approved")
        
        print(f"تمت الموافقة على طلب الشحن رقم: {request_id}")
        
        return {
            "status": "success",
            "message": f"تمت الموافقة على طلب الشحن {request_id} بنجاح"
        }
    except Exception as e:
        print("Error approving recharge:", str(e))
        raise HTTPException(status_code=500, detail=f"فشلت العملية: {str(e)}")