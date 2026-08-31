import os
from fastapi import FastAPI, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# استيراد راوترات الـ API
from routers.api_router import router as api_router
from routers.session_router import router as session_router

from routers.payment_router import router as payment_router

# ==========================================
# 🚀 تهيئة تطبيق FastAPI
# ==========================================
app = FastAPI(title="منصة لبيب التعليمية")

# إعداد المجلدات (للتأكد من وجودها عند أول تشغيل)
os.makedirs("static", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
os.makedirs("static/css", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("routers", exist_ok=True)

# إعداد الملفات الثابتة والقوالب
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# تضمين راوترات الـ API (الخاصة بعمليات الباك-ند)
app.include_router(api_router)
app.include_router(session_router)
app.include_router(payment_router)

# ==========================================
# 🔒 نظام الحماية والتوجيه (Redirect Logic)
# ==========================================
async def auth_guard(request: Request):
    """
    محاكاة لـ GoRouter redirect:
    إذا لم يكن مسجلاً ويحاول الدخول لصفحة محمية، أرجعه للرئيسية
    """
    auth_token = request.cookies.get("labeeb_auth_token")
    is_logged_in = auth_token is not None
    
    path = request.url.path
    is_auth_route = path in ["/", "/auth"]

    if not is_logged_in and not is_auth_route:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    return None



# ==========================================
# 🛣️ المسارات (Routes) - مطابقة لـ GoRouter
# ==========================================

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def main_gate(request: Request):
    """1️⃣ البوابة الرئيسية"""
    return templates.TemplateResponse(request=request, name="main_gate.html")

@app.get("/auth", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def auth_flow(request: Request, role: str = "student"):
    """2️⃣ تدفق التسجيل والتحقق"""
    return templates.TemplateResponse(request=request, name="auth_flow.html", context={"role": role})

@app.get("/teacher-dashboard", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def teacher_dashboard(request: Request, name: str = "أ. باسل أبو هدة"):
    """3️⃣ لوحة تحكم المعلم"""
    return templates.TemplateResponse(request=request, name="teacher_dashboard.html", context={"teacher_name": name})

@app.get("/student-dashboard", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def student_dashboard(request: Request, name: str = "طالب لبيب", subject: str = "الرياضيات", type: str = "مباشر"):
    """4️⃣ لوحة تحكم الطالب"""
    return templates.TemplateResponse(request=request, name="student_dashboard.html", context={
        "student_name": name,
        "selected_subject": subject,
        "booking_type": type
    })

@app.get("/live-session", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def live_session(request: Request, sessionId: str = "unknown_session", role: str = "student", studentName: str = "طالب"):
    """5️⃣ مسار غرفة البث الحية"""
    is_teacher = False
    display_name = studentName

    if role == "admin":
        display_name = "مراقبة الأدمن"
        is_teacher = False
    elif role == "teacher":
        is_teacher = True
        display_name = studentName

    return templates.TemplateResponse(request=request, name="live_session.html", context={
        "session_id": sessionId,
        "role": role,
        "student_name": display_name,
        "is_teacher": is_teacher
    })


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success(request: Request):
    """صفحة نجاح الدفع - تظهر بعد إعادة التوجيه من بوابة Tap"""
    html_content = """
    <!DOCTYPE html>
    <html lang="ar" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>تم الدفع بنجاح - منصة لبيب</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet">
        <script>
            tailwind.config = {
                theme: {
                    extend: {
                        colors: {
                            oliveGreen: '#4E6B45',
                            beigeBg: '#F4EFE6',
                            beigeCard: '#FAF7F2',
                            textDark: '#1E241B',
                            accentOrange: '#D97706',
                        },
                        fontFamily: { cairo: ['Cairo', 'sans-serif'] }
                    }
                }
            }
        </script>
        <style>
            body { font-family: 'Cairo', sans-serif; background-color: #F4EFE6; }
            .checkmark {
                width: 80px;
                height: 80px;
                border-radius: 50%;
                display: block;
                stroke-width: 3;
                stroke: #4E6B45;
                stroke-miterlimit: 10;
                margin: 0 auto;
                box-shadow: inset 0px 0px 0px #4E6B45;
                animation: fill .4s ease-in-out .4s forwards, scale .3s ease-in-out .9s both;
            }
            .checkmark__circle {
                stroke-dasharray: 166;
                stroke-dashoffset: 166;
                stroke-width: 3;
                stroke-miterlimit: 10;
                stroke: #4E6B45;
                fill: none;
                animation: stroke .6s cubic-bezier(0.65, 0, 0.45, 1) forwards;
            }
            .checkmark__check {
                transform-origin: 50% 50%;
                stroke-dasharray: 48;
                stroke-dashoffset: 48;
                animation: stroke .3s cubic-bezier(0.65, 0, 0.45, 1) .8s forwards;
            }
            @keyframes stroke {100% {stroke-dashoffset: 0;}}
            @keyframes scale {0%, 100% {transform: none;} 50% {transform: scale3d(1.1, 1.1, 1);}}
            @keyframes fill {100% {box-shadow: inset 0px 0px 0px 40px #4E6B45;}}
        </style>
    </head>
    <body class="flex items-center justify-center min-h-screen">
        <div class="bg-white rounded-3xl shadow-xl p-10 max-w-md w-full text-center">
            <svg class="checkmark" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 52 52">
                <circle class="checkmark__circle" cx="26" cy="26" r="25" fill="none"/>
                <path class="checkmark__check" fill="none" d="M14.1 27.2l7.1 7.2 16.7-16.8"/>
            </svg>
            <h1 class="text-2xl font-bold text-oliveGreen mt-6 mb-3">تمت عملية الدفع بنجاح!</h1>
            <p class="text-gray-500 text-sm mb-8">تم شحن رصيدك في محفظتك بنجاح. يمكنك الآن طلب حصصك الدراسية.</p>
            <a href="/student-dashboard" class="bg-oliveGreen text-white font-bold py-3 px-8 rounded-xl hover:bg-oliveLight transition inline-block">
                العودة للوحة التحكم
            </a>
        </div>
        <script>
            // تحويل الطالب تلقائياً للوحة التحكم بعد 5 ثوانٍ
            setTimeout(() => {
                window.location.href = '/student-dashboard';
            }, 5000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)



# ==========================================
# ▶️ تشغيل السيرفر
# ==========================================

@app.get("/payment-success", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
async def payment_success(request: Request):
    """صفحة العودة بعد إتمام الدفع"""
    return templates.TemplateResponse(request=request, name="payment_success.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)