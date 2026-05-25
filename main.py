import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

import pytz
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage


# =========================================================
# CONFIG & SETUP
# =========================================================

app = FastAPI(title="ระบบงานกิจการนักเรียน โรงเรียนหารเทารังสีประชาสรรค์")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

STUDENT_AFFAIRS_GROUP_ID = os.environ.get("STUDENT_AFFAIRS_GROUP_ID")

LIFF_ID = os.environ.get("LIFF_ID", "2010184816-R1BNqd1n")
LIFF_URL = os.environ.get("LIFF_URL", f"https://liff.line.me/2010184816-R1BNqd1n")

BANGKOK_TZ = pytz.timezone("Asia/Bangkok")

if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
else:
    line_bot_api = None
    handler = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None


def require_supabase() -> Client:
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase is not configured.")
    return supabase


def now_bangkok() -> datetime:
    return datetime.now(BANGKOK_TZ)


# =========================================================
# MODELS
# =========================================================

class BehaviorRecord(BaseModel):
    student_id: str
    student_name: str
    room: str
    offense_name: str
    points_deducted: int
    reason: Optional[str] = ""


class ReportRequest(BaseModel):
    teacher_id: str
    activity_type: str
    records: List[BehaviorRecord]


class NewStudent(BaseModel):
    student_id: str
    name: str
    room: str


class AuthRequest(BaseModel):
    line_user_id: str
    display_name: Optional[str] = ""


class UpdateRoleRequest(BaseModel):
    admin_line_user_id: str
    target_line_user_id: str
    role: str


class UpdateUserStatusRequest(BaseModel):
    admin_line_user_id: str
    target_line_user_id: str
    is_active: bool


# =========================================================
# PERMISSION HELPERS
# =========================================================

def get_menus_by_role(role: str):
    if role == "admin":
        return [
            {"id": "report", "label": "แจ้งพฤติกรรม", "icon": "fa-paper-plane"},
            {"id": "add", "label": "เพิ่มนักเรียน", "icon": "fa-user-plus"},
            {"id": "daily_report", "label": "รายงานวันนี้", "icon": "fa-calendar-day"},
            {"id": "weekly_report", "label": "รายงานสัปดาห์", "icon": "fa-calendar-week"},
            {"id": "monthly_report", "label": "รายงานเดือน", "icon": "fa-calendar-days"},
            {"id": "report_summary", "label": "รายการล่าสุด", "icon": "fa-list"},
            {"id": "manage_users", "label": "สิทธิ์ผู้ใช้", "icon": "fa-user-gear"},
        ]

    return [
        {"id": "report", "label": "แจ้งพฤติกรรม", "icon": "fa-paper-plane"},
        {"id": "add", "label": "เพิ่มนักเรียน", "icon": "fa-user-plus"},
    ]


def get_user_role(line_user_id: str) -> str:
    db = require_supabase()

    res = (
        db.table("users")
        .select("role, is_active")
        .eq("line_user_id", line_user_id)
        .limit(1)
        .execute()
    )

    if not res.data:
        return "user"

    user = res.data[0]

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับการใช้งาน")

    return user.get("role", "user")


def get_display_name(line_user_id: str) -> str:
    db = require_supabase()

    res = (
        db.table("users")
        .select("display_name")
        .eq("line_user_id", line_user_id)
        .limit(1)
        .execute()
    )

    if res.data:
        return res.data[0].get("display_name") or "คุณครู"

    return "คุณครู"


def require_admin(line_user_id: str):
    role = get_user_role(line_user_id)

    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin permission required.")


# =========================================================
# REPORT HELPERS
# =========================================================

def get_period_range(period: str):
    now = now_bangkok()

    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        title = "รายงานวันนี้"

    elif period == "weekly":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
        title = "รายงานสัปดาห์นี้"

    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)

        title = "รายงานเดือนนี้"

    else:
        raise HTTPException(status_code=400, detail="Invalid report period.")

    return start, end, title


def summarize_logs(logs: List[Dict[str, Any]]):
    by_offense: Dict[str, Dict[str, Any]] = {}
    by_room: Dict[str, Dict[str, Any]] = {}
    by_activity: Dict[str, Dict[str, Any]] = {}
    by_teacher: Dict[str, Dict[str, Any]] = {}

    total_records = len(logs)
    total_points = 0
    total_negative = 0
    total_positive = 0

    for log in logs:
        offense = log.get("offense_name") or "ไม่ระบุ"
        room = log.get("room") or "ไม่ระบุ"
        activity = log.get("activity_type") or "ไม่ระบุ"
        teacher = log.get("teacher_id") or "ไม่ระบุ"

        points = int(log.get("points_deducted") or 0)
        total_points += points

        if points < 0:
            total_negative += points
        elif points > 0:
            total_positive += points

        if offense not in by_offense:
            by_offense[offense] = {"name": offense, "count": 0, "points": 0}
        by_offense[offense]["count"] += 1
        by_offense[offense]["points"] += points

        if room not in by_room:
            by_room[room] = {"name": room, "count": 0, "points": 0}
        by_room[room]["count"] += 1
        by_room[room]["points"] += points

        if activity not in by_activity:
            by_activity[activity] = {"name": activity, "count": 0, "points": 0}
        by_activity[activity]["count"] += 1
        by_activity[activity]["points"] += points

        if teacher not in by_teacher:
            by_teacher[teacher] = {"name": teacher, "count": 0, "points": 0}
        by_teacher[teacher]["count"] += 1
        by_teacher[teacher]["points"] += points

    def sort_items(data):
        return sorted(data.values(), key=lambda x: x["count"], reverse=True)

    return {
        "total_records": total_records,
        "total_points": total_points,
        "total_negative": total_negative,
        "total_positive": total_positive,
        "by_offense": sort_items(by_offense),
        "by_room": sort_items(by_room),
        "by_activity": sort_items(by_activity),
        "by_teacher": sort_items(by_teacher),
    }


# =========================================================
# BACKEND APIs
# =========================================================

@app.post("/api/auth/check")
def check_user_role(req: AuthRequest):
    try:
        db = require_supabase()

        res = (
            db.table("users")
            .select("line_user_id, display_name, role, is_active")
            .eq("line_user_id", req.line_user_id)
            .limit(1)
            .execute()
        )

        if res.data and len(res.data) > 0:
            user = res.data[0]

            if not user.get("is_active", True):
                return JSONResponse(
                    {"status": "error", "error": "บัญชีนี้ถูกระงับการใช้งาน"},
                    status_code=403,
                )

            return {
                "status": "success",
                "user": {
                    "line_user_id": user["line_user_id"],
                    "display_name": user.get("display_name") or req.display_name,
                    "role": user["role"],
                },
                "menus": get_menus_by_role(user["role"]),
            }

        db.table("users").insert(
            {
                "line_user_id": req.line_user_id,
                "display_name": req.display_name,
                "role": "user",
                "is_active": True,
            }
        ).execute()

        return {
            "status": "success",
            "user": {
                "line_user_id": req.line_user_id,
                "display_name": req.display_name,
                "role": "user",
            },
            "menus": get_menus_by_role("user"),
        }

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/init")
def get_init_data():
    try:
        db = require_supabase()
        rules = db.table("offense_rules").select("*").order("id").execute()

        return {
            "status": "success",
            "rules": rules.data or [],
        }

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/students/search")
def search_students(q: str):
    try:
        db = require_supabase()

        res = (
            db.table("students")
            .select("student_id, name, room")
            .or_(f"name.ilike.%{q}%,student_id.ilike.%{q}%")
            .limit(20)
            .execute()
        )

        return {
            "status": "success",
            "results": res.data or [],
        }

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/students/add")
def add_student(student: NewStudent):
    try:
        db = require_supabase()

        check = (
            db.table("students")
            .select("id")
            .eq("student_id", student.student_id)
            .eq("name", student.name)
            .eq("room", student.room)
            .limit(1)
            .execute()
        )

        if check.data and len(check.data) > 0:
            return JSONResponse(
                {
                    "status": "error",
                    "error": "ข้อมูลนักเรียนนี้มีในระบบแล้ว",
                },
                status_code=400,
            )

        db.table("students").insert(
            {
                "student_id": student.student_id,
                "name": student.name,
                "room": student.room,
            }
        ).execute()

        return {"status": "success"}

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/report-behavior")
def report_behavior(req: ReportRequest):
    try:
        db = require_supabase()

        role = get_user_role(req.teacher_id)

        if role not in ["admin", "user"]:
            raise HTTPException(status_code=403, detail="Permission denied.")

        now = now_bangkok()
        teacher_name = get_display_name(req.teacher_id)

        if not req.records:
            return JSONResponse(
                {"status": "error", "error": "ไม่มีข้อมูลนักเรียนที่ต้องการแจ้ง"},
                status_code=400,
            )

        log_entries = []

        for r in req.records:
            log_entries.append(
                {
                    "student_id": r.student_id,
                    "student_name": r.student_name,
                    "room": r.room,
                    "teacher_id": req.teacher_id,
                    "activity_type": req.activity_type,
                    "offense_name": r.offense_name,
                    "points_deducted": r.points_deducted,
                    "reason": r.reason,
                    "created_at": now.isoformat(),
                }
            )

        db.table("behavior_logs").insert(log_entries).execute()

        status_groups = {}

        for r in req.records:
            status_groups.setdefault(r.offense_name, []).append(r)

        lines = [
            "🚨 แจ้งพฤติกรรม/การเข้าเรียน",
            f"👤 ผู้แจ้ง: {teacher_name}",
            f"📋 กิจกรรม: {req.activity_type}",
            f"📅 วันที่: {now.strftime('%d/%m/%Y')}",
            f"⏰ เวลา: {now.strftime('%H:%M น.')}",
            "-" * 24,
        ]

        for offense, students in status_groups.items():
            lines.append(f"\n📌 {offense} ({len(students)} คน):")

            for s in students:
                reason_text = f" | หมายเหตุ: {s.reason}" if s.reason else ""
                lines.append(
                    f"- {s.student_name} ({s.room}) | {s.points_deducted} คะแนน{reason_text}"
                )

        notify_text = "\n".join(lines)

        if line_bot_api and STUDENT_AFFAIRS_GROUP_ID:
            line_bot_api.push_message(
                STUDENT_AFFAIRS_GROUP_ID,
                TextSendMessage(text=notify_text),
            )

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/users")
def list_users(admin_line_user_id: str):
    try:
        require_admin(admin_line_user_id)

        db = require_supabase()

        res = (
            db.table("users")
            .select("line_user_id, display_name, role, is_active, created_at")
            .order("created_at", desc=True)
            .execute()
        )

        return {
            "status": "success",
            "users": res.data or [],
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/users/update-role")
def update_user_role(req: UpdateRoleRequest):
    try:
        require_admin(req.admin_line_user_id)

        if req.role not in ["admin", "user"]:
            return JSONResponse(
                {"status": "error", "error": "Invalid role"},
                status_code=400,
            )

        db = require_supabase()

        db.table("users").update(
            {
                "role": req.role,
            }
        ).eq("line_user_id", req.target_line_user_id).execute()

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/users/update-status")
def update_user_status(req: UpdateUserStatusRequest):
    try:
        require_admin(req.admin_line_user_id)

        db = require_supabase()

        db.table("users").update(
            {
                "is_active": req.is_active,
            }
        ).eq("line_user_id", req.target_line_user_id).execute()

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/report-summary")
def report_summary(admin_line_user_id: str):
    try:
        require_admin(admin_line_user_id)

        db = require_supabase()

        logs = (
            db.table("behavior_logs")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return {
            "status": "success",
            "logs": logs.data or [],
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/report-period")
def report_period(admin_line_user_id: str, period: str):
    """
    period = daily | weekly | monthly
    """
    try:
        require_admin(admin_line_user_id)

        start, end, title = get_period_range(period)

        db = require_supabase()

        logs = (
            db.table("behavior_logs")
            .select("*")
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .order("created_at", desc=True)
            .execute()
        )

        data = logs.data or []
        summary = summarize_logs(data)

        return {
            "status": "success",
            "period": period,
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "summary": summary,
            "logs": data[:100],
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# =========================================================
# LINE WEBHOOK
# =========================================================

@app.post("/callback")
async def callback(request: Request):
    if not handler:
        raise HTTPException(status_code=500, detail="LINE handler is not configured.")

    signature = request.headers.get("X-Line-Signature")
    body = await request.body()

    try:
        handler.handle(body.decode("utf-8"), signature)

    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature.")

    return "OK"


if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_text_message(event):
        if event.source.type == "group":
            group_id = event.source.group_id
            user_id = getattr(event.source, "user_id", None)
            user_text = event.message.text.strip().lower()

            print(f"=== YOUR GROUP ID IS: {group_id} ===")
            print(f"=== USER ID IS: {user_id} ===")

            if user_text == "groupid" and line_bot_api:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"Group ID คือ:\n{group_id}\n\nUser ID ของผู้ส่งคือ:\n{user_id or '-'}"
                    ),
                )

            elif user_text == "userid" and line_bot_api:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"User ID ของคุณคือ:\n{user_id or '-'}"),
                )


# =========================================================
# LIFF FRONTEND
# =========================================================

HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">

    <title>ระบบงานกิจการนักเรียน</title>

    <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>

    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">

    <style>
        body {
            font-family: 'Kanit', sans-serif;
            background: #f0f9ff;
        }

        .tab-content {
            display: none;
        }

        .tab-content.active {
            display: block;
            animation: fadeIn 0.25s;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .no-scrollbar::-webkit-scrollbar {
            display: none;
        }

        .no-scrollbar {
            -ms-overflow-style: none;
            scrollbar-width: none;
        }
    </style>
</head>

<body class="pb-10">

    <div class="bg-blue-600 text-white p-5 shadow-md rounded-b-3xl mb-4">
        <h1 class="text-xl font-bold">
            <i class="fa-solid fa-user-shield"></i> ฝ่ายกิจการนักเรียน
        </h1>
        <p class="text-sm opacity-90">โรงเรียนหารเทารังสีประชาสรรค์</p>
        <p id="user_info" class="text-xs opacity-80 mt-2">กำลังเชื่อมต่อ LINE...</p>
    </div>

    <div class="px-4">

        <div id="role_menu_box" class="hidden gap-2 bg-white p-1 rounded-xl shadow-sm mb-4 border border-gray-200 overflow-x-auto no-scrollbar"></div>

        <div id="view_landing" class="tab-content active">
            <div class="bg-white p-6 rounded-2xl shadow-sm text-center border-t-4 border-blue-500">
                <div class="text-5xl text-blue-600 mb-4">
                    <i class="fa-solid fa-user-shield"></i>
                </div>

                <h2 class="text-xl font-bold text-gray-800 mb-2">
                    ระบบกิจการนักเรียน
                </h2>

                <p class="text-sm text-gray-500 mb-5">
                    กดปุ่มด้านล่างเพื่อเข้าสู่ระบบ ระบบจะตรวจสอบสิทธิ์จากบัญชี LINE โดยอัตโนมัติ
                </p>

                <button
                    onclick="enterSystem()"
                    class="w-full bg-blue-600 hover:bg-blue-700 text-white p-4 rounded-xl font-bold shadow-lg text-lg"
                >
                    <i class="fa-solid fa-right-to-bracket"></i>
                    เข้าสู่ระบบกิจการนักเรียน
                </button>
            </div>
        </div>

        <div id="view_report" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-t-4 border-blue-500">
                <label class="font-bold text-gray-700 mb-2 block">
                    <i class="fa-solid fa-clipboard-list text-blue-500"></i> เลือกประเภทกิจกรรม
                </label>

                <select id="activity_type" class="w-full p-3 border rounded-xl bg-gray-50">
                    <option value="">-- กรุณาเลือก --</option>
                    <option value="กิจกรรมหน้าเสาธง">🇹🇭 กิจกรรมหน้าเสาธง</option>
                    <option value="โฮมรูม (Homeroom)">🏠 โฮมรูม (Homeroom)</option>
                    <option value="เช็คชื่อเข้าชั้นเรียน">📚 เช็คชื่อเข้าชั้นเรียน</option>
                    <option value="ตรวจเวร/จราจร">👮 ตรวจเวร/ความเรียบร้อย</option>
                </select>
            </div>

            <div class="bg-white p-4 rounded-xl shadow-sm mb-4">
                <label class="font-bold text-gray-700 mb-2 block">
                    <i class="fa-solid fa-magnifying-glass text-blue-500"></i> ค้นหานักเรียน
                </label>

                <input
                    type="text"
                    id="search_box"
                    onkeyup="searchStudent()"
                    placeholder="พิมพ์ชื่อ หรือ รหัสนักเรียน..."
                    class="w-full p-3 border rounded-xl bg-gray-50 mb-2"
                >

                <div id="search_results" class="hidden border rounded-xl bg-white shadow-sm max-h-48 overflow-y-auto mb-3"></div>

                <div id="selected_student_form" class="hidden mt-3 p-4 bg-blue-50 border border-blue-200 rounded-xl">
                    <div id="selected_name" class="font-bold text-lg text-blue-800 mb-3"></div>

                    <label class="block text-sm font-bold text-gray-700 mb-1">หมวดหมู่ความผิด</label>

                    <select
                        id="offense_type"
                        onchange="handleOffenseChange()"
                        class="w-full p-3 border rounded-xl bg-white mb-3"
                    >
                        <option value="">-- เลือกความผิด --</option>
                    </select>

                    <label class="block text-sm font-bold text-gray-700 mb-1">
                        คะแนน หักให้ใส่ติดลบ เช่น -5
                    </label>

                    <input
                        type="number"
                        id="deduct_score"
                        placeholder="คะแนน เช่น -5"
                        class="w-full p-3 border rounded-xl bg-white mb-3 transition"
                    >

                    <input
                        type="text"
                        id="reason"
                        placeholder="หมายเหตุเพิ่มเติม ถ้ามี"
                        class="w-full p-3 border rounded-xl bg-white mb-3"
                    >

                    <button
                        type="button"
                        onclick="addToList()"
                        class="w-full bg-blue-500 hover:bg-blue-600 text-white p-3 rounded-xl font-bold transition shadow-sm"
                    >
                        <i class="fa-solid fa-plus"></i> เพิ่มลงรายการเตรียมส่ง
                    </button>
                </div>
            </div>

            <h3 class="font-bold text-gray-700 mb-2">
                รายการที่เตรียมแจ้ง (<span id="draft_count">0</span> คน)
            </h3>

            <div id="draft_list" class="space-y-2 mb-6"></div>

            <button
                onclick="submitReport()"
                class="w-full bg-green-500 text-white p-4 rounded-xl font-bold shadow-lg"
            >
                <i class="fa-solid fa-paper-plane"></i> ส่งข้อมูลเข้ากลุ่มกิจการนักเรียน
            </button>
        </div>

        <div id="view_add" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-green-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-user-plus text-green-500"></i> เพิ่มนักเรียนตกหล่น
                </h2>

                <p class="text-xs text-gray-500 mb-4">
                    หากค้นหารายชื่อไม่พบ ครูสามารถเพิ่มข้อมูลนักเรียนเข้าสู่ระบบได้ที่นี่
                </p>

                <label class="block text-sm font-bold text-gray-700 mb-1">รหัสนักเรียน</label>
                <input type="text" id="new_id" placeholder="เช่น 22111" class="w-full p-3 border rounded-xl bg-gray-50 mb-3">

                <label class="block text-sm font-bold text-gray-700 mb-1">ชื่อ-นามสกุล</label>
                <input type="text" id="new_name" placeholder="เช่น ด.ช. สมชาย ใจดี" class="w-full p-3 border rounded-xl bg-gray-50 mb-3">

                <label class="block text-sm font-bold text-gray-700 mb-1">ชั้นเรียน</label>
                <input type="text" id="new_room" placeholder="เช่น ม.1/1" class="w-full p-3 border rounded-xl bg-gray-50 mb-4">

                <button onclick="confirmAddStudent()" class="w-full bg-green-600 text-white p-3 rounded-xl font-bold shadow-sm">
                    บันทึกข้อมูลนักเรียน
                </button>
            </div>
        </div>

        <div id="view_manage_users" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-purple-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-user-gear text-purple-500"></i> จัดการสิทธิ์ผู้ใช้
                </h2>

                <p class="text-xs text-gray-500 mb-4">
                    เฉพาะแอดมินเท่านั้น สามารถเปลี่ยนสิทธิ์ และเปิด/ปิดการใช้งานผู้ใช้ได้
                </p>

                <button onclick="loadUsers()" class="w-full bg-purple-600 text-white p-3 rounded-xl font-bold mb-4">
                    โหลดรายชื่อผู้ใช้
                </button>

                <div id="users_list" class="space-y-2"></div>
            </div>
        </div>

        <div id="view_daily_report" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-sky-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-calendar-day text-sky-500"></i> รายงานวันนี้
                </h2>
                <button onclick="loadPeriodReport('daily', 'daily_report_box')" class="w-full bg-sky-600 text-white p-3 rounded-xl font-bold mb-4">
                    โหลดรายงานวันนี้
                </button>
                <div id="daily_report_box"></div>
            </div>
        </div>

        <div id="view_weekly_report" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-indigo-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-calendar-week text-indigo-500"></i> รายงานสัปดาห์นี้
                </h2>
                <button onclick="loadPeriodReport('weekly', 'weekly_report_box')" class="w-full bg-indigo-600 text-white p-3 rounded-xl font-bold mb-4">
                    โหลดรายงานสัปดาห์นี้
                </button>
                <div id="weekly_report_box"></div>
            </div>
        </div>

        <div id="view_monthly_report" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-teal-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-calendar-days text-teal-500"></i> รายงานเดือนนี้
                </h2>
                <button onclick="loadPeriodReport('monthly', 'monthly_report_box')" class="w-full bg-teal-600 text-white p-3 rounded-xl font-bold mb-4">
                    โหลดรายงานเดือนนี้
                </button>
                <div id="monthly_report_box"></div>
            </div>
        </div>

        <div id="view_report_summary" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-orange-500">
                <h2 class="font-bold text-gray-700 mb-3">
                    <i class="fa-solid fa-list text-orange-500"></i> รายการแจ้งล่าสุด
                </h2>

                <p class="text-xs text-gray-500 mb-4">
                    แสดงรายการแจ้งพฤติกรรมล่าสุด 100 รายการ
                </p>

                <button onclick="loadReportSummary()" class="w-full bg-orange-500 text-white p-3 rounded-xl font-bold mb-4">
                    โหลดรายการล่าสุด
                </button>

                <div id="summary_list" class="space-y-2"></div>
            </div>
        </div>
    </div>

<script>
    const LIFF_ID = "__LIFF_ID__";

    let USER_ID = "";
    let CURRENT_ROLE = "user";
    let CURRENT_USER = null;

    let offenseRules = [];
    let currentSelectedStudent = null;
    let draftList = [];
    let searchTimeout = null;

    async function main() {
        try {
            await liff.init({ liffId: LIFF_ID });

            if (!liff.isLoggedIn()) {
                liff.login();
                return;
            }

            const profile = await liff.getProfile();
            USER_ID = profile.userId;

            document.getElementById("user_info").innerText =
                `LINE: ${profile.displayName || "-"}`;

            renderDraftList();

        } catch (e) {
            console.error(e);
            Swal.fire("Error", "ไม่สามารถเชื่อมต่อ LINE LIFF ได้", "error");
        }
    }

    async function enterSystem() {
        try {
            Swal.fire({
                title: "กำลังตรวจสอบสิทธิ์...",
                allowOutsideClick: false,
                didOpen: () => {
                    Swal.showLoading();
                }
            });

            const profile = await liff.getProfile();
            USER_ID = profile.userId;

            const authRes = await fetch("/api/auth/check", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    line_user_id: profile.userId,
                    display_name: profile.displayName
                })
            });

            const authData = await authRes.json();

            if (authData.status === "error") {
                throw new Error(authData.error);
            }

            CURRENT_USER = authData.user;
            CURRENT_ROLE = authData.user.role;

            document.getElementById("user_info").innerText =
                `ผู้ใช้: ${CURRENT_USER.display_name || "-"} | สิทธิ์: ${CURRENT_ROLE}`;

            renderMenusByRole(authData.menus);

            await loadInitData();

            document.getElementById("role_menu_box").classList.remove("hidden");
            document.getElementById("role_menu_box").classList.add("flex");

            Swal.close();

            switchTab("report");

        } catch (e) {
            console.error(e);
            Swal.fire("ข้อผิดพลาด", e.message || "ไม่สามารถตรวจสอบสิทธิ์ได้", "error");
        }
    }

    async function loadInitData() {
        const res = await fetch("/api/init");
        const data = await res.json();

        if (data.status === "error") {
            throw new Error(data.error);
        }

        offenseRules = data.rules || [];

        const select = document.getElementById("offense_type");
        select.innerHTML = '<option value="">-- เลือกความผิด --</option>';

        offenseRules.forEach(rule => {
            const opt = document.createElement("option");

            opt.value = `${rule.rule_name}|${rule.default_points !== null ? rule.default_points : 0}|${rule.require_manual_score}`;

            opt.innerText = `${rule.rule_name} ${
                rule.require_manual_score
                    ? "(ระบุคะแนนเอง)"
                    : `(${rule.default_points})`
            }`;

            select.appendChild(opt);
        });
    }

    function renderMenusByRole(menus) {
        const navBox = document.getElementById("role_menu_box");

        navBox.innerHTML = menus.map(menu => `
            <button
                onclick="switchTab('${menu.id}')"
                id="nav_${menu.id}"
                class="min-w-[90px] flex-1 py-2 text-xs font-bold text-gray-500 rounded-lg transition-colors"
            >
                <i class="fa-solid ${menu.icon}"></i><br>${menu.label}
            </button>
        `).join("");
    }

    function switchTab(tab) {
        const views = document.querySelectorAll(".tab-content");

        views.forEach(v => {
            v.classList.remove("active");
        });

        const navs = document.querySelectorAll("[id^='nav_']");

        navs.forEach(n => {
            n.className = "min-w-[90px] flex-1 py-2 text-xs font-bold text-gray-500 rounded-lg transition-colors";
        });

        const targetView = document.getElementById(`view_${tab}`);
        const targetNav = document.getElementById(`nav_${tab}`);

        if (targetView) {
            targetView.classList.add("active");
        }

        if (targetNav) {
            targetNav.className = "min-w-[90px] flex-1 py-2 text-xs font-bold bg-blue-100 text-blue-700 rounded-lg transition-colors";
        }
    }

    function handleOffenseChange() {
        const select = document.getElementById("offense_type");
        const scoreInput = document.getElementById("deduct_score");

        if (!select.value) {
            scoreInput.value = "";
            scoreInput.classList.remove("border-red-500", "bg-red-50");
            return;
        }

        const parts = select.value.split("|");
        const defaultScore = parts[1];
        const requireManual = parts[2] === "true";

        if (requireManual) {
            scoreInput.value = "";
            scoreInput.placeholder = "กรุณาระบุคะแนนที่ต้องการหัก";
            scoreInput.classList.add("border-red-500", "bg-red-50");
            scoreInput.focus();
        } else {
            scoreInput.value = defaultScore;
            scoreInput.classList.remove("border-red-500", "bg-red-50");
        }
    }

    async function searchStudent() {
        clearTimeout(searchTimeout);

        const query = document.getElementById("search_box").value.trim();
        const resultsBox = document.getElementById("search_results");

        if (query.length < 2) {
            resultsBox.classList.add("hidden");
            return;
        }

        searchTimeout = setTimeout(async () => {
            try {
                const res = await fetch(`/api/students/search?q=${encodeURIComponent(query)}`);
                const data = await res.json();

                if (!data.results || data.results.length === 0) {
                    resultsBox.innerHTML = `
                        <div class="p-3 text-sm text-gray-400">
                            ไม่พบข้อมูลนักเรียน
                        </div>
                    `;
                    resultsBox.classList.remove("hidden");
                    return;
                }

                resultsBox.innerHTML = data.results.map((s, idx) => `
                    <div
                        onclick="selectStudentByIndex(${idx})"
                        class="p-3 border-b hover:bg-blue-50 cursor-pointer"
                    >
                        <span class="font-bold text-gray-800">${escapeHtml(s.student_id)}</span>
                        - ${escapeHtml(s.name)}
                        <span class="text-sm text-gray-500">(${escapeHtml(s.room)})</span>
                    </div>
                `).join("");

                window.latestStudentSearchResults = data.results;
                resultsBox.classList.remove("hidden");

            } catch (e) {
                console.error(e);
            }
        }, 300);
    }

    function selectStudentByIndex(index) {
        const s = window.latestStudentSearchResults[index];

        if (!s) {
            return;
        }

        selectStudent(s.student_id, s.name, s.room);
    }

    function selectStudent(id, name, room) {
        currentSelectedStudent = {
            student_id: id,
            student_name: name,
            room: room
        };

        document.getElementById("search_box").value = "";
        document.getElementById("search_results").classList.add("hidden");
        document.getElementById("selected_name").innerText = `${name} (${room})`;
        document.getElementById("selected_student_form").classList.remove("hidden");
    }

    function addToList() {
        const select = document.getElementById("offense_type");
        const scoreInput = document.getElementById("deduct_score");
        const reasonInput = document.getElementById("reason");

        if (!currentSelectedStudent) {
            return Swal.fire("แจ้งเตือน", "กรุณาเลือกนักเรียนก่อน", "warning");
        }

        if (!select.value) {
            return Swal.fire("แจ้งเตือน", "กรุณาเลือกความผิด", "warning");
        }

        const parts = select.value.split("|");
        const offenseName = parts[0];
        const requireManual = parts[2] === "true";
        const points = parseInt(scoreInput.value);

        if (requireManual && isNaN(points)) {
            return Swal.fire("แจ้งเตือน", "กรุณาระบุคะแนน", "warning");
        }

        draftList.push({
            ...currentSelectedStudent,
            offense_name: offenseName,
            points_deducted: isNaN(points) ? 0 : points,
            reason: reasonInput.value
        });

        currentSelectedStudent = null;

        document.getElementById("selected_student_form").classList.add("hidden");

        select.value = "";
        scoreInput.value = "";
        reasonInput.value = "";
        scoreInput.classList.remove("border-red-500", "bg-red-50");

        renderDraftList();
    }

    function renderDraftList() {
        document.getElementById("draft_count").innerText = draftList.length;

        const box = document.getElementById("draft_list");

        if (draftList.length === 0) {
            box.innerHTML = `
                <div class="text-gray-400 text-center p-5 bg-white rounded-xl border border-dashed">
                    ยังไม่ได้เลือกนักเรียน
                </div>
            `;
            return;
        }

        box.innerHTML = draftList.map((item, index) => `
            <div class="bg-white p-3 border rounded-xl flex justify-between items-center shadow-sm">
                <div>
                    <div class="font-bold text-gray-800">
                        ${escapeHtml(item.student_name)}
                        <span class="text-xs text-gray-500">(${escapeHtml(item.room)})</span>
                    </div>

                    <div class="text-sm text-red-600">
                        ${escapeHtml(item.offense_name)} (${item.points_deducted})
                        ${item.reason ? `- ${escapeHtml(item.reason)}` : ""}
                    </div>
                </div>

                <button onclick="removeFromList(${index})" class="text-red-400 hover:text-red-600 bg-red-50 p-2 rounded-lg">
                    <i class="fa-solid fa-trash"></i>
                </button>
            </div>
        `).join("");
    }

    function removeFromList(index) {
        draftList.splice(index, 1);
        renderDraftList();
    }

    async function submitReport() {
        const activityType = document.getElementById("activity_type").value;

        if (!activityType) {
            return Swal.fire("แจ้งเตือน", "กรุณาเลือกประเภทกิจกรรมก่อน", "warning");
        }

        if (draftList.length === 0) {
            return Swal.fire("แจ้งเตือน", "กรุณาเพิ่มนักเรียนอย่างน้อย 1 คน", "warning");
        }

        Swal.fire({
            title: "กำลังส่งข้อมูล...",
            allowOutsideClick: false,
            didOpen: () => {
                Swal.showLoading();
            }
        });

        try {
            const res = await fetch("/api/report-behavior", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    teacher_id: USER_ID,
                    activity_type: activityType,
                    records: draftList
                })
            });

            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            Swal.fire("สำเร็จ", "ส่งข้อมูลเข้ากลุ่มกิจการนักเรียนเรียบร้อย", "success");

            draftList = [];
            document.getElementById("activity_type").value = "";

            renderDraftList();

        } catch (e) {
            Swal.fire("ข้อผิดพลาด", e.message, "error");
        }
    }

    async function confirmAddStudent() {
        const student_id = document.getElementById("new_id").value.trim();
        const name = document.getElementById("new_name").value.trim();
        const room = document.getElementById("new_room").value.trim();

        if (!student_id || !name || !room) {
            return Swal.fire("แจ้งเตือน", "กรุณากรอกให้ครบทุกช่อง", "warning");
        }

        const result = await Swal.fire({
            title: "ยืนยันเพิ่มข้อมูล?",
            html: `ชื่อ: <b>${escapeHtml(name)}</b><br>ห้อง: ${escapeHtml(room)}<br>รหัส: ${escapeHtml(student_id)}`,
            icon: "question",
            showCancelButton: true,
            confirmButtonText: "ยืนยัน บันทึกเลย",
            cancelButtonText: "ยกเลิก",
            confirmButtonColor: "#16a34a"
        });

        if (!result.isConfirmed) {
            return;
        }

        try {
            const response = await fetch("/api/students/add", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    student_id,
                    name,
                    room
                })
            });

            const data = await response.json();

            if (data.status === "success") {
                Swal.fire("สำเร็จ", "เพิ่มรายชื่อนักเรียนเรียบร้อยแล้ว", "success");

                document.getElementById("new_id").value = "";
                document.getElementById("new_name").value = "";
                document.getElementById("new_room").value = "";

            } else {
                Swal.fire("เกิดข้อผิดพลาด", data.error, "error");
            }

        } catch (e) {
            Swal.fire("ข้อผิดพลาด", "ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้", "error");
        }
    }

    async function loadUsers() {
        if (CURRENT_ROLE !== "admin") {
            return Swal.fire("ไม่ได้รับอนุญาต", "เมนูนี้สำหรับแอดมินเท่านั้น", "error");
        }

        const box = document.getElementById("users_list");
        box.innerHTML = `<div class="text-gray-400 p-3">กำลังโหลด...</div>`;

        try {
            const res = await fetch(`/api/admin/users?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            if (!data.users || data.users.length === 0) {
                box.innerHTML = `<div class="text-gray-400 p-3">ยังไม่มีผู้ใช้</div>`;
                return;
            }

            box.innerHTML = data.users.map(user => `
                <div class="bg-gray-50 border rounded-xl p-3">
                    <div class="font-bold text-gray-800">
                        ${escapeHtml(user.display_name || "ไม่ระบุชื่อ")}
                    </div>

                    <div class="text-xs text-gray-500 break-all mb-2">
                        ${escapeHtml(user.line_user_id)}
                    </div>

                    <div class="text-xs mb-2 ${user.is_active ? 'text-green-600' : 'text-red-600'}">
                        สถานะ: ${user.is_active ? 'ใช้งานได้' : 'ถูกระงับ'}
                    </div>

                    <div class="flex items-center gap-2 mb-2">
                        <select id="role_${escapeAttr(user.line_user_id)}" class="flex-1 p-2 border rounded-lg bg-white">
                            <option value="user" ${user.role === "user" ? "selected" : ""}>user</option>
                            <option value="admin" ${user.role === "admin" ? "selected" : ""}>admin</option>
                        </select>

                        <button onclick="updateRole('${escapeJs(user.line_user_id)}')" class="bg-purple-600 text-white px-3 py-2 rounded-lg text-sm">
                            บันทึกสิทธิ์
                        </button>
                    </div>

                    <div class="flex gap-2">
                        <button onclick="updateUserStatus('${escapeJs(user.line_user_id)}', true)" class="flex-1 bg-green-600 text-white px-3 py-2 rounded-lg text-sm">
                            เปิดใช้
                        </button>

                        <button onclick="updateUserStatus('${escapeJs(user.line_user_id)}', false)" class="flex-1 bg-red-600 text-white px-3 py-2 rounded-lg text-sm">
                            ระงับ
                        </button>
                    </div>
                </div>
            `).join("");

        } catch (e) {
            box.innerHTML = `<div class="text-red-500 p-3">${escapeHtml(e.message)}</div>`;
        }
    }

    async function updateRole(targetLineUserId) {
        const role = document.getElementById(`role_${targetLineUserId}`).value;

        try {
            const res = await fetch("/api/admin/users/update-role", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    admin_line_user_id: USER_ID,
                    target_line_user_id: targetLineUserId,
                    role: role
                })
            });

            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            Swal.fire("สำเร็จ", "อัปเดตสิทธิ์เรียบร้อยแล้ว", "success");

        } catch (e) {
            Swal.fire("ข้อผิดพลาด", e.message, "error");
        }
    }

    async function updateUserStatus(targetLineUserId, isActive) {
        try {
            const res = await fetch("/api/admin/users/update-status", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    admin_line_user_id: USER_ID,
                    target_line_user_id: targetLineUserId,
                    is_active: isActive
                })
            });

            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            Swal.fire("สำเร็จ", "อัปเดตสถานะผู้ใช้เรียบร้อยแล้ว", "success");
            loadUsers();

        } catch (e) {
            Swal.fire("ข้อผิดพลาด", e.message, "error");
        }
    }

    async function loadPeriodReport(period, boxId) {
        if (CURRENT_ROLE !== "admin") {
            return Swal.fire("ไม่ได้รับอนุญาต", "เมนูนี้สำหรับแอดมินเท่านั้น", "error");
        }

        const box = document.getElementById(boxId);
        box.innerHTML = `<div class="text-gray-400 p-3">กำลังโหลดรายงาน...</div>`;

        try {
            const res = await fetch(`/api/admin/report-period?admin_line_user_id=${encodeURIComponent(USER_ID)}&period=${encodeURIComponent(period)}`);
            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            box.innerHTML = renderPeriodReport(data);

        } catch (e) {
            box.innerHTML = `<div class="text-red-500 p-3">${escapeHtml(e.message)}</div>`;
        }
    }

    function renderPeriodReport(data) {
        const s = data.summary || {};

        return `
            <div class="space-y-3">
                <div class="bg-gray-50 border rounded-xl p-3">
                    <div class="font-bold text-gray-800">${escapeHtml(data.title || "-")}</div>
                    <div class="text-xs text-gray-500 break-all">
                        ${escapeHtml(data.start || "")} ถึง ${escapeHtml(data.end || "")}
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-2">
                    <div class="bg-white border rounded-xl p-3 text-center">
                        <div class="text-2xl font-bold text-blue-600">${s.total_records || 0}</div>
                        <div class="text-xs text-gray-500">รายการทั้งหมด</div>
                    </div>

                    <div class="bg-white border rounded-xl p-3 text-center">
                        <div class="text-2xl font-bold text-red-600">${s.total_negative || 0}</div>
                        <div class="text-xs text-gray-500">คะแนนหักรวม</div>
                    </div>

                    <div class="bg-white border rounded-xl p-3 text-center">
                        <div class="text-2xl font-bold text-green-600">${s.total_positive || 0}</div>
                        <div class="text-xs text-gray-500">คะแนนบวกรวม</div>
                    </div>

                    <div class="bg-white border rounded-xl p-3 text-center">
                        <div class="text-2xl font-bold text-gray-700">${s.total_points || 0}</div>
                        <div class="text-xs text-gray-500">สุทธิ</div>
                    </div>
                </div>

                ${renderSummaryGroup("แยกตามความผิด", s.by_offense)}
                ${renderSummaryGroup("แยกตามห้อง", s.by_room)}
                ${renderSummaryGroup("แยกตามกิจกรรม", s.by_activity)}

                <div class="bg-white border rounded-xl p-3">
                    <div class="font-bold text-gray-700 mb-2">รายการล่าสุดในช่วงนี้</div>
                    ${(data.logs || []).length === 0 ? `
                        <div class="text-gray-400 text-sm">ไม่มีรายการ</div>
                    ` : (data.logs || []).map(log => `
                        <div class="border-b py-2">
                            <div class="font-bold text-gray-800">
                                ${escapeHtml(log.student_name || log.student_id || "-")}
                                <span class="text-xs text-gray-500">(${escapeHtml(log.room || "-")})</span>
                            </div>
                            <div class="text-sm text-red-600">
                                ${escapeHtml(log.offense_name || "-")} (${log.points_deducted || 0})
                            </div>
                            <div class="text-xs text-gray-500">
                                ${escapeHtml(log.activity_type || "-")} | ${escapeHtml(log.created_at || "")}
                            </div>
                        </div>
                    `).join("")}
                </div>
            </div>
        `;
    }

    function renderSummaryGroup(title, items) {
        if (!items || items.length === 0) {
            return `
                <div class="bg-white border rounded-xl p-3">
                    <div class="font-bold text-gray-700 mb-2">${escapeHtml(title)}</div>
                    <div class="text-gray-400 text-sm">ไม่มีข้อมูล</div>
                </div>
            `;
        }

        return `
            <div class="bg-white border rounded-xl p-3">
                <div class="font-bold text-gray-700 mb-2">${escapeHtml(title)}</div>
                ${items.map(item => `
                    <div class="flex justify-between border-b py-2 text-sm">
                        <div>
                            <div class="font-bold text-gray-800">${escapeHtml(item.name)}</div>
                            <div class="text-xs text-gray-500">${item.count} รายการ</div>
                        </div>
                        <div class="${item.points < 0 ? 'text-red-600' : 'text-green-600'} font-bold">
                            ${item.points}
                        </div>
                    </div>
                `).join("")}
            </div>
        `;
    }

    async function loadReportSummary() {
        if (CURRENT_ROLE !== "admin") {
            return Swal.fire("ไม่ได้รับอนุญาต", "เมนูนี้สำหรับแอดมินเท่านั้น", "error");
        }

        const box = document.getElementById("summary_list");
        box.innerHTML = `<div class="text-gray-400 p-3">กำลังโหลด...</div>`;

        try {
            const res = await fetch(`/api/admin/report-summary?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
            const data = await res.json();

            if (data.status === "error") {
                throw new Error(data.error);
            }

            if (!data.logs || data.logs.length === 0) {
                box.innerHTML = `<div class="text-gray-400 p-3">ยังไม่มีข้อมูล</div>`;
                return;
            }

            box.innerHTML = data.logs.map(log => `
                <div class="bg-gray-50 border rounded-xl p-3">
                    <div class="font-bold text-gray-800">
                        ${escapeHtml(log.student_name || log.student_id || "-")}
                        <span class="text-xs text-gray-500">(${escapeHtml(log.room || "-")})</span>
                    </div>

                    <div class="text-sm text-red-600">
                        ${escapeHtml(log.offense_name || "-")}
                        <span>(${log.points_deducted})</span>
                    </div>

                    <div class="text-xs text-gray-500">
                        กิจกรรม: ${escapeHtml(log.activity_type || "-")}
                    </div>

                    <div class="text-xs text-gray-400">
                        ${escapeHtml(log.created_at || "")}
                    </div>
                </div>
            `).join("");

        } catch (e) {
            box.innerHTML = `<div class="text-red-500 p-3">${escapeHtml(e.message)}</div>`;
        }
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) {
            return "";
        }

        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function escapeAttr(value) {
        return escapeHtml(value);
    }

    function escapeJs(value) {
        if (value === null || value === undefined) {
            return "";
        }

        return String(value)
            .replace(/\\/g, "\\\\")
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, " ")
            .replace(/\r/g, " ");
    }

    main();
</script>

</body>
</html>
'''


@app.get("/liff")
def get_liff_page():
    html = HTML_TEMPLATE.replace("__LIFF_ID__", LIFF_ID)
    return HTMLResponse(content=html, status_code=200)


@app.get("/")
def health_check():
    return {
        "status": "running",
        "service": "Harnthao Rangsi Prachasan - Student Affairs",
        "liff_id": LIFF_ID,
        "liff_url": LIFF_URL,
        "student_affairs_group_id": STUDENT_AFFAIRS_GROUP_ID,
    }
