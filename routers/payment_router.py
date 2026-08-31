from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

class PaymentRequest(BaseModel):
    amount: float
    credits: int
    userId: str
    customerName: str
    customerEmail: str

@router.post("/api/payment/create-tap-charge")
async def create_tap_charge(req: PaymentRequest):
    tap_secret_key = os.getenv("TAP_SECRET_KEY")
    
    if not tap_secret_key:
        raise HTTPException(status_code=500, detail="لم يتم العثور على مفتاح تاب (TAP_SECRET_KEY) في ملف .env")
    
    tap_url = "https://api.tap.company/v2/charges"
    headers = {
        "Authorization": f"Bearer {tap_secret_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "amount": req.amount,
        "currency": "SAR", # قم بتغيير العملة إذا لزم الأمر (مثلاً KWD أو AED)
        "threeDSecure": True,
        "save_card": False,
        "description": f"شحن {req.credits} حصة لمنصة لبيب",
        "metadata": {
            "userId": req.userId,
            "credits": req.credits
        },
        "customer": {
            "first_name": req.customerName,
            "email": req.customerEmail
        },
        "source": {"id": "src_all"},
        "post": {"url": "http://localhost:8000/api/payment/webhook"}, 
        # رابط العودة بعد نجاح الدفع:
        "redirect": {"url": "http://localhost:8000/payment-success"} 
    }

    try:
        response = requests.post(tap_url, json=payload, headers=headers)
        data = response.json()

        if response.status_code == 200 and "transaction" in data:
            transaction_url = data["transaction"]["url"]
            return {"transactionUrl": transaction_url}
        else:
            print("Tap API Error:", data)
            error_msg = data.get("errors", [{}])[0].get("description", "فشل إنشاء طلب الدفع")
            raise HTTPException(status_code=400, detail=error_msg)
            
    except Exception as e:
        print("Payment Exception:", str(e))
        raise HTTPException(status_code=500, detail=f"خطأ في الاتصال ببوابة الدفع: {str(e)}")