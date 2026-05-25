import os
from datetime import datetime
from typing import List, Optional

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

# ID ของกลุ่มไลน์กิจการนักเรียน
STUDENT_AFFAIRS_GROUP_ID = os.environ.get("STUDENT_AFFAIRS_GROUP_ID") 

# LIFF ID
LIFF_ID = os.environ.get("2010184816-R1BNqd1n", "https://liff.line.me/2010184816-R1BNqd1n") 
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

# =========================================================
# BACKEND APIs
# =========================================================

@app.get("/api/init")
def get_init_data():
    """ดึงข้อมูลเริ่มต้นเมื่อเปิด LIFF (ดึงเกณฑ์คะแนน)"""
    try:
        db = require_supabase()
        rules = db.table("offense_rules").select("*").execute()
        return {"status": "success", "rules": rules.data or []}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

@app.get("/api/students/search")
def search_students(q: str):
    """ค้นหารายชื่อนักเรียนจากชื่อ หรือ รหัส"""
    try:
        db = require_supabase()
        res = (
            db.table("students")
            .select("student_id, name, room")
            .or_(f"name.ilike.%{q}%,student_id.ilike.%{q}%")
            .limit(15)
            .execute()
        )
        return {"status": "success", "results": res.data or []}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

@app.post("/api/students/add")
def add_student(student: NewStudent):
    """เพิ่มข้อมูลนักเรียนใหม่เข้าระบบ (ครูทุกคนเพิ่มได้)"""
    try:
        db = require_supabase()
        # เช็คซ้ำ
        check = db.table("students").select("student_id").eq("student_id", student.student_id).execute()
        if len(check.data) > 0:
            return JSONResponse({"status": "error", "error": "รหัสนักเรียนนี้มีในระบบแล้ว"}, status_code=400)
            
        db.table("students").insert({
            "student_id": student.student_id,
            "name": student.name,
            "room": student.room
        }).execute()
        return {"status": "success"}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

@app.post("/api/report-behavior")
def report_behavior(req: ReportRequest):
    """รับข้อมูลการแจ้งพฤติกรรม บันทึก และยิงเข้า LINE กลุ่ม"""
    try:
        db = require_supabase()
        now = now_bangkok()
        
        # ชื่อครูชั่วคราว (หากมีการเชื่อมระบบโปรไฟล์ สามารถดึงชื่อมาใส่แทนได้)
        teacher_name = "คุณครู"
        
        # 1. บันทึกลงตาราง
        log_entries = []
        for r in req.records:
            log_entries.append({
                "student_id": r.student_id,
                "teacher_id": req.teacher_id,
                "activity_type": req.activity_type,
                "offense_name": r.offense_name,
                "points_deducted": r.points_deducted,
                "reason": r.reason,
                "created_at": now.isoformat()
            })
        db.table("behavior_logs").insert(log_entries).execute()

        # 2. จัดกลุ่มข้อความเพื่อส่งเข้า LINE (แยกตามความผิด)
        status_groups = {}
        for r in req.records:
            status_groups.setdefault(r.offense_name, []).append(r)

        lines = [
            f"🚨 แจ้งพฤติกรรม/การเข้าเรียน",
            f"📋 กิจกรรม: {req.activity_type}",
            f"⏰ เวลา: {now.strftime('%H:%M น.')}",
            "-" * 20
        ]

        for offense, students in status_groups.items():
            lines.append(f"\n📌 {offense} ({len(students)} คน):")
            for s in students:
                reason_text = f" ({s.reason})" if s.reason else ""
                lines.append(f" - {s.student_name} ({s.room}) | {s.points_deducted} คะแนน{reason_text}")

        notify_text = "\n".join(lines)

        # 3. ส่งเข้ากลุ่ม LINE
        if line_bot_api and STUDENT_AFFAIRS_GROUP_ID:
            line_bot_api.push_message(STUDENT_AFFAIRS_GROUP_ID, TextSendMessage(text=notify_text))

        return {"status": "success"}

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

# Webhook ไว้สำหรับอ่านค่า Group ID
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature.")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    # ปริ้นท์ Group ID ออกมาใน Console เมื่อมีคนพิมพ์ในกลุ่ม
    if event.source.type == 'group':
        print(f"=== YOUR GROUP ID IS: {event.source.group_id} ===")

# =========================================================
# LIFF FRONTEND (HTML + JS)
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
        .tab-content.active { display: block; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
    </style>
</head>
<body class="pb-10">

    <div class="bg-blue-600 text-white p-5 shadow-md rounded-b-3xl mb-4">
        <h1 class="text-xl font-bold"><i class="fa-solid fa-user-shield"></i> ฝ่ายกิจการนักเรียน</h1>
        <p class="text-sm opacity-90">โรงเรียนหารเทารังสีประชาสรรค์</p>
    </div>

    <div class="px-4">
        <div class="flex space-x-2 bg-white p-1 rounded-xl shadow-sm mb-4 border border-gray-200">
            <button onclick="switchTab('report')" id="nav_report" class="flex-1 py-2 text-sm font-bold bg-blue-100 text-blue-700 rounded-lg transition-colors">แจ้งพฤติกรรม</button>
            <button onclick="switchTab('add')" id="nav_add" class="flex-1 py-2 text-sm font-bold text-gray-500 rounded-lg transition-colors">เพิ่มนักเรียน</button>
        </div>

        <div id="view_report" class="tab-content active">
            
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-t-4 border-blue-500">
                <label class="font-bold text-gray-700 mb-2 block"><i class="fa-solid fa-clipboard-list text-blue-500"></i> เลือกประเภทกิจกรรม</label>
                <select id="activity_type" class="w-full p-3 border rounded-xl bg-gray-50">
                    <option value="">-- กรุณาเลือก --</option>
                    <option value="กิจกรรมหน้าเสาธง">🇹🇭 กิจกรรมหน้าเสาธง</option>
                    <option value="โฮมรูม (Homeroom)">🏠 โฮมรูม (Homeroom)</option>
                    <option value="เช็คชื่อเข้าชั้นเรียน">📚 เช็คชื่อเข้าชั้นเรียน</option>
                    <option value="ตรวจเวร/จราจร">👮 ตรวจเวร/ความเรียบร้อย</option>
                </select>
            </div>

            <div class="bg-white p-4 rounded-xl shadow-sm mb-4">
                <label class="font-bold text-gray-700 mb-2 block"><i class="fa-solid fa-magnifying-glass text-blue-500"></i> ค้นหานักเรียน</label>
                <input type="text" id="search_box" onkeyup="searchStudent()" placeholder="พิมพ์ชื่อ หรือ รหัสนักเรียน..." class="w-full p-3 border rounded-xl bg-gray-50 mb-2">
                <div id="search_results" class="hidden border rounded-xl bg-white shadow-sm max-h-40 overflow-y-auto mb-3"></div>

                <div id="selected_student_form" class="hidden mt-3 p-4 bg-blue-50 border border-blue-200 rounded-xl">
                    <div id="selected_name" class="font-bold text-lg text-blue-800 mb-3"></div>
                    
                    <label class="block text-sm font-bold text-gray-700 mb-1">หมวดหมู่ความผิด</label>
                    <select id="offense_type" onchange="handleOffenseChange()" class="w-full p-3 border rounded-xl bg-white mb-3">
                        <option value="">-- เลือกความผิด --</option>
                    </select>

                    <label class="block text-sm font-bold text-gray-700 mb-1">คะแนน (หักใส่ -, บวกใส่ตัวเลข)</label>
                    <input type="number" id="deduct_score" placeholder="คะแนน เช่น -5" class="w-full p-3 border rounded-xl bg-white mb-3 transition">

                    <input type="text" id="reason" placeholder="หมายเหตุเพิ่มเติม (ถ้ามี)" class="w-full p-3 border rounded-xl bg-white mb-3">
                    
                    <button type="button" onclick="addToList()" class="w-full bg-blue-500 hover:bg-blue-600 text-white p-3 rounded-xl font-bold transition shadow-sm">
                        <i class="fa-solid fa-plus"></i> เพิ่มลงรายการเตรียมส่ง
                    </button>
                </div>
            </div>

            <h3 class="font-bold text-gray-700 mb-2">รายการที่เตรียมแจ้ง (<span id="draft_count">0</span> คน)</h3>
            <div id="draft_list" class="space-y-2 mb-6"></div>

            <button onclick="submitReport()" class="w-full bg-green-500 text-white p-4 rounded-xl font-bold shadow-lg">
                <i class="fa-solid fa-paper-plane"></i> ส่งข้อมูลเข้ากลุ่มกิจการนักเรียน
            </button>
        </div>

        <div id="view_add" class="tab-content">
            <div class="bg-white p-4 rounded-xl shadow-sm mb-4 border-l-4 border-green-500">
                <h2 class="font-bold text-gray-700 mb-3"><i class="fa-solid fa-user-plus text-green-500"></i> เพิ่มนักเรียนตกหล่น</h2>
                <p class="text-xs text-gray-500 mb-4">หากค้นหารายชื่อไม่พบ ครูสามารถเพิ่มข้อมูลนักเรียนเข้าสู่ระบบได้ที่นี่ ข้อมูลจะถูกจัดเก็บทันทีครับ</p>
                
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
    </div>

<script>
    const LIFF_ID = "__LIFF_ID__"; 
    let USER_ID = "";
    let offenseRules = [];
    let currentSelectedStudent = null;
    let draftList = [];

    async function main() {
        try {
            await liff.init({ liffId: LIFF_ID });
            if (!liff.isLoggedIn()) { liff.login(); return; }
            const profile = await liff.getProfile();
            USER_ID = profile.userId;

            // ดึงข้อมูลเกณฑ์คะแนน
            const res = await fetch(`/api/init`);
            const data = await res.json();
            
            offenseRules = data.rules || [];
            const select = document.getElementById("offense_type");
            offenseRules.forEach(rule => {
                const opt = document.createElement("option");
                opt.value = `${rule.rule_name}|${rule.default_points !== null ? rule.default_points : 0}|${rule.require_manual_score}`;
                opt.innerText = `${rule.rule_name} ${rule.require_manual_score ? '(ระบุคะแนนเอง)' : `(${rule.default_points})`}`;
                select.appendChild(opt);
            });
        } catch (e) {
            Swal.fire("Error", "ไม่สามารถเชื่อมต่อระบบได้", "error");
        }
    }
    main();

    // Tab Switcher
    function switchTab(tab) {
        document.getElementById('view_report').classList.remove('active');
        document.getElementById('view_add').classList.remove('active');
        document.getElementById('nav_report').className = "flex-1 py-2 text-sm font-bold text-gray-500 rounded-lg transition-colors";
        document.getElementById('nav_add').className = "flex-1 py-2 text-sm font-bold text-gray-500 rounded-lg transition-colors";
        
        document.getElementById(`view_${tab}`).classList.add('active');
        document.getElementById(`nav_${tab}`).className = "flex-1 py-2 text-sm font-bold bg-blue-100 text-blue-700 rounded-lg transition-colors";
    }

    // จัดการคะแนนอัตโนมัติ/ระบุเอง
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
            scoreInput.placeholder = "⚠️ กรุณาระบุคะแนนที่ต้องการหัก";
            scoreInput.classList.add("border-red-500", "bg-red-50");
            scoreInput.focus();
        } else {
            scoreInput.value = defaultScore;
            scoreInput.classList.remove("border-red-500", "bg-red-50");
        }
    }

    // ค้นหานักเรียน (Debounce)
    let searchTimeout = null;
    async function searchStudent() {
        clearTimeout(searchTimeout);
        const query = document.getElementById("search_box").value;
        const resultsBox = document.getElementById("search_results");
        
        if (query.length < 2) {
            resultsBox.classList.add("hidden");
            return;
        }

        searchTimeout = setTimeout(async () => {
            try {
                const res = await fetch(`/api/students/search?q=${encodeURIComponent(query)}`);
                const data = await res.json();
                
                resultsBox.innerHTML = data.results.map(s => `
                    <div onclick="selectStudent('${s.student_id}', '${s.name}', '${s.room}')" class="p-3 border-b hover:bg-blue-50 cursor-pointer">
                        <span class="font-bold text-gray-800">${s.student_id}</span> - ${s.name} <span class="text-sm text-gray-500">(${s.room})</span>
                    </div>
                `).join("");
                resultsBox.classList.remove("hidden");
            } catch (e) { console.error(e); }
        }, 300);
    }

    function selectStudent(id, name, room) {
        currentSelectedStudent = { student_id: id, student_name: name, room: room };
        document.getElementById("search_box").value = "";
        document.getElementById("search_results").classList.add("hidden");
        document.getElementById("selected_name").innerText = `${name} (${room})`;
        document.getElementById("selected_student_form").classList.remove("hidden");
    }

    function addToList() {
        const select = document.getElementById("offense_type");
        const scoreInput = document.getElementById("deduct_score");
        const reasonInput = document.getElementById("reason");
        
        if (!select.value) return Swal.fire("แจ้งเตือน", "กรุณาเลือกความผิด", "warning");
        
        const parts = select.value.split("|");
        const offenseName = parts[0];
        const requireManual = parts[2] === "true";
        const points = parseInt(scoreInput.value);

        if (requireManual && isNaN(points)) {
            return Swal.fire("แจ้งเตือน", "กรุณาระบุคะแนนด้วยครับ", "warning");
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
        
        if(draftList.length === 0) {
            box.innerHTML = '<div class="text-gray-400 text-center p-5 bg-white rounded-xl border border-dashed">ยังไม่ได้เลือกนักเรียน</div>';
            return;
        }
        
        box.innerHTML = draftList.map((item, index) => `
            <div class="bg-white p-3 border rounded-xl flex justify-between items-center shadow-sm">
                <div>
                    <div class="font-bold text-gray-800">${item.student_name} <span class="text-xs text-gray-500">(${item.room})</span></div>
                    <div class="text-sm text-red-600">${item.offense_name} (${item.points_deducted}) ${item.reason ? `- ${item.reason}` : ''}</div>
                </div>
                <button onclick="removeFromList(${index})" class="text-red-400 hover:text-red-600 bg-red-50 p-2 rounded-lg"><i class="fa-solid fa-trash"></i></button>
            </div>
        `).join("");
    }

    function removeFromList(index) {
        draftList.splice(index, 1);
        renderDraftList();
    }

    async function submitReport() {
        const activityType = document.getElementById("activity_type").value;
        if (!activityType) return Swal.fire("แจ้งเตือน", "กรุณาเลือกประเภทกิจกรรมก่อนครับ", "warning");
        if (draftList.length === 0) return Swal.fire("แจ้งเตือน", "กรุณาเพิ่มนักเรียนอย่างน้อย 1 คน", "warning");
        
        Swal.fire({ title: 'กำลังส่งข้อมูล...', allowOutsideClick: false, didOpen: () => { Swal.showLoading(); } });

        try {
            const res = await fetch("/api/report-behavior", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ teacher_id: USER_ID, activity_type: activityType, records: draftList })
            });
            const data = await res.json();
            if (data.status === "error") throw new Error(data.error);

            Swal.fire("สำเร็จ!", "ส่งข้อมูลเข้ากลุ่มกิจการนักเรียนเรียบร้อย", "success");
            draftList = [];
            document.getElementById("activity_type").value = "";
            renderDraftList();
        } catch (e) {
            Swal.fire("ข้อผิดพลาด", e.message, "error");
        }
    }

    // ==========================================
    // ฟังก์ชันเพิ่มนักเรียน
    // ==========================================
    async function confirmAddStudent() {
        const student_id = document.getElementById("new_id").value.trim();
        const name = document.getElementById("new_name").value.trim();
        const room = document.getElementById("new_room").value.trim();

        if (!student_id || !name || !room) return Swal.fire("แจ้งเตือน", "กรุณากรอกให้ครบทุกช่อง", "warning");

        const result = await Swal.fire({
            title: 'ยืนยันเพิ่มข้อมูล?',
            html: `ชื่อ: <b>${name}</b><br>ห้อง: ${room}<br>รหัส: ${student_id}`,
            icon: 'question',
            showCancelButton: true,
            confirmButtonText: 'ยืนยัน บันทึกเลย!',
            confirmButtonColor: '#16a34a'
        });

        if (result.isConfirmed) {
            try {
                const response = await fetch("/api/students/add", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ student_id, name, room })
                });
                const data = await response.json();
                
                if (data.status === "success") {
                    Swal.fire("สำเร็จ!", "เพิ่มรายชื่อนักเรียนเรียบร้อยแล้ว", "success");
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
    }
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
    return {"status": "running", "service": "Harnthao Rangsi Prachasan - Student Affairs"}
