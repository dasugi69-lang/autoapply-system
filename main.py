from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
import json
import os
import re
import hashlib
import jwt
from datetime import datetime, timedelta
from pathlib import Path

# Try importing optional libraries
try:
    import PyPDF2
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ==================== CONFIG ====================
SECRET_KEY = os.getenv("SECRET_KEY", "autoapply-secret-key-2026-change-in-production")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

app = FastAPI(title="AutoApply AI", version="2.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==================== DATABASE ====================
DB_PATH = os.getenv("DATABASE_PATH", "autoapply.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cvs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_name TEXT,
            raw_text TEXT,
            extracted_data TEXT,
            skills TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            description TEXT,
            requirements TEXT,
            skills TEXT,
            salary TEXT,
            source TEXT,
            posted_date TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            cv_id INTEGER,
            status TEXT DEFAULT 'draft',
            applied_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY,
            user_id INTEGER UNIQUE NOT NULL,
            openai_key TEXT,
            max_daily INTEGER DEFAULT 10,
            min_match INTEGER DEFAULT 70,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()

init_db()

# ==================== AUTH HELPERS ====================

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        token = authorization.split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# ==================== CV ANALYSIS ====================

class CVData(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    skills: List[str] = []
    experience_years: Optional[int] = None
    education: List[str] = []
    languages: List[str] = []
    summary: Optional[str] = None

TECH_SKILLS = [
    "Python", "JavaScript", "TypeScript", "React", "Node.js", "Vue", "Angular", "Next.js",
    "Java", "C++", "C#", "Go", "Rust", "PHP", "Ruby", "Swift", "Kotlin", "Dart", "Flutter",
    "SQL", "PostgreSQL", "MongoDB", "MySQL", "Redis", "Elasticsearch",
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform",
    "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch", "NLP", "Data Science",
    "Git", "CI/CD", "Linux", "HTML", "CSS", "Tailwind", "REST API", "GraphQL",
    "Agile", "Scrum", "Jira", "Figma", "Excel"
]

def extract_cv_regex(text: str) -> CVData:
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    name = lines[0] if lines else "Unknown"
    name = re.sub(r'^(CV|Resume|السيرة)[\s:]*', '', name, flags=re.I).strip()

    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    email = email_match.group(0) if email_match else None

    phone_match = re.search(r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}', text)
    phone = phone_match.group(0) if phone_match else None

    exp_match = re.search(r'(\d+)\+?\s*(?:years?|سنوات?|خبرة)', text, re.I)
    exp = int(exp_match.group(1)) if exp_match else None

    found_skills = [s for s in TECH_SKILLS if s.lower() in text.lower()]

    edu_keywords = ["بكالوريوس", "ماجستير", "دكتوراه", "Bachelor", "Master", "PhD", "MBA"]
    education = [l for l in lines if any(k in l for k in edu_keywords)]

    languages = []
    if any(k in text for k in ["Arabic", "العربية"]): languages.append("Arabic")
    if any(k in text for k in ["English", "الإنجليزية"]): languages.append("English")
    if any(k in text for k in ["French", "الفرنسية"]): languages.append("French")
    if not languages: languages = ["Arabic", "English"]

    title = "Software Developer"
    if any(s in found_skills for s in ["React", "Vue", "Angular"]): title = "Frontend Developer"
    elif any(s in found_skills for s in ["Node.js", "Python", "Java", "Go"]): title = "Backend Developer"
    elif any(s in found_skills for s in ["Machine Learning", "TensorFlow", "PyTorch"]): title = "ML Engineer"
    elif any(s in found_skills for s in ["AWS", "Docker", "Kubernetes"]): title = "DevOps Engineer"
    elif any(s in found_skills for s in ["Flutter", "Swift", "Kotlin"]): title = "Mobile Developer"

    return CVData(
        name=name, title=title, email=email, phone=phone,
        skills=found_skills, experience_years=exp,
        education=education, languages=languages,
        summary=f"{title} with {exp or 'several'} years of experience in {', '.join(found_skills[:5])}"
    )

# ==================== DEMO JOBS ====================

DEMO_JOBS = [
    {
        "title": "Senior Full Stack Developer",
        "company": "TechFlow Solutions",
        "location": "Riyadh, Saudi Arabia",
        "description": "We are seeking an experienced Full Stack Developer to lead our development team and build scalable web applications.",
        "requirements": "5+ years experience with React, Node.js, and cloud platforms",
        "skills": json.dumps(["React", "Node.js", "TypeScript", "AWS", "PostgreSQL", "Docker"]),
        "salary": "25,000 - 35,000 SAR",
        "source": "LinkedIn",
        "posted_date": "2026-06-15"
    },
    {
        "title": "AI/ML Engineer",
        "company": "DataMind AI",
        "location": "Dubai, UAE",
        "description": "Join our AI research team to build next-generation machine learning solutions for enterprise clients.",
        "requirements": "3+ years in machine learning, Python, TensorFlow or PyTorch",
        "skills": json.dumps(["Python", "Machine Learning", "TensorFlow", "PyTorch", "NLP", "Computer Vision"]),
        "salary": "30,000 - 45,000 AED",
        "source": "Indeed",
        "posted_date": "2026-06-14"
    },
    {
        "title": "DevOps Engineer",
        "company": "CloudFirst Technologies",
        "location": "Remote",
        "description": "Manage and optimize our cloud infrastructure and CI/CD pipelines for microservices architecture.",
        "requirements": "4+ years with Kubernetes, Docker, AWS, and Infrastructure as Code",
        "skills": json.dumps(["Kubernetes", "Docker", "AWS", "Terraform", "CI/CD", "Linux", "Python"]),
        "salary": "$80,000 - $120,000",
        "source": "LinkedIn",
        "posted_date": "2026-06-13"
    },
    {
        "title": "Mobile Developer (Flutter)",
        "company": "AppVision Studio",
        "location": "Jeddah, Saudi Arabia",
        "description": "Build beautiful cross-platform mobile applications for our clients in the MENA region.",
        "requirements": "2+ years Flutter experience, strong Dart skills, Firebase knowledge",
        "skills": json.dumps(["Flutter", "Dart", "Firebase", "REST API", "Git", "UI/UX"]),
        "salary": "18,000 - 25,000 SAR",
        "source": "Bayt",
        "posted_date": "2026-06-12"
    },
    {
        "title": "Backend Engineer - Python",
        "company": "ScaleUp Inc",
        "location": "Cairo, Egypt",
        "description": "Design and implement scalable backend services and APIs for our fintech platform.",
        "requirements": "4+ years Python, FastAPI/Django, PostgreSQL, Redis experience",
        "skills": json.dumps(["Python", "FastAPI", "Django", "PostgreSQL", "Redis", "Docker", "Microservices"]),
        "salary": "25,000 - 40,000 EGP",
        "source": "Indeed",
        "posted_date": "2026-06-11"
    },
    {
        "title": "Frontend Tech Lead",
        "company": "Digital Innovation Hub",
        "location": "Abu Dhabi, UAE",
        "description": "Lead frontend architecture decisions and mentor junior developers in a growing team.",
        "requirements": "7+ years frontend, 2+ years leadership, React/Vue expertise",
        "skills": json.dumps(["React", "Vue", "TypeScript", "Architecture", "Leadership", "Agile", "Mentoring"]),
        "salary": "35,000 - 50,000 AED",
        "source": "LinkedIn",
        "posted_date": "2026-06-10"
    }
]

# ==================== API ENDPOINTS ====================

@app.get("/")
async def root():
    return {"service": "AutoApply AI", "version": "2.0.0", "status": "running"}

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# Serve frontend
@app.get("/app")
async def serve_app():
    return FileResponse("static/index.html")

# Auth
@app.post("/auth/register")
async def register(data: dict):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (data.get("email"),))
    if c.fetchone():
        conn.close()
        raise HTTPException(400, "Email already registered")

    c.execute("INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
              (data["email"], hash_password(data["password"]), data.get("name")))
    user_id = c.lastrowid
    c.execute("INSERT INTO settings (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    token = create_token(user_id)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user_id, "email": data["email"], "name": data.get("name")}}

@app.post("/auth/login")
async def login(data: dict):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, email, name, password_hash FROM users WHERE email = ? AND is_active = 1", (data.get("email"),))
    user = c.fetchone()
    conn.close()

    if not user or user["password_hash"] != hash_password(data.get("password", "")):
        raise HTTPException(401, "Invalid credentials")

    token = create_token(user["id"])
    return {"access_token": token, "token_type": "bearer", "user": {"id": user["id"], "email": user["email"], "name": user["name"]}}

# CV Upload
@app.post("/cv/upload")
async def upload_cv(file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    upload_dir = Path("uploads")
    upload_dir.mkdir(exist_ok=True)
    file_path = upload_dir / f"{datetime.now().timestamp()}_{file.filename}"

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    raw_text = ""
    if file.filename.endswith('.pdf') and PDF_AVAILABLE:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                raw_text += page.extract_text() + "\n"
    elif file.filename.endswith('.docx') and DOCX_AVAILABLE:
        doc = Document(file_path)
        for para in doc.paragraphs:
            raw_text += para.text + "\n"
    else:
        raw_text = content.decode('utf-8', errors='ignore')

    extracted = extract_cv_regex(raw_text)

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO cvs (user_id, file_name, raw_text, extracted_data, skills)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, file.filename, raw_text, json.dumps(extracted.dict(), ensure_ascii=False),
          json.dumps(extracted.skills, ensure_ascii=False)))
    cv_id = c.lastrowid
    conn.commit()
    conn.close()

    return {"cv_id": cv_id, "raw_text": raw_text[:500] + "...", "extracted": extracted}

@app.get("/cv/list")
async def list_cvs(user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, file_name, extracted_data, created_at FROM cvs WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r["id"], "file_name": r["file_name"], "extracted": json.loads(r["extracted_data"]) if r["extracted_data"] else {}, "created_at": r["created_at"]} for r in rows]

# Jobs
@app.get("/jobs/search")
async def search_jobs(skills: str = "", min_match: int = 50, user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE is_active = 1 ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    user_skills = [s.strip().lower() for s in skills.split(",") if s.strip()]
    if not user_skills:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT skills FROM cvs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
        cv_row = c.fetchone()
        conn.close()
        if cv_row and cv_row["skills"]:
            user_skills = [s.lower() for s in json.loads(cv_row["skills"])]

    jobs = []
    for row in rows:
        job_skills = json.loads(row["skills"]) if row["skills"] else []
        match_score = None
        if user_skills:
            job_skills_lower = [s.lower() for s in job_skills]
            matched = sum(1 for s in user_skills if any(s in js for js in job_skills_lower))
            match_score = int((matched / len(user_skills)) * 100) if user_skills else 0
            if match_score < min_match:
                continue

        jobs.append({
            "id": row["id"], "title": row["title"], "company": row["company"],
            "location": row["location"], "description": row["description"],
            "requirements": row["requirements"], "skills": job_skills,
            "salary": row["salary"], "source": row["source"],
            "posted_date": row["posted_date"], "match_score": match_score
        })

    if user_skills:
        jobs.sort(key=lambda x: x["match_score"] or 0, reverse=True)
    return jobs

@app.post("/jobs/scrape")
async def scrape_jobs(user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    inserted = 0
    for job in DEMO_JOBS:
        c.execute("""
            INSERT OR IGNORE INTO jobs (title, company, location, description, requirements, skills, salary, source, posted_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job["title"], job.get("company"), job.get("location"), job.get("description"),
              job.get("requirements"), job.get("skills"), job.get("salary"), job["source"], job.get("posted_date")))
        if c.rowcount > 0:
            inserted += 1
    conn.commit()
    conn.close()
    return {"message": f"Added {inserted} new jobs", "total": inserted}

# CV Customization
@app.post("/cv/customize")
async def customize_cv(data: dict, user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT extracted_data FROM cvs WHERE id = ? AND user_id = ?", (data["cv_id"], user_id))
    cv_row = c.fetchone()
    c.execute("SELECT * FROM jobs WHERE id = ?", (data["job_id"],))
    job_row = c.fetchone()
    conn.close()

    if not cv_row or not job_row:
        raise HTTPException(404, "CV or Job not found")

    cv_data = CVData(**json.loads(cv_row["extracted_data"]))
    job_skills = json.loads(job_row["skills"]) if job_row["skills"] else []

    matching = [s for s in job_skills if any(s.lower() in us.lower() for us in cv_data.skills)]
    missing = [s for s in job_skills if s not in matching]

    summary = f"""Experienced {cv_data.title} with {cv_data.experience_years or 'several'} years of expertise in {', '.join(matching[:3]) if matching else 'software development'}.

Proven track record building scalable applications. Passionate about delivering high-quality solutions.{f" Currently expanding skills in {missing[0]}." if missing else ""}"""

    cover = f"""Dear Hiring Manager,

I am writing to express my strong interest in the {job_row['title']} position at {job_row.get('company', 'your company')}.

With my background in {', '.join(cv_data.skills[:4])}, I am confident in my ability to contribute effectively to your team.

My experience aligns well with your requirements:
{chr(10).join([f"• Proficient in {skill}" for skill in matching[:5]])}

I am particularly excited about the opportunity to work with {job_skills[0] if job_skills else 'your team'}.

Thank you for considering my application. I look forward to discussing how I can add value.

Best regards,
{cv_data.name or 'Applicant'}"""

    return {
        "original_summary": cv_data.summary or "",
        "customized_summary": summary,
        "highlighted_skills": matching,
        "missing_skills": missing,
        "suggested_additions": missing[:3],
        "cover_letter": cover,
        "match_improvement": min(95, len(matching) * 15 + 50),
        "ai_model_used": "fallback"
    }

# Applications
@app.post("/applications/apply")
async def create_application(data: dict, user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as count FROM applications WHERE user_id = ? AND DATE(created_at) = DATE('now')", (user_id,))
    daily = c.fetchone()["count"]
    c.execute("SELECT max_daily FROM settings WHERE user_id = ?", (user_id,))
    max_daily = c.fetchone()
    max_daily = max_daily["max_daily"] if max_daily else 10

    if daily >= max_daily:
        conn.close()
        raise HTTPException(429, f"Daily limit reached: {max_daily}")

    c.execute("""
        INSERT INTO applications (user_id, job_id, cv_id, status, notes)
        VALUES (?, ?, ?, 'draft', ?)
    """, (user_id, data["job_id"], data.get("cv_id"), data.get("notes", "")))
    app_id = c.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": app_id,
        "status": "draft",
        "message": "Application created. Review before sending.",
        "daily_remaining": max_daily - daily - 1
    }

@app.get("/applications")
async def get_applications(user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT a.*, j.title as job_title, j.company, j.source
        FROM applications a JOIN jobs j ON a.job_id = j.id
        WHERE a.user_id = ? ORDER BY a.created_at DESC
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r["id"], "job_title": r["job_title"], "company": r["company"], "source": r["source"], "status": r["status"], "applied_at": r["applied_at"]} for r in rows]

@app.post("/applications/{app_id}/approve")
async def approve(app_id: int, user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE applications SET status = 'ready_to_send', applied_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", (app_id, user_id))
    conn.commit()
    conn.close()
    return {"status": "ready_to_send", "message": "Approved. Apply manually on the job site."}

# Settings
@app.get("/settings")
async def get_settings(user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {"max_daily": 10, "min_match": 70, "api_configured": False}
    return {"max_daily": row["max_daily"], "min_match": row["min_match"], "api_configured": bool(row["openai_key"])}

@app.put("/settings")
async def update_settings(data: dict, user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO settings (id, user_id, openai_key, max_daily, min_match)
        VALUES ((SELECT id FROM settings WHERE user_id = ?), ?, ?, ?, ?)
    """, (user_id, user_id, data.get("openai_key"), data.get("max_daily", 10), data.get("min_match", 70)))
    conn.commit()
    conn.close()
    return {"message": "Settings updated"}

# Stats
@app.get("/stats")
async def get_stats(user_id: int = Depends(get_current_user)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as count FROM jobs WHERE is_active = 1")
    total_jobs = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) as count FROM applications WHERE user_id = ?", (user_id,))
    total_apps = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) as count FROM applications WHERE user_id = ? AND DATE(created_at) = DATE('now')", (user_id,))
    today_apps = c.fetchone()["count"]
    c.execute("SELECT COUNT(*) as count FROM applications WHERE user_id = ? AND response_status IS NOT NULL", (user_id,))
    responses = c.fetchone()["count"]
    rate = int((responses / total_apps) * 100) if total_apps > 0 else 0
    conn.close()
    return {"total_jobs": total_jobs, "total_applications": total_apps, "today_applications": today_apps, "response_rate": rate}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
