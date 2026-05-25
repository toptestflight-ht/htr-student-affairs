import os
import uuid
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

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None
supabase: Optional[Client] = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def require_supabase() -> Client:
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase is not configured.")
    return supabase


def now_bangkok() -> datetime:
    return datetime.now(BANGKOK_TZ)


def send_line_message(text: str) -> bool:
    if line_bot_api and STUDENT_AFFAIRS_GROUP_ID:
        line_bot_api.push_message(STUDENT_AFFAIRS_GROUP_ID, TextSendMessage(text=text))
        return True
    return False


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
    request_id: Optional[str] = None
    academic_year: Optional[int] = None
    semester: Optional[int] = None


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


class UpdateBehaviorLogRequest(BaseModel):
    admin_line_user_id: str
    log_id: str
    activity_type: Optional[str] = None
    offense_name: Optional[str] = None
    points_deducted: Optional[int] = None
    reason: Optional[str] = None


class DeleteBehaviorLogRequest(BaseModel):
    admin_line_user_id: str
    log_id: str


class UpsertRuleRequest(BaseModel):
    admin_line_user_id: str
    rule_id: Optional[int] = None
    rule_name: str
    default_points: Optional[int] = None
    require_manual_score: bool = False
    is_active: bool = True


class UpdateSettingsRequest(BaseModel):
    admin_line_user_id: str
    academic_year: int
    semester: int
    base_score: int = 100
    warning_threshold: int = 80
    risk_threshold: int = 60
    repeat_offense_threshold: int = 3


class SendReportToGroupRequest(BaseModel):
    admin_line_user_id: str
    period: str


# =========================================================
# PERMISSION HELPERS
# =========================================================

ADMIN_ROLES = ["super_admin", "admin"]
REPORT_VIEW_ROLES = ["super_admin", "admin", "viewer"]
WRITE_ROLES = ["super_admin", "admin", "user", "teacher", "homeroom_teacher"]


def can_manage(role: str) -> bool:
    return role in ADMIN_ROLES


def can_view_report(role: str) -> bool:
    return role in REPORT_VIEW_ROLES


def get_user(line_user_id: str):
    db = require_supabase()
    res = db.table("users").select("*").eq("line_user_id", line_user_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_user_role(line_user_id: str) -> str:
    user = get_user(line_user_id)

    if not user:
        return "user"

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="บัญชีนี้ถูกระงับการใช้งาน")

    return user.get("role", "user")


def get_display_name(line_user_id: str) -> str:
    user = get_user(line_user_id)
    return user.get("display_name") if user and user.get("display_name") else "คุณครู"


def require_admin(line_user_id: str):
    role = get_user_role(line_user_id)
    if not can_manage(role):
        raise HTTPException(status_code=403, detail="Admin permission required.")


def require_report_view(line_user_id: str):
    role = get_user_role(line_user_id)
    if not can_view_report(role):
        raise HTTPException(status_code=403, detail="Report permission required.")


def audit_log(actor_line_user_id: str, action: str, target_type: str = "", target_id: str = "", detail: Optional[dict] = None):
    try:
        db = require_supabase()
        db.table("audit_logs").insert({
            "actor_line_user_id": actor_line_user_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "detail": detail or {},
            "created_at": now_bangkok().isoformat()
        }).execute()
    except Exception as e:
        print(f"AUDIT ERROR: {e}")


def get_menus_by_role(role: str):
    if role in ["super_admin", "admin"]:
        return [
            {"id": "dashboard", "label": "แดชบอร์ด", "icon": "fa-gauge"},
            {"id": "report", "label": "แจ้งพฤติกรรม", "icon": "fa-paper-plane"},
            {"id": "student_report", "label": "รายบุคคล", "icon": "fa-user-graduate"},
            {"id": "add", "label": "เพิ่มนักเรียน", "icon": "fa-user-plus"},
            {"id": "daily_report", "label": "วันนี้", "icon": "fa-calendar-day"},
            {"id": "weekly_report", "label": "สัปดาห์", "icon": "fa-calendar-week"},
            {"id": "monthly_report", "label": "เดือน", "icon": "fa-calendar-days"},
            {"id": "risk_report", "label": "กลุ่มเสี่ยง", "icon": "fa-triangle-exclamation"},
            {"id": "manage_logs", "label": "แก้ไขรายการ", "icon": "fa-pen-to-square"},
            {"id": "rules", "label": "เกณฑ์คะแนน", "icon": "fa-list-check"},
            {"id": "settings", "label": "ตั้งค่า", "icon": "fa-gear"},
            {"id": "audit", "label": "Audit", "icon": "fa-shield-halved"},
            {"id": "manage_users", "label": "สิทธิ์", "icon": "fa-user-gear"},
        ]

    if role == "viewer":
        return [
            {"id": "dashboard", "label": "แดชบอร์ด", "icon": "fa-gauge"},
            {"id": "student_report", "label": "รายบุคคล", "icon": "fa-user-graduate"},
            {"id": "daily_report", "label": "วันนี้", "icon": "fa-calendar-day"},
            {"id": "weekly_report", "label": "สัปดาห์", "icon": "fa-calendar-week"},
            {"id": "monthly_report", "label": "เดือน", "icon": "fa-calendar-days"},
        ]

    return [
        {"id": "report", "label": "แจ้งพฤติกรรม", "icon": "fa-paper-plane"},
        {"id": "student_report", "label": "รายบุคคล", "icon": "fa-user-graduate"},
        {"id": "add", "label": "เพิ่มนักเรียน", "icon": "fa-user-plus"},
    ]


# =========================================================
# SETTINGS / REPORT HELPERS
# =========================================================

def get_settings():
    db = require_supabase()
    res = db.table("system_settings").select("*").eq("id", 1).limit(1).execute()

    if res.data:
        return res.data[0]

    default = {
        "id": 1,
        "academic_year": now_bangkok().year + 543,
        "semester": 1,
        "base_score": 100,
        "warning_threshold": 80,
        "risk_threshold": 60,
        "repeat_offense_threshold": 3,
    }

    db.table("system_settings").insert(default).execute()
    return default


def get_period_range(period: str):
    now = now_bangkok()

    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        title = "รายงานวันนี้"

    elif period == "weekly":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        title = "รายงานสัปดาห์นี้"

    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
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

        for bucket, name in [
            (by_offense, offense),
            (by_room, room),
            (by_activity, activity),
            (by_teacher, teacher),
        ]:
            if name not in bucket:
                bucket[name] = {"name": name, "count": 0, "points": 0}
            bucket[name]["count"] += 1
            bucket[name]["points"] += points

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


def calc_student_score(student_id: str, academic_year: Optional[int] = None, semester: Optional[int] = None):
    db = require_supabase()
    settings = get_settings()

    ay = academic_year or settings["academic_year"]
    sem = semester or settings["semester"]

    logs = (
        db.table("behavior_logs")
        .select("*")
        .eq("student_id", student_id)
        .eq("academic_year", ay)
        .eq("semester", sem)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )

    rows = logs.data or []

    total = sum(int(r.get("points_deducted") or 0) for r in rows)
    base_score = int(settings.get("base_score") or 100)
    current_score = base_score + total

    warning_threshold = int(settings.get("warning_threshold") or 80)
    risk_threshold = int(settings.get("risk_threshold") or 60)

    status = "ปกติ"

    if current_score < risk_threshold:
        status = "เสี่ยงสูง"
    elif current_score < warning_threshold:
        status = "เฝ้าระวัง"

    return {
        "base_score": base_score,
        "total_points": total,
        "current_score": current_score,
        "status": status,
        "academic_year": ay,
        "semester": sem,
        "logs": rows,
    }


def build_period_report_text(data: dict):
    summary = data.get("summary") or {}

    lines = [
        f"📊 {data.get('title', 'รายงาน')}",
        f"ช่วงเวลา: {data.get('start', '')[:10]} ถึง {data.get('end', '')[:10]}",
        "-" * 24,
        f"รายการทั้งหมด: {summary.get('total_records', 0)}",
        f"คะแนนหักรวม: {summary.get('total_negative', 0)}",
        f"คะแนนบวกรวม: {summary.get('total_positive', 0)}",
        f"คะแนนสุทธิ: {summary.get('total_points', 0)}",
        "",
        "📌 ความผิดสูงสุด:",
    ]

    for item in (summary.get("by_offense") or [])[:5]:
        lines.append(f"- {item['name']}: {item['count']} รายการ ({item['points']} คะแนน)")

    lines.append("")
    lines.append("🏫 ห้องที่มีรายการสูงสุด:")

    for item in (summary.get("by_room") or [])[:5]:
        lines.append(f"- {item['name']}: {item['count']} รายการ ({item['points']} คะแนน)")

    return "\n".join(lines)


def check_and_alert_risk(records: List[BehaviorRecord], academic_year: int, semester: int):
    db = require_supabase()
    settings = get_settings()

    risk_threshold = int(settings.get("risk_threshold") or 60)
    repeat_threshold = int(settings.get("repeat_offense_threshold") or 3)

    alerts = []

    for record in records:
        score = calc_student_score(record.student_id, academic_year, semester)

        if score["current_score"] < risk_threshold:
            alerts.append(
                f"⚠️ {record.student_name} ({record.room}) คะแนนคงเหลือ {score['current_score']} สถานะ {score['status']}"
            )

        repeat = (
            db.table("behavior_logs")
            .select("id")
            .eq("student_id", record.student_id)
            .eq("offense_name", record.offense_name)
            .eq("academic_year", academic_year)
            .eq("semester", semester)
            .is_("deleted_at", "null")
            .execute()
        )

        repeat_count = len(repeat.data or [])

        if repeat_count >= repeat_threshold:
            alerts.append(
                f"🔁 {record.student_name} ({record.room}) ทำผิดซ้ำ: {record.offense_name} จำนวน {repeat_count} ครั้ง"
            )

    if alerts:
        send_line_message("🚨 แจ้งเตือนนักเรียนกลุ่มเสี่ยง\n" + "\n".join(alerts[:10]))


def risk_students_internal():
    db = require_supabase()
    settings = get_settings()

    students = db.table("students").select("student_id, name, room").limit(5000).execute()

    risks = []

    for student in students.data or []:
        score = calc_student_score(
            student["student_id"],
            settings["academic_year"],
            settings["semester"],
        )

        if score["status"] != "ปกติ":
            risks.append({
                **student,
                "current_score": score["current_score"],
                "total_points": score["total_points"],
                "status": score["status"],
            })

    return sorted(risks, key=lambda x: x["current_score"])


# =========================================================
# BACKEND APIs
# =========================================================

@app.post("/api/auth/check")
def check_user_role(req: AuthRequest):
    try:
        db = require_supabase()

        res = (
            db.table("users")
            .select("*")
            .eq("line_user_id", req.line_user_id)
            .limit(1)
            .execute()
        )

        if res.data:
            user = res.data[0]

            if not user.get("is_active", True):
                return JSONResponse(
                    {"status": "error", "error": "บัญชีนี้ถูกระงับการใช้งาน"},
                    status_code=403,
                )

            db.table("users").update({
                "display_name": req.display_name or user.get("display_name")
            }).eq("line_user_id", req.line_user_id).execute()

            return {
                "status": "success",
                "user": {
                    "line_user_id": user["line_user_id"],
                    "display_name": req.display_name or user.get("display_name"),
                    "role": user["role"],
                },
                "menus": get_menus_by_role(user["role"]),
                "settings": get_settings(),
            }

        db.table("users").insert({
            "line_user_id": req.line_user_id,
            "display_name": req.display_name,
            "role": "user",
            "is_active": True,
        }).execute()

        audit_log(req.line_user_id, "create_user", "users", req.line_user_id)

        return {
            "status": "success",
            "user": {
                "line_user_id": req.line_user_id,
                "display_name": req.display_name,
                "role": "user",
            },
            "menus": get_menus_by_role("user"),
            "settings": get_settings(),
        }

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/init")
def get_init_data():
    try:
        db = require_supabase()

        rules = (
            db.table("offense_rules")
            .select("*")
            .eq("is_active", True)
            .order("id")
            .execute()
        )

        return {
            "status": "success",
            "rules": rules.data or [],
            "settings": get_settings(),
        }

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/students/search")
def search_students(q: str):
    try:
        db = require_supabase()

        res = (
            db.table("students")
            .select("id, student_id, name, room")
            .or_(f"name.ilike.%{q}%,student_id.ilike.%{q}%,room.ilike.%{q}%")
            .limit(30)
            .execute()
        )

        return {"status": "success", "results": res.data or []}

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

        if check.data:
            return JSONResponse(
                {"status": "error", "error": "ข้อมูลนักเรียนนี้มีในระบบแล้ว"},
                status_code=400,
            )

        db.table("students").insert({
            "student_id": student.student_id,
            "name": student.name,
            "room": student.room,
        }).execute()

        return {"status": "success"}

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/report-behavior")
def report_behavior(req: ReportRequest):
    try:
        db = require_supabase()

        role = get_user_role(req.teacher_id)

        if role not in WRITE_ROLES:
            raise HTTPException(status_code=403, detail="Permission denied.")

        if not req.records:
            return JSONResponse(
                {"status": "error", "error": "ไม่มีข้อมูลนักเรียนที่ต้องการแจ้ง"},
                status_code=400,
            )

        settings = get_settings()
        academic_year = req.academic_year or settings["academic_year"]
        semester = req.semester or settings["semester"]
        request_id = req.request_id or str(uuid.uuid4())

        duplicate = (
            db.table("behavior_request_logs")
            .select("request_id")
            .eq("request_id", request_id)
            .limit(1)
            .execute()
        )

        if duplicate.data:
            return {
                "status": "success",
                "duplicate": True,
                "message": "รายการนี้ถูกบันทึกแล้ว",
            }

        now = now_bangkok()
        teacher_name = get_display_name(req.teacher_id)

        log_entries = []

        for record in req.records:
            log_entries.append({
                "student_id": record.student_id,
                "student_name": record.student_name,
                "room": record.room,
                "teacher_id": req.teacher_id,
                "activity_type": req.activity_type,
                "offense_name": record.offense_name,
                "points_deducted": record.points_deducted,
                "reason": record.reason,
                "academic_year": academic_year,
                "semester": semester,
                "request_id": request_id,
                "created_at": now.isoformat(),
            })

        db.table("behavior_logs").insert(log_entries).execute()
        db.table("behavior_request_logs").insert({
            "request_id": request_id,
            "teacher_id": req.teacher_id,
            "created_at": now.isoformat(),
        }).execute()

        audit_log(
            req.teacher_id,
            "create_behavior_logs",
            "behavior_logs",
            request_id,
            {"count": len(log_entries)},
        )

        status_groups: Dict[str, List[BehaviorRecord]] = {}

        for record in req.records:
            status_groups.setdefault(record.offense_name, []).append(record)

        lines = [
            "🚨 แจ้งพฤติกรรม/การเข้าเรียน",
            f"👤 ผู้แจ้ง: {teacher_name}",
            f"📋 กิจกรรม: {req.activity_type}",
            f"📚 ปีการศึกษา/ภาคเรียน: {academic_year}/{semester}",
            f"📅 วันที่: {now.strftime('%d/%m/%Y')}",
            f"⏰ เวลา: {now.strftime('%H:%M น.')}",
            "-" * 24,
        ]

        for offense, students in status_groups.items():
            lines.append(f"\n📌 {offense} ({len(students)} คน):")

            for student in students:
                reason_text = f" | หมายเหตุ: {student.reason}" if student.reason else ""
                lines.append(
                    f"- {student.student_name} ({student.room}) | {student.points_deducted} คะแนน{reason_text}"
                )

        send_line_message("\n".join(lines))
        check_and_alert_risk(req.records, academic_year, semester)

        return {"status": "success", "request_id": request_id}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/student/report")
def student_report(
    line_user_id: str,
    q: str,
    academic_year: Optional[int] = None,
    semester: Optional[int] = None,
):
    try:
        get_user_role(line_user_id)

        db = require_supabase()

        students = (
            db.table("students")
            .select("id, student_id, name, room")
            .or_(f"name.ilike.%{q}%,student_id.ilike.%{q}%")
            .limit(10)
            .execute()
        )

        results = []

        for student in students.data or []:
            score = calc_student_score(student["student_id"], academic_year, semester)
            results.append({
                **student,
                **score,
                "logs": score["logs"][:50],
            })

        return {"status": "success", "results": results}

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# =========================================================
# ADMIN APIS
# =========================================================

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

        return {"status": "success", "users": res.data or []}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/users/update-role")
def update_user_role(req: UpdateRoleRequest):
    try:
        require_admin(req.admin_line_user_id)

        allowed_roles = ["super_admin", "admin", "user", "teacher", "homeroom_teacher", "viewer"]

        if req.role not in allowed_roles:
            return JSONResponse({"status": "error", "error": "Invalid role"}, status_code=400)

        db = require_supabase()

        db.table("users").update({
            "role": req.role,
        }).eq("line_user_id", req.target_line_user_id).execute()

        audit_log(
            req.admin_line_user_id,
            "update_user_role",
            "users",
            req.target_line_user_id,
            {"role": req.role},
        )

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

        db.table("users").update({
            "is_active": req.is_active,
        }).eq("line_user_id", req.target_line_user_id).execute()

        audit_log(
            req.admin_line_user_id,
            "update_user_status",
            "users",
            req.target_line_user_id,
            {"is_active": req.is_active},
        )

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/report-period")
def report_period(
    admin_line_user_id: str,
    period: str,
    room: Optional[str] = None,
    offense: Optional[str] = None,
    activity: Optional[str] = None,
):
    try:
        require_report_view(admin_line_user_id)

        start, end, title = get_period_range(period)

        db = require_supabase()

        query = (
            db.table("behavior_logs")
            .select("*")
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .is_("deleted_at", "null")
        )

        if room:
            query = query.eq("room", room)

        if offense:
            query = query.eq("offense_name", offense)

        if activity:
            query = query.eq("activity_type", activity)

        logs = query.order("created_at", desc=True).execute()
        data = logs.data or []

        return {
            "status": "success",
            "period": period,
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "summary": summarize_logs(data),
            "logs": data[:200],
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/send-report-to-group")
def send_report_to_group(req: SendReportToGroupRequest):
    try:
        require_admin(req.admin_line_user_id)

        start, end, title = get_period_range(req.period)

        db = require_supabase()

        logs = (
            db.table("behavior_logs")
            .select("*")
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .is_("deleted_at", "null")
            .execute()
        )

        data = logs.data or []

        payload = {
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "summary": summarize_logs(data),
        }

        sent = send_line_message(build_period_report_text(payload))

        audit_log(
            req.admin_line_user_id,
            "send_report_to_group",
            "report",
            req.period,
        )

        return {"status": "success", "sent": sent}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/report-summary")
def report_summary(admin_line_user_id: str):
    try:
        require_report_view(admin_line_user_id)

        db = require_supabase()

        logs = (
            db.table("behavior_logs")
            .select("*")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return {"status": "success", "logs": logs.data or []}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/dashboard")
def dashboard(admin_line_user_id: str):
    try:
        require_report_view(admin_line_user_id)

        daily_start, daily_end, _ = get_period_range("daily")

        db = require_supabase()

        today_logs = (
            db.table("behavior_logs")
            .select("*")
            .gte("created_at", daily_start.isoformat())
            .lt("created_at", daily_end.isoformat())
            .is_("deleted_at", "null")
            .execute()
        )

        recent_logs = (
            db.table("behavior_logs")
            .select("*")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(1000)
            .execute()
        )

        risks = risk_students_internal()

        return {
            "status": "success",
            "today": summarize_logs(today_logs.data or []),
            "overall_recent": summarize_logs(recent_logs.data or []),
            "risk_students": risks[:20],
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/risk-students")
def risk_students(admin_line_user_id: str):
    try:
        require_report_view(admin_line_user_id)

        return {
            "status": "success",
            "students": risk_students_internal(),
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/logs")
def admin_logs(admin_line_user_id: str):
    try:
        require_admin(admin_line_user_id)

        db = require_supabase()

        logs = (
            db.table("behavior_logs")
            .select("*")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return {"status": "success", "logs": logs.data or []}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/logs/update")
def update_behavior_log(req: UpdateBehaviorLogRequest):
    try:
        require_admin(req.admin_line_user_id)

        db = require_supabase()

        old = (
            db.table("behavior_logs")
            .select("*")
            .eq("id", req.log_id)
            .limit(1)
            .execute()
        )

        payload: Dict[str, Any] = {}

        for field in ["activity_type", "offense_name", "points_deducted", "reason"]:
            value = getattr(req, field)

            if value is not None:
                payload[field] = value

        payload["updated_at"] = now_bangkok().isoformat()
        payload["updated_by"] = req.admin_line_user_id

        db.table("behavior_logs").update(payload).eq("id", req.log_id).execute()

        audit_log(
            req.admin_line_user_id,
            "update_behavior_log",
            "behavior_logs",
            req.log_id,
            {
                "old": old.data[0] if old.data else None,
                "new": payload,
            },
        )

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/logs/delete")
def delete_behavior_log(req: DeleteBehaviorLogRequest):
    try:
        require_admin(req.admin_line_user_id)

        db = require_supabase()

        old = (
            db.table("behavior_logs")
            .select("*")
            .eq("id", req.log_id)
            .limit(1)
            .execute()
        )

        db.table("behavior_logs").update({
            "deleted_at": now_bangkok().isoformat(),
            "deleted_by": req.admin_line_user_id,
        }).eq("id", req.log_id).execute()

        audit_log(
            req.admin_line_user_id,
            "delete_behavior_log",
            "behavior_logs",
            req.log_id,
            {"old": old.data[0] if old.data else None},
        )

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/rules")
def list_rules(admin_line_user_id: str):
    try:
        require_admin(admin_line_user_id)

        db = require_supabase()

        rules = (
            db.table("offense_rules")
            .select("*")
            .order("id")
            .execute()
        )

        return {"status": "success", "rules": rules.data or []}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/rules/upsert")
def upsert_rule(req: UpsertRuleRequest):
    try:
        require_admin(req.admin_line_user_id)

        db = require_supabase()

        payload = {
            "rule_name": req.rule_name,
            "default_points": req.default_points,
            "require_manual_score": req.require_manual_score,
            "is_active": req.is_active,
        }

        if req.rule_id:
            db.table("offense_rules").update(payload).eq("id", req.rule_id).execute()
            audit_log(req.admin_line_user_id, "update_rule", "offense_rules", str(req.rule_id), payload)
        else:
            db.table("offense_rules").insert(payload).execute()
            audit_log(req.admin_line_user_id, "create_rule", "offense_rules", "", payload)

        return {"status": "success"}

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.post("/api/admin/settings/update")
def update_settings(req: UpdateSettingsRequest):
    try:
        require_admin(req.admin_line_user_id)

        db = require_supabase()

        payload = {
            "academic_year": req.academic_year,
            "semester": req.semester,
            "base_score": req.base_score,
            "warning_threshold": req.warning_threshold,
            "risk_threshold": req.risk_threshold,
            "repeat_offense_threshold": req.repeat_offense_threshold,
            "updated_at": now_bangkok().isoformat(),
            "updated_by": req.admin_line_user_id,
        }

        db.table("system_settings").upsert({
            "id": 1,
            **payload,
        }).execute()

        audit_log(req.admin_line_user_id, "update_settings", "system_settings", "1", payload)

        return {
            "status": "success",
            "settings": get_settings(),
        }

    except HTTPException as e:
        raise e

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


@app.get("/api/admin/audit")
def audit(admin_line_user_id: str):
    try:
        require_report_view(admin_line_user_id)

        db = require_supabase()

        logs = (
            db.table("audit_logs")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return {"status": "success", "logs": logs.data or []}

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
body { font-family: 'Kanit', sans-serif; background: #f0f9ff; }
.tab-content { display: none; }
.tab-content.active { display: block; animation: fadeIn 0.2s; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
.no-scrollbar::-webkit-scrollbar { display: none; }
.no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
.card { background: white; padding: 1rem; border-radius: .75rem; box-shadow: 0 1px 2px #0001; margin-bottom: 1rem; border-left: 4px solid #3b82f6; }
.title { font-weight: 700; color: #374151; margin-bottom: .75rem; }
.input { width: 100%; padding: .75rem; border: 1px solid #d1d5db; border-radius: .75rem; background: #f9fafb; }
.btn { width: 100%; color: white; padding: .75rem; border-radius: .75rem; font-weight: 700; box-shadow: 0 1px 2px #0001; }
</style>
</head>

<body class="pb-10">

<div class="bg-blue-600 text-white p-5 shadow-md rounded-b-3xl mb-4">
  <h1 class="text-xl font-bold"><i class="fa-solid fa-user-shield"></i> ฝ่ายกิจการนักเรียน</h1>
  <p class="text-sm opacity-90">โรงเรียนหารเทารังสีประชาสรรค์</p>
  <p id="user_info" class="text-xs opacity-80 mt-2">กำลังเชื่อมต่อ LINE...</p>
</div>

<div class="px-4">

  <div id="role_menu_box" class="hidden gap-2 bg-white p-1 rounded-xl shadow-sm mb-4 border border-gray-200 overflow-x-auto no-scrollbar"></div>

  <div id="view_landing" class="tab-content active">
    <div class="bg-white p-6 rounded-2xl shadow-sm text-center border-t-4 border-blue-500">
      <div class="text-5xl text-blue-600 mb-4"><i class="fa-solid fa-user-shield"></i></div>
      <h2 class="text-xl font-bold text-gray-800 mb-2">ระบบกิจการนักเรียน</h2>
      <p class="text-sm text-gray-500 mb-5">กดปุ่มด้านล่างเพื่อเข้าสู่ระบบ ระบบจะตรวจสอบสิทธิ์จากบัญชี LINE โดยอัตโนมัติ</p>
      <button onclick="enterSystem()" class="w-full bg-blue-600 hover:bg-blue-700 text-white p-4 rounded-xl font-bold shadow-lg text-lg">
        <i class="fa-solid fa-right-to-bracket"></i> เข้าสู่ระบบกิจการนักเรียน
      </button>
    </div>
  </div>

  <div id="view_dashboard" class="tab-content">
    <div class="card border-blue-500">
      <h2 class="title"><i class="fa-solid fa-gauge text-blue-500"></i> แดชบอร์ดแอดมิน</h2>
      <button onclick="loadDashboard()" class="btn bg-blue-600">โหลดแดชบอร์ด</button>
      <div id="dashboard_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_report" class="tab-content">
    <div class="card border-blue-500">
      <label class="font-bold text-gray-700 mb-2 block"><i class="fa-solid fa-clipboard-list text-blue-500"></i> เลือกประเภทกิจกรรม</label>
      <select id="activity_type" class="input">
        <option value="">-- กรุณาเลือก --</option>
        <option value="กิจกรรมหน้าเสาธง">🇹🇭 กิจกรรมหน้าเสาธง</option>
        <option value="โฮมรูม (Homeroom)">🏠 โฮมรูม (Homeroom)</option>
        <option value="เช็คชื่อเข้าชั้นเรียน">📚 เช็คชื่อเข้าชั้นเรียน</option>
        <option value="ตรวจเวร/จราจร">👮 ตรวจเวร/ความเรียบร้อย</option>
      </select>

      <div class="grid grid-cols-2 gap-2 mt-3">
        <input type="number" id="academic_year" class="input" placeholder="ปีการศึกษา">
        <input type="number" id="semester" class="input" placeholder="ภาคเรียน">
      </div>
    </div>

    <div class="card">
      <label class="font-bold text-gray-700 mb-2 block"><i class="fa-solid fa-magnifying-glass text-blue-500"></i> ค้นหานักเรียน</label>
      <input type="text" id="search_box" onkeyup="searchStudent()" placeholder="พิมพ์ชื่อ หรือ รหัสนักเรียน..." class="input mb-2">
      <div id="search_results" class="hidden border rounded-xl bg-white shadow-sm max-h-48 overflow-y-auto mb-3"></div>

      <div id="selected_student_form" class="hidden mt-3 p-4 bg-blue-50 border border-blue-200 rounded-xl">
        <div id="selected_name" class="font-bold text-lg text-blue-800 mb-3"></div>
        <select id="offense_type" onchange="handleOffenseChange()" class="input mb-3">
          <option value="">-- เลือกความผิด --</option>
        </select>
        <input type="number" id="deduct_score" placeholder="คะแนน เช่น -5" class="input mb-3">
        <input type="text" id="reason" placeholder="หมายเหตุเพิ่มเติม ถ้ามี" class="input mb-3">
        <button type="button" onclick="addToList()" class="btn bg-blue-500">
          <i class="fa-solid fa-plus"></i> เพิ่มลงรายการเตรียมส่ง
        </button>
      </div>
    </div>

    <h3 class="font-bold text-gray-700 mb-2">รายการที่เตรียมแจ้ง (<span id="draft_count">0</span> คน)</h3>
    <div id="draft_list" class="space-y-2 mb-6"></div>

    <button onclick="submitReport()" id="submit_btn" class="w-full bg-green-500 text-white p-4 rounded-xl font-bold shadow-lg">
      <i class="fa-solid fa-paper-plane"></i> ส่งข้อมูลเข้ากลุ่มกิจการนักเรียน
    </button>
  </div>

  <div id="view_student_report" class="tab-content">
    <div class="card border-cyan-500">
      <h2 class="title"><i class="fa-solid fa-user-graduate text-cyan-500"></i> รายงานรายบุคคล</h2>
      <input id="student_report_q" class="input mb-2" placeholder="ค้นหาชื่อ/รหัสนักเรียน">
      <button onclick="loadStudentReport()" class="btn bg-cyan-600">ค้นหารายบุคคล</button>
      <div id="student_report_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_add" class="tab-content">
    <div class="card border-green-500">
      <h2 class="title"><i class="fa-solid fa-user-plus text-green-500"></i> เพิ่มนักเรียนตกหล่น</h2>
      <input type="text" id="new_id" placeholder="รหัสนักเรียน" class="input mb-3">
      <input type="text" id="new_name" placeholder="ชื่อ-นามสกุล" class="input mb-3">
      <input type="text" id="new_room" placeholder="ชั้นเรียน เช่น ม.1/1" class="input mb-4">
      <button onclick="confirmAddStudent()" class="btn bg-green-600">บันทึกข้อมูลนักเรียน</button>
    </div>
  </div>

  <div id="view_daily_report" class="tab-content">
    <div class="card border-sky-500">
      <h2 class="title">รายงานวันนี้</h2>
      <button onclick="loadPeriodReport('daily','daily_report_box')" class="btn bg-sky-600 mb-2">โหลดรายงานวันนี้</button>
      <button onclick="sendReportToGroup('daily')" class="btn bg-green-600">ส่งรายงานวันนี้เข้ากลุ่ม</button>
      <div id="daily_report_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_weekly_report" class="tab-content">
    <div class="card border-indigo-500">
      <h2 class="title">รายงานสัปดาห์นี้</h2>
      <button onclick="loadPeriodReport('weekly','weekly_report_box')" class="btn bg-indigo-600 mb-2">โหลดรายงานสัปดาห์</button>
      <button onclick="sendReportToGroup('weekly')" class="btn bg-green-600">ส่งรายงานสัปดาห์เข้ากลุ่ม</button>
      <div id="weekly_report_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_monthly_report" class="tab-content">
    <div class="card border-teal-500">
      <h2 class="title">รายงานเดือนนี้</h2>
      <button onclick="loadPeriodReport('monthly','monthly_report_box')" class="btn bg-teal-600 mb-2">โหลดรายงานเดือน</button>
      <button onclick="sendReportToGroup('monthly')" class="btn bg-green-600">ส่งรายงานเดือนเข้ากลุ่ม</button>
      <div id="monthly_report_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_risk_report" class="tab-content">
    <div class="card border-red-500">
      <h2 class="title"><i class="fa-solid fa-triangle-exclamation text-red-500"></i> นักเรียนกลุ่มเสี่ยง</h2>
      <button onclick="loadRiskStudents()" class="btn bg-red-600">โหลดกลุ่มเสี่ยง</button>
      <div id="risk_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_manage_logs" class="tab-content">
    <div class="card border-amber-500">
      <h2 class="title">แก้ไข/ลบรายการแจ้ง</h2>
      <button onclick="loadManageLogs()" class="btn bg-amber-600">โหลดรายการล่าสุด</button>
      <div id="manage_logs_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_rules" class="tab-content">
    <div class="card border-lime-500">
      <h2 class="title">จัดการเกณฑ์คะแนน</h2>
      <input id="rule_id" class="input mb-2" placeholder="ID ถ้าแก้ไขรายการเดิม">
      <input id="rule_name" class="input mb-2" placeholder="ชื่อเกณฑ์">
      <input id="rule_points" type="number" class="input mb-2" placeholder="คะแนนเริ่มต้น">
      <label class="text-sm"><input type="checkbox" id="rule_manual"> ระบุคะแนนเอง</label>
      <label class="text-sm ml-3"><input type="checkbox" id="rule_active" checked> เปิดใช้งาน</label>
      <button onclick="saveRule()" class="btn bg-lime-600 mt-3">บันทึกเกณฑ์</button>
      <button onclick="loadRules()" class="btn bg-gray-600 mt-2">โหลดเกณฑ์ทั้งหมด</button>
      <div id="rules_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_settings" class="tab-content">
    <div class="card border-gray-500">
      <h2 class="title">ตั้งค่าระบบ</h2>
      <input id="set_year" type="number" class="input mb-2" placeholder="ปีการศึกษา">
      <input id="set_sem" type="number" class="input mb-2" placeholder="ภาคเรียน">
      <input id="set_base" type="number" class="input mb-2" placeholder="คะแนนตั้งต้น">
      <input id="set_warn" type="number" class="input mb-2" placeholder="เกณฑ์เฝ้าระวัง">
      <input id="set_risk" type="number" class="input mb-2" placeholder="เกณฑ์เสี่ยง">
      <input id="set_repeat" type="number" class="input mb-2" placeholder="ทำผิดซ้ำกี่ครั้งให้เตือน">
      <button onclick="saveSettings()" class="btn bg-gray-700">บันทึกตั้งค่า</button>
    </div>
  </div>

  <div id="view_audit" class="tab-content">
    <div class="card border-slate-500">
      <h2 class="title">Audit Log</h2>
      <button onclick="loadAudit()" class="btn bg-slate-600">โหลด Audit</button>
      <div id="audit_box" class="mt-3"></div>
    </div>
  </div>

  <div id="view_manage_users" class="tab-content">
    <div class="card border-purple-500">
      <h2 class="title">จัดการสิทธิ์ผู้ใช้</h2>
      <button onclick="loadUsers()" class="btn bg-purple-600">โหลดรายชื่อผู้ใช้</button>
      <div id="users_list" class="space-y-2 mt-3"></div>
    </div>
  </div>

</div>

<script>
const LIFF_ID = "__LIFF_ID__";

let USER_ID = "";
let CURRENT_ROLE = "user";
let CURRENT_USER = null;
let SETTINGS = {};
let offenseRules = [];
let currentSelectedStudent = null;
let draftList = [];
let searchTimeout = null;

function el(id) {
  return document.getElementById(id);
}

async function main() {
  try {
    await liff.init({ liffId: LIFF_ID });

    if (!liff.isLoggedIn()) {
      liff.login();
      return;
    }

    const profile = await liff.getProfile();
    USER_ID = profile.userId;

    el("user_info").innerText = `LINE: ${profile.displayName || "-"}`;
    renderDraftList();

  } catch (e) {
    Swal.fire("Error", "ไม่สามารถเชื่อมต่อ LINE LIFF ได้", "error");
  }
}

async function enterSystem() {
  try {
    Swal.fire({
      title: "กำลังตรวจสอบสิทธิ์...",
      allowOutsideClick: false,
      didOpen: () => Swal.showLoading()
    });

    const profile = await liff.getProfile();
    USER_ID = profile.userId;

    const res = await fetch("/api/auth/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        line_user_id: profile.userId,
        display_name: profile.displayName
      })
    });

    const data = await res.json();

    if (data.status === "error") {
      throw new Error(data.error);
    }

    CURRENT_USER = data.user;
    CURRENT_ROLE = data.user.role;
    SETTINGS = data.settings || {};

    fillSettings();

    el("user_info").innerText =
      `ผู้ใช้: ${CURRENT_USER.display_name || "-"} | สิทธิ์: ${CURRENT_ROLE}`;

    renderMenusByRole(data.menus);

    await loadInitData();

    el("role_menu_box").classList.remove("hidden");
    el("role_menu_box").classList.add("flex");

    Swal.close();

    switchTab(CURRENT_ROLE === "user" || CURRENT_ROLE === "teacher" || CURRENT_ROLE === "homeroom_teacher" ? "report" : "dashboard");

  } catch (e) {
    Swal.fire("ข้อผิดพลาด", e.message || "ไม่สามารถตรวจสอบสิทธิ์ได้", "error");
  }
}

function fillSettings() {
  if (el("academic_year")) el("academic_year").value = SETTINGS.academic_year || "";
  if (el("semester")) el("semester").value = SETTINGS.semester || "";

  if (el("set_year")) {
    el("set_year").value = SETTINGS.academic_year || "";
    el("set_sem").value = SETTINGS.semester || "";
    el("set_base").value = SETTINGS.base_score || 100;
    el("set_warn").value = SETTINGS.warning_threshold || 80;
    el("set_risk").value = SETTINGS.risk_threshold || 60;
    el("set_repeat").value = SETTINGS.repeat_offense_threshold || 3;
  }
}

async function loadInitData() {
  const res = await fetch("/api/init");
  const data = await res.json();

  if (data.status === "error") {
    throw new Error(data.error);
  }

  offenseRules = data.rules || [];
  SETTINGS = data.settings || SETTINGS;
  fillSettings();

  const select = el("offense_type");
  select.innerHTML = '<option value="">-- เลือกความผิด --</option>';

  offenseRules.forEach(rule => {
    const opt = document.createElement("option");

    opt.value = `${rule.rule_name}|${rule.default_points !== null ? rule.default_points : 0}|${rule.require_manual_score}`;
    opt.innerText = `${rule.rule_name} ${rule.require_manual_score ? "(ระบุคะแนนเอง)" : `(${rule.default_points})`}`;

    select.appendChild(opt);
  });
}

function renderMenusByRole(menus) {
  el("role_menu_box").innerHTML = menus.map(menu => `
    <button
      onclick="switchTab('${menu.id}')"
      id="nav_${menu.id}"
      class="min-w-[92px] flex-1 py-2 text-xs font-bold text-gray-500 rounded-lg transition-colors"
    >
      <i class="fa-solid ${menu.icon}"></i><br>${menu.label}
    </button>
  `).join("");
}

function switchTab(tab) {
  document.querySelectorAll(".tab-content").forEach(v => v.classList.remove("active"));

  document.querySelectorAll("[id^='nav_']").forEach(n => {
    n.className = "min-w-[92px] flex-1 py-2 text-xs font-bold text-gray-500 rounded-lg transition-colors";
  });

  if (el(`view_${tab}`)) {
    el(`view_${tab}`).classList.add("active");
  }

  if (el(`nav_${tab}`)) {
    el(`nav_${tab}`).className = "min-w-[92px] flex-1 py-2 text-xs font-bold bg-blue-100 text-blue-700 rounded-lg transition-colors";
  }
}

function handleOffenseChange() {
  const select = el("offense_type");
  const scoreInput = el("deduct_score");

  if (!select.value) {
    scoreInput.value = "";
    return;
  }

  const parts = select.value.split("|");
  const defaultScore = parts[1];
  const requireManual = parts[2] === "true";

  if (requireManual) {
    scoreInput.value = "";
    scoreInput.placeholder = "กรุณาระบุคะแนน";
    scoreInput.focus();
  } else {
    scoreInput.value = defaultScore;
  }
}

async function searchStudent() {
  clearTimeout(searchTimeout);

  const query = el("search_box").value.trim();
  const resultsBox = el("search_results");

  if (query.length < 2) {
    resultsBox.classList.add("hidden");
    return;
  }

  searchTimeout = setTimeout(async () => {
    const res = await fetch(`/api/students/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();

    window.latestStudentSearchResults = data.results || [];

    resultsBox.innerHTML = window.latestStudentSearchResults.length
      ? window.latestStudentSearchResults.map((student, index) => `
        <div onclick="selectStudentByIndex(${index})" class="p-3 border-b hover:bg-blue-50 cursor-pointer">
          <b>${escapeHtml(student.student_id)}</b>
          - ${escapeHtml(student.name)}
          <span class="text-sm text-gray-500">(${escapeHtml(student.room)})</span>
        </div>
      `).join("")
      : `<div class="p-3 text-sm text-gray-400">ไม่พบข้อมูลนักเรียน</div>`;

    resultsBox.classList.remove("hidden");

  }, 300);
}

function selectStudentByIndex(index) {
  const student = window.latestStudentSearchResults[index];

  if (student) {
    selectStudent(student.student_id, student.name, student.room);
  }
}

function selectStudent(id, name, room) {
  currentSelectedStudent = {
    student_id: id,
    student_name: name,
    room: room
  };

  el("search_box").value = "";
  el("search_results").classList.add("hidden");
  el("selected_name").innerText = `${name} (${room})`;
  el("selected_student_form").classList.remove("hidden");
}

function addToList() {
  const select = el("offense_type");
  const scoreInput = el("deduct_score");
  const reasonInput = el("reason");

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

  el("selected_student_form").classList.add("hidden");
  select.value = "";
  scoreInput.value = "";
  reasonInput.value = "";

  renderDraftList();
}

function renderDraftList() {
  el("draft_count").innerText = draftList.length;

  const box = el("draft_list");

  if (!draftList.length) {
    box.innerHTML = `<div class="text-gray-400 text-center p-5 bg-white rounded-xl border border-dashed">ยังไม่ได้เลือกนักเรียน</div>`;
    return;
  }

  box.innerHTML = draftList.map((item, index) => `
    <div class="bg-white p-3 border rounded-xl flex justify-between items-center shadow-sm">
      <div>
        <div class="font-bold">
          ${escapeHtml(item.student_name)}
          <span class="text-xs text-gray-500">(${escapeHtml(item.room)})</span>
        </div>
        <div class="text-sm text-red-600">
          ${escapeHtml(item.offense_name)} (${item.points_deducted})
          ${item.reason ? `- ${escapeHtml(item.reason)}` : ""}
        </div>
      </div>
      <button onclick="removeFromList(${index})" class="text-red-500 bg-red-50 p-2 rounded-lg">
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
  const button = el("submit_btn");
  const activityType = el("activity_type").value;

  if (!activityType) {
    return Swal.fire("แจ้งเตือน", "กรุณาเลือกประเภทกิจกรรมก่อน", "warning");
  }

  if (!draftList.length) {
    return Swal.fire("แจ้งเตือน", "กรุณาเพิ่มนักเรียนอย่างน้อย 1 คน", "warning");
  }

  button.disabled = true;

  try {
    Swal.fire({
      title: "กำลังส่งข้อมูล...",
      allowOutsideClick: false,
      didOpen: () => Swal.showLoading()
    });

    const body = {
      teacher_id: USER_ID,
      activity_type: activityType,
      records: draftList,
      request_id: crypto.randomUUID(),
      academic_year: parseInt(el("academic_year").value || SETTINGS.academic_year),
      semester: parseInt(el("semester").value || SETTINGS.semester)
    };

    const res = await fetch("/api/report-behavior", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    const data = await res.json();

    if (data.status === "error") {
      throw new Error(data.error);
    }

    Swal.fire("สำเร็จ", "ส่งข้อมูลเรียบร้อย", "success");

    draftList = [];
    el("activity_type").value = "";

    renderDraftList();

  } catch (e) {
    Swal.fire("ข้อผิดพลาด", e.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function confirmAddStudent() {
  const student_id = el("new_id").value.trim();
  const name = el("new_name").value.trim();
  const room = el("new_room").value.trim();

  if (!student_id || !name || !room) {
    return Swal.fire("แจ้งเตือน", "กรุณากรอกให้ครบ", "warning");
  }

  const data = await post("/api/students/add", { student_id, name, room });

  if (data.status === "success") {
    Swal.fire("สำเร็จ", "เพิ่มนักเรียนแล้ว", "success");
    el("new_id").value = "";
    el("new_name").value = "";
    el("new_room").value = "";
  }
}

async function loadStudentReport() {
  const q = el("student_report_q").value.trim();

  if (!q) return;

  const res = await fetch(`/api/student/report?line_user_id=${encodeURIComponent(USER_ID)}&q=${encodeURIComponent(q)}`);
  const data = await res.json();

  el("student_report_box").innerHTML = (data.results || []).map(student => `
    <div class="bg-gray-50 border rounded-xl p-3 mb-2">
      <b>${escapeHtml(student.name)} (${escapeHtml(student.room)})</b>
      <div>รหัส: ${escapeHtml(student.student_id)}</div>
      <div>คะแนนปัจจุบัน: <b>${student.current_score}</b> | สถานะ: ${escapeHtml(student.status)}</div>
      <div class="text-xs text-gray-500">รายการ ${student.logs.length} รายการ</div>
      ${student.logs.map(log => `
        <div class="border-t mt-2 pt-2 text-sm">
          ${escapeHtml(log.offense_name)} (${log.points_deducted}) | ${escapeHtml(log.created_at || "")}
        </div>
      `).join("")}
    </div>
  `).join("") || "<div class='text-gray-400'>ไม่พบข้อมูล</div>";
}

async function loadUsers() {
  const res = await fetch(`/api/admin/users?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  if (data.status === "error") {
    return Swal.fire("ข้อผิดพลาด", data.error, "error");
  }

  el("users_list").innerHTML = (data.users || []).map(user => `
    <div class="bg-gray-50 border rounded-xl p-3">
      <b>${escapeHtml(user.display_name || "ไม่ระบุ")}</b>
      <div class="text-xs break-all">${escapeHtml(user.line_user_id)}</div>
      <select id="role_${escapeAttr(user.line_user_id)}" class="input my-2">
        <option value="user" ${user.role === "user" ? "selected" : ""}>user</option>
        <option value="teacher" ${user.role === "teacher" ? "selected" : ""}>teacher</option>
        <option value="homeroom_teacher" ${user.role === "homeroom_teacher" ? "selected" : ""}>homeroom_teacher</option>
        <option value="viewer" ${user.role === "viewer" ? "selected" : ""}>viewer</option>
        <option value="admin" ${user.role === "admin" ? "selected" : ""}>admin</option>
        <option value="super_admin" ${user.role === "super_admin" ? "selected" : ""}>super_admin</option>
      </select>
      <button onclick="updateRole('${escapeJs(user.line_user_id)}')" class="btn bg-purple-600 mb-2">บันทึกสิทธิ์</button>
      <button onclick="updateUserStatus('${escapeJs(user.line_user_id)}', ${!user.is_active})" class="btn ${user.is_active ? "bg-red-600" : "bg-green-600"}">
        ${user.is_active ? "ระงับ" : "เปิดใช้"}
      </button>
    </div>
  `).join("");
}

async function updateRole(id) {
  const role = el(`role_${id}`).value;

  await post("/api/admin/users/update-role", {
    admin_line_user_id: USER_ID,
    target_line_user_id: id,
    role
  });

  Swal.fire("สำเร็จ", "อัปเดตสิทธิ์แล้ว", "success");
}

async function updateUserStatus(id, is_active) {
  await post("/api/admin/users/update-status", {
    admin_line_user_id: USER_ID,
    target_line_user_id: id,
    is_active
  });

  loadUsers();
}

async function loadPeriodReport(period, boxId) {
  const res = await fetch(`/api/admin/report-period?admin_line_user_id=${encodeURIComponent(USER_ID)}&period=${period}`);
  const data = await res.json();

  if (data.status === "error") {
    el(boxId).innerHTML = `<div class="text-red-500">${escapeHtml(data.error)}</div>`;
    return;
  }

  el(boxId).innerHTML = renderPeriodReport(data);
}

function renderPeriodReport(data) {
  const summary = data.summary || {};

  return `
    <div class="space-y-3">
      <div class="grid grid-cols-2 gap-2">
        ${summaryCard("รายการ", summary.total_records || 0, "blue")}
        ${summaryCard("หักรวม", summary.total_negative || 0, "red")}
        ${summaryCard("บวกรวม", summary.total_positive || 0, "green")}
        ${summaryCard("สุทธิ", summary.total_points || 0, "gray")}
      </div>
      ${renderSummaryGroup("ความผิด", summary.by_offense)}
      ${renderSummaryGroup("ห้อง", summary.by_room)}
      ${renderSummaryGroup("กิจกรรม", summary.by_activity)}
      <div class="bg-white border rounded-xl p-3">
        <b>รายการล่าสุด</b>
        ${(data.logs || []).map(log => `
          <div class="border-t py-2 text-sm">
            ${escapeHtml(log.student_name || log.student_id)}
            (${escapeHtml(log.room || "-")})
            - ${escapeHtml(log.offense_name || "-")}
            (${log.points_deducted})
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function summaryCard(title, value, color) {
  return `
    <div class="bg-white border rounded-xl p-3 text-center">
      <div class="text-2xl font-bold text-${color}-600">${value}</div>
      <div class="text-xs text-gray-500">${title}</div>
    </div>
  `;
}

function renderSummaryGroup(title, items) {
  return `
    <div class="bg-white border rounded-xl p-3">
      <b>แยกตาม${title}</b>
      ${(items || []).slice(0, 10).map(item => `
        <div class="flex justify-between border-t py-2 text-sm">
          <span>${escapeHtml(item.name)} (${item.count})</span>
          <b>${item.points}</b>
        </div>
      `).join("") || "<div class='text-gray-400'>ไม่มีข้อมูล</div>"}
    </div>
  `;
}

async function sendReportToGroup(period) {
  await post("/api/admin/send-report-to-group", {
    admin_line_user_id: USER_ID,
    period
  });

  Swal.fire("สำเร็จ", "ส่งรายงานเข้ากลุ่มแล้ว", "success");
}

async function loadRiskStudents() {
  const res = await fetch(`/api/admin/risk-students?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  el("risk_box").innerHTML = (data.students || []).map(student => `
    <div class="bg-red-50 border border-red-200 rounded-xl p-3 mb-2">
      <b>${escapeHtml(student.name)} (${escapeHtml(student.room)})</b>
      <div>คะแนน ${student.current_score} | ${escapeHtml(student.status)}</div>
    </div>
  `).join("") || "<div class='text-gray-400'>ไม่มีนักเรียนกลุ่มเสี่ยง</div>";
}

async function loadDashboard() {
  const res = await fetch(`/api/admin/dashboard?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  const today = data.today || {};
  const overall = data.overall_recent || {};

  el("dashboard_box").innerHTML = `
    <div class="grid grid-cols-2 gap-2 mb-3">
      ${summaryCard("วันนี้", today.total_records || 0, "blue")}
      ${summaryCard("หักวันนี้", today.total_negative || 0, "red")}
      ${summaryCard("ล่าสุด 1000", overall.total_records || 0, "gray")}
      ${summaryCard("กลุ่มเสี่ยง", (data.risk_students || []).length, "red")}
    </div>
    ${renderSummaryGroup("ความผิดวันนี้", today.by_offense)}
    ${renderSummaryGroup("ห้องวันนี้", today.by_room)}
  `;
}

async function loadManageLogs() {
  const res = await fetch(`/api/admin/logs?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  el("manage_logs_box").innerHTML = (data.logs || []).map(log => `
    <div class="bg-gray-50 border rounded-xl p-3 mb-2">
      <b>${escapeHtml(log.student_name || log.student_id)} (${escapeHtml(log.room || "-")})</b>
      <input id="off_${log.id}" class="input my-1" value="${escapeAttr(log.offense_name || "")}">
      <input id="pts_${log.id}" type="number" class="input my-1" value="${log.points_deducted || 0}">
      <input id="rea_${log.id}" class="input my-1" value="${escapeAttr(log.reason || "")}">
      <button onclick="updateLog('${log.id}')" class="btn bg-amber-600 mb-1">บันทึกแก้ไข</button>
      <button onclick="deleteLog('${log.id}')" class="btn bg-red-600">ลบรายการ</button>
    </div>
  `).join("");
}

async function updateLog(id) {
  await post("/api/admin/logs/update", {
    admin_line_user_id: USER_ID,
    log_id: id,
    offense_name: el(`off_${id}`).value,
    points_deducted: parseInt(el(`pts_${id}`).value),
    reason: el(`rea_${id}`).value
  });

  Swal.fire("สำเร็จ", "แก้ไขแล้ว", "success");
}

async function deleteLog(id) {
  const confirm = await Swal.fire({
    title: "ยืนยันลบ?",
    showCancelButton: true,
    confirmButtonText: "ลบ",
    cancelButtonText: "ยกเลิก"
  });

  if (!confirm.isConfirmed) return;

  await post("/api/admin/logs/delete", {
    admin_line_user_id: USER_ID,
    log_id: id
  });

  loadManageLogs();
}

async function loadRules() {
  const res = await fetch(`/api/admin/rules?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  el("rules_box").innerHTML = (data.rules || []).map(rule => `
    <div class="border rounded-xl p-2 mb-1">
      ID ${rule.id}: ${escapeHtml(rule.rule_name)} (${rule.default_points}) | ${rule.is_active ? "เปิด" : "ปิด"}
    </div>
  `).join("");
}

async function saveRule() {
  await post("/api/admin/rules/upsert", {
    admin_line_user_id: USER_ID,
    rule_id: el("rule_id").value ? parseInt(el("rule_id").value) : null,
    rule_name: el("rule_name").value,
    default_points: el("rule_points").value ? parseInt(el("rule_points").value) : null,
    require_manual_score: el("rule_manual").checked,
    is_active: el("rule_active").checked
  });

  Swal.fire("สำเร็จ", "บันทึกเกณฑ์แล้ว", "success");

  loadRules();
  await loadInitData();
}

async function saveSettings() {
  const data = await post("/api/admin/settings/update", {
    admin_line_user_id: USER_ID,
    academic_year: parseInt(el("set_year").value),
    semester: parseInt(el("set_sem").value),
    base_score: parseInt(el("set_base").value),
    warning_threshold: parseInt(el("set_warn").value),
    risk_threshold: parseInt(el("set_risk").value),
    repeat_offense_threshold: parseInt(el("set_repeat").value)
  });

  SETTINGS = data.settings || SETTINGS;
  fillSettings();

  Swal.fire("สำเร็จ", "บันทึกตั้งค่าแล้ว", "success");
}

async function loadAudit() {
  const res = await fetch(`/api/admin/audit?admin_line_user_id=${encodeURIComponent(USER_ID)}`);
  const data = await res.json();

  el("audit_box").innerHTML = (data.logs || []).map(audit => `
    <div class="bg-gray-50 border rounded-xl p-2 mb-1 text-xs">
      <b>${escapeHtml(audit.action)}</b> | ${escapeHtml(audit.actor_line_user_id)}
      <br>${escapeHtml(audit.target_type)} ${escapeHtml(audit.target_id)}
      <br>${escapeHtml(audit.created_at)}
    </div>
  `).join("");
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  const data = await res.json();

  if (data.status === "error") {
    throw new Error(data.error);
  }

  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
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
  return String(value ?? "")
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
