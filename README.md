# Gettik — Complete Deployment Guide
### Roman Urdu mein — Hostinger Premium (Frontend) + Render.com (Backend)

---

## Yeh Project Kya Hai?

**Gettik** ek TikTok video downloader platform hai jisme:
- **FastAPI Python backend** — video download karna, user auth, admin panel API
- **Static HTML/CSS/JS frontend** — browser mein chalne wala UI
- **Admin panel** — users, licenses, downloads manage karna

Is project ko **2 jagah deploy karna hoga**:

| Cheez | Kahan Deploy Hogi | Folder |
|-------|--------------------|--------|
| Frontend + Admin Panel | **Hostinger Premium** (Shared Hosting) | `gettik-frontend/` |
| FastAPI Backend | **Render.com** (Cloud Platform) | `gettik-backend/` |

---

## Project Structure (Actual Files)

```
Gettik-Hostinger-Backend/
│
├── gettik-backend/                  ← Render.com par deploy hoga
│   ├── render.yaml                  ← Render deployment config
│   ├── .env.render.example          ← Environment variables guide
│   ├── requirements.txt             ← Python dependencies
│   ├── gunicorn.conf.py             ← Production server config
│   ├── gettik-backend.service       ← (VPS k liye, Render par zarori nahi)
│   ├── nginx.conf.example           ← (VPS k liye, Render par zarori nahi)
│   └── src/
│       ├── main.py                  ← FastAPI app entry point
│       └── app/
│           ├── config.py            ← Settings aur env vars
│           ├── database/db.py       ← SQLite database
│           ├── routes/              ← API endpoints
│           ├── services/            ← Business logic
│           └── workers/             ← Background download workers
│
└── gettik-frontend/                 ← Hostinger public_html/gettik/ mein jayega
    ├── config.js                    ← ⭐ SIRF YAHAN Render URL change karo
    ├── index.html                   ← Main app
    ├── .htaccess                    ← SPA routing (zarori hai)
    ├── css/                         ← Stylesheets
    ├── js/                          ← JavaScript files
    └── admin/
        ├── config.js                ← Admin panel Render URL
        ├── index.html               ← Admin panel
        ├── .htaccess
        ├── css/
        └── js/
```

---

## Part 1 — Render.com par Backend Deploy Karna

### Step 1: GitHub par Code Upload Karo

Pehle `gettik-backend/` folder ko GitHub par push karo:

```bash
# Agar Git nahi hai to pehle install karo: https://git-scm.com/

# gettik-backend folder mein jao
cd gettik-backend

# Git initialize karo
git init

# Sab files add karo
git add .

# Pehla commit banao
git commit -m "Initial Gettik backend deployment"

# GitHub par nayi repository banao: https://github.com/new
# Phir remote add karo (apna username aur repo name daalo)
git remote add origin https://github.com/YOUR-USERNAME/gettik-backend.git

# Push karo
git branch -M main
git push -u origin main
```

### Step 2: Render.com par Account Banao

1. **[render.com](https://render.com)** par jao
2. **"Get Started for Free"** click karo
3. GitHub se sign up karo (isi GitHub account se jo upar use kiya)

### Step 3: Render par Web Service Banao

1. Render Dashboard mein **"New +"** button click karo
2. **"Web Service"** select karo
3. **"Connect a repository"** → apna `gettik-backend` repo select karo
4. Yeh settings fill karo:

| Setting | Value |
|---------|-------|
| **Name** | `gettik-backend` (ya jo chahte ho) |
| **Region** | Singapore (Asia k liye best) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install --upgrade pip && pip install -r requirements.txt` |
| **Start Command** | `gunicorn -c gunicorn.conf.py src.main:app` |

5. **"Advanced"** section mein **Health Check Path:** `/health` daalo

6. Plan: **Free** (testing k liye) ya **Starter $7/mo** (production k liye)

### Step 4: Environment Variables Set Karo

Render mein **"Environment"** tab par yeh variables add karo:

```
ENVIRONMENT          = production
DEBUG                = false
APP_HOST             = 0.0.0.0
APP_PORT             = 8000
DATABASE_URL         = sqlite:///data/gettik.db
GETTIK_DOWNLOAD_WORKERS = 3
DOWNLOAD_STORAGE_PATH   = ./data/downloads
JWT_SECRET           = (yahan apna random 32+ char secret daalo — neeche generate karna bataya hai)
SESSION_SECRET       = (yahan dusra alag random 32+ char secret daalo)
```

**JWT_SECRET generate karne ka tarika:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Jo output aaye woh copy karo aur JWT_SECRET mein paste karo. Dobaara chalaao aur SESSION_SECRET k liye alag value use karo.

> **Note:** `API_BASE_URL`, `ADMIN_BASE_URL`, aur `CORS_ORIGINS` deploy hone ke BAAD update karo jab URL pata ho.

### Step 5: Deploy Karo

**"Create Web Service"** button click karo. Render automatically:
1. GitHub se code pull karega
2. `pip install -r requirements.txt` chalayega
3. `gunicorn -c gunicorn.conf.py src.main:app` se server start karega

Deploy hone mein **3-5 minute** lagte hain. Logs mein yeh dikhna chahiye:
```
[INFO] Starting gunicorn
[INFO] Uvicorn running on http://0.0.0.0:8000
```

### Step 6: Backend URL Note Karo

Deploy hone ke baad Render ek URL dega jaise:
```
https://gettik-backend-xxxx.onrender.com
```

Yeh URL notebook mein likh lo — aage zaroorat padegi.

### Step 7: Health Check Karo

Browser mein yeh URL kholo:
```
https://YOUR-APP-NAME.onrender.com/health
```

Yeh JSON response aana chahiye:
```json
{
  "status": "healthy",
  "app": "Gettik",
  "version": "2.2.0",
  "database": {"status": "healthy"},
  "storage": {"status": "healthy"}
}
```

Agar `"status": "healthy"` aa raha hai to backend successfully deploy ho gaya! ✅

### Step 8: CORS Origins Update Karo

Ab backend URL pata hai to Render Dashboard mein Environment Variables mein update karo:

```
API_BASE_URL    = https://YOUR-APP-NAME.onrender.com
ADMIN_BASE_URL  = https://YOUR-APP-NAME.onrender.com/admin
CORS_ORIGINS    = https://YOUR-APP-NAME.onrender.com,https://yourdomain.com,http://localhost:8000
```

`yourdomain.com` ki jagah apna actual Hostinger domain daalo.

Save ke baad Render automatically redeploy karega.

---

## Part 2 — Hostinger Premium par Frontend Deploy Karna

### Step 1: config.js Files Update Karo

**File 1:** `gettik-frontend/config.js` open karo aur Render URL daalo:
```javascript
window.GETTIK_API_BASE_URL = 'https://YOUR-APP-NAME.onrender.com';
```

**File 2:** `gettik-frontend/admin/config.js` bhi update karo:
```javascript
window.GETTIK_ADMIN_API_BASE_URL = 'https://YOUR-APP-NAME.onrender.com';
window.GETTIK_API_BASE_URL = 'https://YOUR-APP-NAME.onrender.com';
```

### Step 2: Hostinger File Manager Se Upload Karo

1. **Hostinger hPanel** → **File Manager** open karo
2. `public_html/` folder mein jao
3. Nayi folder banao: `gettik` (ya jo naam chahte ho)
4. `gettik-frontend/` ki **sab files** upload karo

Upload ke baad structure aisa hona chahiye:
```
public_html/
└── gettik/
    ├── .htaccess          ← Hidden file — zaroor upload karo
    ├── config.js          ← Render URL updated wala
    ├── index.html
    ├── css/
    ├── js/
    └── admin/
        ├── .htaccess      ← Admin k liye bhi zarori
        ├── config.js
        ├── index.html
        ├── css/
        └── js/
```

> **Important:** `.htaccess` ek hidden file hai. FTP client (FileZilla) use kar rahe ho to
> `View → Show Hidden Files` enable karo. Hostinger File Manager automatically dikhata hai.

### Step 3: FTP se Upload (Alternative)

Agar File Manager se mushkil ho to FileZilla use karo:

1. **FileZilla** download karo: [filezilla-project.org](https://filezilla-project.org)
2. Hostinger hPanel → **FTP Accounts** se credentials lo
3. FileZilla mein connect karo:
   - Host: `ftp.yourdomain.com`
   - Username: FTP account username
   - Password: FTP password
   - Port: `21`
4. Remote site mein `public_html/gettik/` folder banao
5. Local `gettik-frontend/` ki sab files wahan upload karo

### Step 4: Frontend Test Karo

Browser mein kholo:
```
https://yourdomain.com/gettik/
```

Login page aana chahiye. Test login try karo:
- Pehle admin panel se user banana hoga

---

## Hostinger aur Render Ko Connect Karna

### Kaise Connect Hote Hain?

```
Browser → yourdomain.com/gettik/ (Hostinger)
              ↓ (JavaScript fetch requests)
API Calls → YOUR-APP.onrender.com/api/... (Render)
```

**Connection ka flow:**
1. User `yourdomain.com/gettik/` kholta hai → Hostinger HTML/CSS/JS deta hai
2. `config.js` load hoti hai → `window.GETTIK_API_BASE_URL` set hota hai
3. Login button click → `js/api.js` Render par `POST /api/auth/login` bhejta hai
4. Render backend response deta hai → JWT token browser mein save hota hai
5. Har baad request → Render backend se data aata hai

### Connection Test Karne Ka Tarika

Browser Console (F12 → Console) mein yeh run karo:
```javascript
// Check karo ke URL set hai ya nahi
console.log(window.GETTIK_API_BASE_URL);
// Output hona chahiye: https://YOUR-APP.onrender.com

// Manual API test
fetch(window.GETTIK_API_BASE_URL + '/health')
  .then(r => r.json())
  .then(d => console.log('Backend status:', d.status));
// Output: Backend status: healthy
```

---

## Environment Variables — Complete Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENVIRONMENT` | Haan | `development` | `production` ya `development` |
| `DEBUG` | Haan | `false` | Production mein `false` rakhna zaroori |
| `APP_HOST` | Haan | `127.0.0.1` | Render par `0.0.0.0` |
| `APP_PORT` | Nahi | `8000` | Server port |
| `DATABASE_URL` | Nahi | SQLite | `sqlite:///data/gettik.db` ya PostgreSQL URL |
| `REDIS_URL` | Nahi | — | Redis URL (nahi diya to memory queue use hogi) |
| `JWT_SECRET` | Haan | — | Login tokens k liye secret key (32+ chars) |
| `SESSION_SECRET` | Haan | — | Session security k liye (32+ chars) |
| `CORS_ORIGINS` | Haan | localhost | Comma-separated allowed domains |
| `API_BASE_URL` | Haan | — | Apna Render URL |
| `ADMIN_BASE_URL` | Nahi | — | Admin panel URL |
| `GETTIK_DOWNLOAD_WORKERS` | Nahi | `5` | Parallel download threads (Render free: 3) |
| `DOWNLOAD_STORAGE_PATH` | Nahi | `~/Downloads/Gettik` | Downloads folder path |
| `GUNICORN_WORKERS` | Nahi | `2` | Gunicorn process count |

---

## Useful Commands

### Local Development (Testing)

```bash
# gettik-backend folder mein jao
cd gettik-backend

# Virtual environment banao (pehli baar)
python -m venv venv

# Virtual environment activate karo
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Dependencies install karo
pip install -r requirements.txt

# Environment check karo (yt-dlp aur FFmpeg verify)
python src/main.py --check-env

# Local server start karo
python src/main.py

# Custom port par chalao
python src/main.py --port 8080

# Development mode (auto-reload)
python src/main.py --reload
```

### Git Commands (Code Update Karne K Liye)

```bash
# Changes add karo
git add .

# Commit karo
git commit -m "Update: jo bhi change kiya"

# GitHub push karo (Render automatically redeploy hoga)
git push origin main
```

### Secret Keys Generate Karna

```bash
# JWT_SECRET k liye
python -c "import secrets; print(secrets.token_hex(32))"

# SESSION_SECRET k liye (alag run karo)
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Troubleshooting

### ❌ Problem: Render deploy fail ho raha hai

**Check karo:**
```bash
# Render Logs mein dekho kya error hai
# Dashboard → Service → Logs tab
```

Aam errors:
- `ModuleNotFoundError` → `requirements.txt` mein dependency missing hai
- `Address already in use` → Port conflict (Render par khud handle karta hai)
- `No module named 'src'` → Start command galat hai, check karo: `gunicorn -c gunicorn.conf.py src.main:app`

---

### ❌ Problem: Frontend login nahi ho raha, "Unable to connect" error

**Check list:**
1. `config.js` mein Render URL sahi hai?
   ```javascript
   // Yeh hona chahiye:
   window.GETTIK_API_BASE_URL = 'https://YOUR-ACTUAL-APP.onrender.com';
   // Nahi hona chahiye (placeholder):
   window.GETTIK_API_BASE_URL = 'https://gettik-backend.onrender.com';
   ```

2. Browser Console (F12) mein koi CORS error hai?
   - Agar `CORS error` aa raha hai → Render mein `CORS_ORIGINS` mein apna Hostinger domain add karo

3. Render backend asleep to nahi?
   - Free tier par pehli request mein 30 sec lag sakte hain (cold start)
   - Pehle `https://YOUR-APP.onrender.com/health` directly kholo → wake up hoga

---

### ❌ Problem: Admin panel nahi khul raha

**Check karo:**
1. `gettik-frontend/admin/config.js` properly upload hua?
2. `gettik-frontend/admin/.htaccess` upload hua? (Hidden file hai)
3. `admin/index.html` ka CSS path sahi hai? Yeh hona chahiye:
   ```html
   <link rel="stylesheet" href="./css/admin.css?v=2.3.0">
   ```

---

### ❌ Problem: .htaccess kaam nahi kar raha (404 errors)

Hostinger cPanel mein **"Softaculous"** ya **"File Manager"** se check karo:
- `RewriteEngine On` support k liye `mod_rewrite` enabled hona chahiye
- Hostinger Premium par yeh by default enabled hota hai

---

### ❌ Problem: Render par data reset ho jaata hai

Yeh normal hai — Render free tier par **ephemeral filesystem** hai. Matlab:
- App restart hone par `data/gettik.db` aur downloaded files delete ho jaate hain
- Fix k liye: Render Dashboard → apni service → **"Disks"** add karo ($1/GB/month)
- Ya Render PostgreSQL (free tier) use karo permanent database k liye:
  ```
  DATABASE_URL = postgresql://user:password@host/dbname
  ```

---

### ❌ Problem: Render app bahut slow hai / timeout ho raha hai

Render **Free tier par 15 minute inactivity ke baad app so jaata hai**.
Pehli request mein 20-30 second lag sakte hain (cold start).

Fix options:
1. **Render Starter Plan** ($7/month) — app hamesha on rehta hai
2. **Cron job** — ek free service se har 14 minute mein `/health` ping karo (UptimeRobot.com free hai)

---

## Frontend Se API Manually Test Karna

Browser Console (F12 → Console) mein:

```javascript
// 1. Config check karo
console.log('API URL:', window.GETTIK_API_BASE_URL);

// 2. Health check
fetch(window.GETTIK_API_BASE_URL + '/health')
  .then(r => r.json()).then(console.log);

// 3. Login test (admin account k saath)
GettikAPI.login('admin@example.com', 'password', 'SERIAL-KEY')
  .then(r => console.log('Login result:', r))
  .catch(e => console.error('Login error:', e.message));
```

---

## App Versions

- **Gettik Version:** 2.2.0
- **FastAPI:** >= 0.110.0
- **Uvicorn:** >= 0.28.0 (ASGI server)
- **Gunicorn:** >= 21.2.0 (Production WSGI manager)
- **yt-dlp:** >= 2024.3.10 (Video download library)
- **imageio-ffmpeg:** >= 0.4.9 (FFmpeg bundled — alag se install nahi karna)
- **Python required:** 3.10+
