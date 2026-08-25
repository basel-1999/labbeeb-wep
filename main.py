import os
from fastapi import FastAPI, Request, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# استيراد راوترات الـ API
from routers.api_router import router as api_router
from routers.session_router import router as session_router

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

# ==========================================
# ▶️ تشغيل السيرفر
# ==========================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)