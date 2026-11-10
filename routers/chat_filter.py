"""
🔒 فلتر محادثات لبيب
بيمنع: أرقام الهواتف، حسابات التواصل (واتساب/تلغرام/...)، والروابط غير التعليمية.
check_message(text) بيرجع None لو الرسالة سليمة، أو "contact" أو "link" حسب سبب المنع.
"""
import re
from urllib.parse import urlparse

# ✅ الروابط التعليمية المسموحة (عدّل القائمة من هون)
ALLOWED_LINK_DOMAINS = (
    "youtube.com", "youtu.be",
    "drive.google.com", "docs.google.com",
    "wikipedia.org",
    "khanacademy.org",
    "res.cloudinary.com",   # ملفات المنصة نفسها (كتب، صور، تسجيلات)
)

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_INVISIBLE_RE = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2060\ufeff]')
_TASHKEEL_RE = re.compile(r'[\u064B-\u065F\u0670\u0640]')

# كلمات بتنكشف حتى لو انكتبت بمسافات أو نقاط بين حروفها (و ا ت س ا ب)
_CONTACT_COMPACT = (
    "واتساب", "واتس", "وتساب", "واتسب", "whatsapp", "whatsap", "watsapp", "watsap",
    "تلغرام", "تلجرام", "تيليجرام", "تيلغرام", "تليجرام", "تيليغرام", "telegram",
    "فايبر", "viber",
    "انستغرام", "انستجرام", "انستا", "instagram",
    "فيسبوك", "facebook", "تيكتوك", "tiktok",
    "سنابشات", "snapchat", "ديسكورد", "discord", "سكايب", "skype",
    "ايميل", "اميل", "جيميل", "email", "gmail", "hotmail", "yahoo", "outlook", "هوتميل",
)
# كلمات قصيرة بتنفحص ككلمة كاملة بس (عشان ما نمنع كلمات عادية مثل سنابل)
_CONTACT_TOKENS = {"سناب", "snap", "insta", "انستا", "زوم", "zoom", "ايمو"}

_DIGIT_WORDS = {
    "صفر", "واحد", "واحده", "اثنين", "اثنان", "ثنتين", "ثلاثه", "ثلاث", "اربعه", "اربع",
    "خمسه", "خمس", "سته", "ست", "سبعه", "سبع", "ثمانيه", "ثماني", "تسعه", "تسع", "عشره",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
}

_TLDS = (
    "com|net|org|io|me|ly|co|app|tv|gg|link|info|xyz|ai|dev|edu|gov|qa|sa|ae|kw|bz|ws|cc|to|us|uk|ru|ir|"
    "tk|ml|site|online|store|shop|page|live|chat|club|cloud|fm|am|be|gl|gd|cutt|rb|sh|id|in|pk|eg|jo|ps|lb"
)
_URL_RE = re.compile(
    r'(?:https?://|www\.)[^\s<>"\']+'
    r'|(?<![\w@.])(?:[a-z0-9][a-z0-9\-]{0,60}\.)+(?:' + _TLDS + r')\b(?:/[^\s<>"\']*)?',
    re.I,
)


def _normalize(text: str) -> str:
    t = (text or "").translate(_DIGIT_MAP)
    t = _INVISIBLE_RE.sub("", t)
    t = _TASHKEEL_RE.sub("", t)
    t = t.lower()
    return (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
             .replace("ة", "ه").replace("ى", "ي"))


def _has_phone(t: str) -> bool:
    # 1) رقم متواصل من 8 خانات أو أكثر
    if re.search(r"\d{8,}", t):
        return True
    # 2) أرقام مفصولة بمسافات/شرطات/نقاط: مجموعتين على الأقل كل وحدة 3 خانات فأكثر، والمجموع 8 فأكثر
    for m in re.finditer(r"\+?\d[\d\s\-\.\(\)/_]{6,}\d", t):
        groups = re.findall(r"\d+", m.group(0))
        if sum(len(g) for g in groups) >= 8 and sum(1 for g in groups if len(g) >= 3) >= 2:
            return True
    # 2ب) أرقام فردية مفصولة (5 5 5 1 2 3 4): 8 خانات فردية ورا بعض أو أكثر
    if re.search(r"(?:\b\d[\s\-\.]+){7,}\d\b", t):
        return True
    # 3) رمز دولة مثل +974 متبوع بأرقام
    if re.search(r"\+\s*\d{1,3}[\s\-\.]*\d{4,}", t):
        return True
    # 4) أرقام مكتوبة بالحروف: 6 كلمات رقم ورا بعض أو أكثر
    run = 0
    for tok in re.findall(r"[^\W\d_]+|\d+", t):
        if tok.isdigit():
            continue
        if tok in _DIGIT_WORDS:
            run += 1
            if run >= 6:
                return True
        else:
            run = 0
    return False


def _has_contact_word(t: str) -> bool:
    # @حساب أو إيميل
    if re.search(r"@[a-z0-9_.\u0600-\u06FF]{3,}", t):
        return True
    compact = re.sub(r"[^0-9a-z\u0600-\u06FF]", "", t)
    if any(w in compact for w in _CONTACT_COMPACT):
        return True
    tokens = set(re.findall(r"[^\W\d_]+", t))
    return bool(tokens & _CONTACT_TOKENS)


def _host_allowed(host: str) -> bool:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in ALLOWED_LINK_DOMAINS)


def _has_forbidden_link(t: str) -> bool:
    for m in _URL_RE.finditer(t):
        raw = m.group(0).rstrip(".,;:!?)»\"'")
        if not re.match(r"^https?://", raw):
            raw = "http://" + raw
        try:
            host = urlparse(raw).hostname or ""
        except ValueError:
            return True
        if not host or not _host_allowed(host):
            return True
    return False


def check_message(text: str):
    """None = الرسالة سليمة، "contact" = رقم هاتف أو حساب تواصل، "link" = رابط غير مسموح"""
    t = _normalize(text)
    if _has_phone(t) or _has_contact_word(t):
        return "contact"
    if _has_forbidden_link(t):
        return "link"
    return None