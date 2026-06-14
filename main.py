import os
import logging
import polars as pl
import hashlib
import time
import asyncio
import aiohttp
import uuid
import uvicorn
import json
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import Request, HTTPException

from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, List, Any
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from google.oauth2.service_account import Credentials
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from googleapiclient.discovery import build

from bot import init_discord_bot, get_discord_user_by_username, get_discord_user_profile, assign_role_to_user

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Discord Verification Student Search")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
STUDENT_SHEET_ID = os.getenv("VERIFICATION_FORM_SHEET_ID")
NTFY_LINK = os.getenv("NTFY_LINK", "")
NTFY_USER = os.getenv("NTFY_USER", "")
NTFY_PASSWORD = os.getenv("NTFY_PASSWORD", "")

sheets_service, sheets_id = None, None
def init_google_sheets():
    """Initialize Google Sheets API with service account"""
    global sheets_service, sheets_id
    try:
        creds_file = Path(__file__).parent / "service-account-key.json"
        if creds_file.exists():
            creds = Credentials.from_service_account_file(creds_file, scopes=['https://www.googleapis.com/auth/spreadsheets'])
            sheets_service = build('sheets', 'v4', credentials=creds)
            logger.info("Google Sheets API initialized successfully")
            try:
                spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=STUDENT_SHEET_ID).execute()
                for sheet in spreadsheet.get('sheets', []):
                    if sheet['properties']['title'] == 'Form Responses 1':
                        sheets_id = sheet['properties']['sheetId']
                        logger.info(f"Found sheet 'Form Responses 1' with ID: {sheets_id}")
                        break
                if sheets_id is None:
                    logger.warning("Could not find 'Form Responses 1' sheet, defaulting to first sheet")
                    if spreadsheet.get('sheets'):
                        sheets_id = spreadsheet['sheets'][0]['properties']['sheetId']
            except Exception as e:
                logger.warning(f"Could not fetch sheet ID: {str(e)}, defaulting to 0")
                sheets_id = 0
        else:
            logger.warning(f"Service account credentials not found at {creds_file}")
    except Exception as e:
        logger.error(f"Error initializing Google Sheets API: {str(e)}")


# ==================== RATE LIMITING / SECURITY ====================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://verify.iancheung.dev", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)

def verify_referer(request: Request) -> bool:
    """Check if request comes from verify.iancheung.dev"""
    referer = request.headers.get("referer", "")
    if not referer:
        return False
    return "verify.iancheung.dev" in referer or "localhost" in referer or "127.0.0.1" in referer

# Track failed login attempts per IP
failed_login_attempts: Dict[str, List[float]] = defaultdict(list)
LOGIN_ATTEMPTS_LIMIT = 5  # Max attempts
LOGIN_ATTEMPTS_WINDOW = 300  # Within 5 minutes
LOGIN_LOCKOUT_DURATION = 900  # Lockout for 15 minutes

# Track search requests per IP
search_requests: Dict[str, List[float]] = defaultdict(list)
SEARCH_RATE_LIMIT = 120  # Max requests
SEARCH_RATE_WINDOW = 60  # Per 60 seconds

# Session management configuration
SESSION_EXPIRATION_TIME = 3600  # 1 hour in seconds
SESSION_COOKIE_NAME = "session_token"
SESSION_COOKIE_MAX_AGE = 3600  # 1 hour

# Store authenticated sessions (token -> {expires_at: float, ip: str})
authenticated_sessions: Dict[str, Dict[str, Any]] = {}

def get_client_ip(request: Request) -> str:
    """Extract client IP address behind reverse proxy"""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"

def is_rate_limited(ip: str, request_list: Dict[str, List[float]], limit: int, window: int) -> bool:
    """Check if IP has exceeded rate limit"""
    now = time.time()
    request_list[ip] = [req_time for req_time in request_list[ip] if now - req_time < window]
    if len(request_list[ip]) >= limit:
        return True
    request_list[ip].append(now)
    return False

def is_login_locked_out(ip: str) -> bool:
    """Check if IP is locked out due to too many failed attempts"""
    now = time.time()
    failed_login_attempts[ip] = [attempt for attempt in failed_login_attempts[ip] if now - attempt < LOGIN_ATTEMPTS_WINDOW]
    if len(failed_login_attempts[ip]) >= LOGIN_ATTEMPTS_LIMIT:
        oldest_attempt = min(failed_login_attempts[ip])
        if now - oldest_attempt < LOGIN_LOCKOUT_DURATION:
            return True
    return False

def verify_authentication(request: Request) -> bool:
    """Verify that request has a valid session token"""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token or session_token not in authenticated_sessions:
        return False
    session_data = authenticated_sessions[session_token]
    if time.time() > session_data.get("expires_at", 0):
        del authenticated_sessions[session_token]
        return False
    return True

def create_session_token(client_ip: str, email: str) -> str:
    """Create a new session token for a client"""
    token = str(uuid.uuid4())
    expires_at = time.time() + SESSION_EXPIRATION_TIME
    authenticated_sessions[token] = {
        "expires_at": expires_at,
        "ip": client_ip,
        "email": email,
        "created_at": time.time()
    }
    logger.info(f"Created session token for IP: {client_ip}, Email: {email}")
    return token

def cleanup_expired_sessions():
    """Remove expired session tokens"""
    now = time.time()
    expired_tokens = [
        token for token, data in authenticated_sessions.items()
        if now > data.get("expires_at", 0)
    ]
    for token in expired_tokens:
        del authenticated_sessions[token]
    if expired_tokens:
        logger.info(f"Cleaned up {len(expired_tokens)} expired sessions")

# ==================== CSV LOADING ====================

# Global dictionary to store loaded DataFrames
loaded_dataframes: Dict[str, pl.DataFrame] = {}

# Global dictionary to store file metadata (last modified time)
file_metadata: Dict[str, Dict[str, Any]] = {}

def load_csv_files():
    """Load all CSV files from the data folder on startup"""
    calverify_dir = Path(__file__).parent / "data"
    csv_files = list(calverify_dir.glob("*.csv"))
    logger.info(f"Found {len(csv_files)} CSV files")
    for csv_file in csv_files:
        try:
            df = pl.read_csv(csv_file)
            filename = csv_file.name
            loaded_dataframes[filename] = df
            mod_time = os.path.getmtime(csv_file)
            mod_datetime = datetime.fromtimestamp(mod_time, tz=timezone.utc)
            file_metadata[filename] = {"last_modified_iso": mod_datetime.isoformat(), "total_rows": len(df)}
            logger.info(f"Loaded {filename} with {len(df)} rows (modified: {mod_datetime.isoformat()})")
        except Exception as e:
            logger.error(f"Error loading {csv_file.name}: {str(e)}")
    logger.info(f"Successfully loaded {len(loaded_dataframes)} DataFrames")

def search_dataframes(query: str) -> Dict[str, List[Dict[str, Any]]]:
    """Search across all loaded DataFrames using Polars optimized string operations."""
    results = {}
    query_lower = query.lower()
    for filename, df in loaded_dataframes.items():
        conditions = None
        for col in df.columns:
            col_condition = pl.col(col).cast(pl.Utf8).str.to_lowercase().str.contains(query_lower)
            conditions = col_condition if conditions is None else (conditions | col_condition)
        if conditions is not None:
            matching_rows = df.filter(conditions).to_dicts()
            if matching_rows:
                results[filename] = matching_rows
    return results

def reload_csv_files():
    """Reload all CSV files from the data folder - clears and repopulates cache"""
    global loaded_dataframes, file_metadata
    loaded_dataframes.clear()
    file_metadata.clear()
    load_csv_files()

def load_manifest() -> Dict[str, Any]:
    """Load manifest.json - returns default dict if file doesn't exist"""
    manifest_path = Path(__file__).parent / "manifest.json"
    default_manifest = {"sheetFiles": {}, "authorizedEmails": []}
    try:
        if manifest_path.exists():
            with open(manifest_path, 'r') as f:
                data = json.load(f)
                if "authorizedEmails" not in data:
                    data["authorizedEmails"] = []
                return data
    except Exception as e:
        logger.error(f"Error loading manifest.json: {str(e)}")
    return default_manifest

def save_manifest(manifest: Dict[str, Any]):
    """Save manifest.json"""
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        logger.info("Manifest saved successfully")
    except Exception as e:
        logger.error(f"Error saving manifest.json: {str(e)}")

def extract_sheet_info_from_url(url: str) -> tuple:
    """Extract sheet ID and gid from Google Sheets URL
    Returns (sheet_id, gid) or (None, None) if invalid"""
    try:
        # URL format: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid={GID}#gid={GID}
        sheet_id_match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', url)
        gid_match = re.search(r'[?#&]gid=(\d+)', url)
        
        if sheet_id_match and gid_match:
            return sheet_id_match.group(1), gid_match.group(1)
    except Exception as e:
        logger.error(f"Error extracting sheet info from URL: {str(e)}")
    return None, None

def download_sheet_as_csv(drive_service, sheets_service, sheet_id: str, gid: str = "0") -> tuple:
    """Download the first sheet from a Google Sheet file (native or XLSX).
    Uses Google Drive API to download/export and Google Sheets API to get title. Returns (csv_content, spreadsheet_name, sheet_name) or (None, None, None) on failure"""
    try:
        # Get the spreadsheet title from Sheets API
        spreadsheet_name = "Sheet"
        try:
            if sheets_service:
                spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id, fields='properties/title').execute()
                spreadsheet_name = spreadsheet.get('properties', {}).get('title', 'Sheet')
                logger.info(f"Spreadsheet title retrieved from Sheets API: '{spreadsheet_name}'")
            else:
                logger.warning("Sheets service not available for fetching spreadsheet title")
        except Exception as e:
            logger.warning(f"Could not fetch spreadsheet title via Sheets API: {str(e)}, using default name")
            spreadsheet_name = "Sheet"
        
        # Check file MIME type to determine if it's a native Google Sheet
        try:
            file_metadata = drive_service.files().get(fileId=sheet_id, fields='mimeType').execute()
            mime_type = file_metadata.get('mimeType', '')
            is_google_sheet = mime_type == 'application/vnd.google-apps.spreadsheet'
            logger.info(f"File MIME type: {mime_type}")
        except Exception as e:
            logger.warning(f"Could not fetch file MIME type: {str(e)}, assuming XLSX format")
            is_google_sheet = False
        
        # Download or export the file
        temp_excel_path = Path(__file__).parent / f"temp_sheet_{sheet_id}.xlsx"
        try:
            if is_google_sheet:
                # Export native Google Sheet to XLSX format
                logger.info(f"Exporting native Google Sheet to XLSX format")
                request = drive_service.files().export(fileId=sheet_id, mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            else:
                # Download uploaded XLSX file directly
                logger.info(f"Downloading XLSX file directly")
                request = drive_service.files().get_media(fileId=sheet_id)
            
            file_content = request.execute()
        except Exception as e:
            logger.error(f"Failed to download/export file: {str(e)}")
            return None, None, None
        
        # Write to temporary file
        try:
            with open(temp_excel_path, 'wb') as f:
                f.write(file_content)
        except Exception as e:
            logger.error(f"Failed to write temporary file: {str(e)}")
            return None, None, None
        
        # Parse with openpyxl to get sheet names and find the target sheet
        try:
            import openpyxl
            wb = openpyxl.load_workbook(temp_excel_path)
            
            # Find the sheet by gid
            target_sheet_name = None
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                # openpyxl stores sheet_id as an attribute
                if str(getattr(ws, 'sheet_id', None)) == gid:
                    target_sheet_name = sheet
                    break
            
            # If gid not found, use first sheet
            if not target_sheet_name:
                logger.warning(f"Sheet with gid {gid} not found, using first sheet")
                if wb.sheetnames:
                    target_sheet_name = wb.sheetnames[0]
                else:
                    logger.error(f"No sheets found in workbook")
                    if temp_excel_path.exists():
                        temp_excel_path.unlink()
                    return None, None, None
            
            logger.info(f"Using sheet: '{target_sheet_name}' (gid: {gid})")
            
        except ImportError:
            logger.error("openpyxl not installed")
            if temp_excel_path.exists():
                temp_excel_path.unlink()
            return None, None, None
        except Exception as e:
            logger.error(f"Error parsing workbook: {str(e)}")
            if temp_excel_path.exists():
                temp_excel_path.unlink()
            return None, None, None
        
        # Read the sheet using polars
        try:
            df = pl.read_excel(temp_excel_path, sheet_name=target_sheet_name)
        except Exception as e:
            logger.error(f"Error reading sheet '{target_sheet_name}': {str(e)}")
            if temp_excel_path.exists():
                temp_excel_path.unlink()
            return None, None, None
        
        # Convert to CSV content
        csv_content = df.write_csv()
        
        # Clean up temp file
        if temp_excel_path.exists():
            temp_excel_path.unlink()
        
        logger.info(f"Successfully downloaded sheet '{target_sheet_name}' with {len(df)} rows")
        return csv_content, spreadsheet_name, target_sheet_name
        
    except Exception as e:
        logger.error(f"Error downloading sheet as CSV: {str(e)}")
        return None, None, None


def fetch_student_entries() -> List[Dict[str, Any]]:
    """Fetch student verification entries from Google Sheet (bottom to top) - OPTIMIZED"""
    if not sheets_service:
        logger.error("Google Sheets API not initialized")
        return []
    try:
        # Fetch both values and formatting in a single API call
        spreadsheet_data = sheets_service.spreadsheets().get(
            spreadsheetId=STUDENT_SHEET_ID,
            includeGridData=True,
            ranges=["Form Responses 1!B:I"],
            fields="sheets/data/rowData/values(userEnteredValue,userEnteredFormat/backgroundColor)"
        ).execute()
        sheet_data = spreadsheet_data.get('sheets', [{}])[0]
        row_data = sheet_data.get('data', [{}])[0].get('rowData', [])
        if len(row_data) < 2:
            logger.warning("No student entries found in sheet")
            return []
        def rgb_to_hex(color_dict):
            """Convert RGB color dict to hex string with proper rounding"""
            if not color_dict:
                return None
            r = round((color_dict.get('red', 0) or 0) * 255)
            g = round((color_dict.get('green', 0) or 0) * 255)
            b = round((color_dict.get('blue', 0) or 0) * 255)
            return f"#{r:02x}{g:02x}{b:02x}"
            
        # Column indices for range B:H (shifted by 1 from A:Z):
        # B = index 0 (Status - background color)
        # C = index 1 (First and Last Name)
        # D = index 2 (Berkeley Email)
        # E = index 3 (Graduating Class)
        # F = index 4 (Discord Username)
        # G = index 5 (Berkeley Student ID)
        # H = index 6 (Manual Notes)
        status_col = 0
        name_col = 1
        email_col = 2
        graduating_class_col = 3
        discord_username_col = 4
        student_id_col = 5
        notes_col = 6
        discord_user_id_col = 7
        
        # row_data[0] is the header row, so start from row_data[1]
        # Go from bottom to top (reverse order)
        entries = []
        for row_idx in range(len(row_data) - 1, 0, -1):
            row_format_data = row_data[row_idx]
            values_data = row_format_data.get('values', [])
            row = []
            for cell in values_data:
                value = cell.get('userEnteredValue', {})
                if 'stringValue' in value:
                    row.append(value['stringValue'])
                elif 'numberValue' in value:
                    row.append(str(value['numberValue']))
                elif 'boolValue' in value:
                    row.append(str(value['boolValue']))
                else:
                    row.append('')
            name = row[name_col] if len(row) > name_col else ""
            email = row[email_col] if len(row) > email_col else ""
            graduating_class = row[graduating_class_col] if len(row) > graduating_class_col else ""
            discord_username = row[discord_username_col] if len(row) > discord_username_col else ""
            student_id = row[student_id_col] if len(row) > student_id_col else ""
            notes = row[notes_col] if len(row) > notes_col else ""
            discord_user_id = row[discord_user_id_col] if len(row) > discord_user_id_col else ""
            if not student_id.strip():
                continue
            bg_color = None
            if status_col < len(values_data):
                cell_format = values_data[status_col].get('userEnteredFormat', {})
                bg_color_dict = cell_format.get('backgroundColor', {})
                bg_color = rgb_to_hex(bg_color_dict) if bg_color_dict else None
            entries.append({
                "row_index": row_idx,
                "name": name.strip(),
                "email": email.strip(),
                "graduating_class": graduating_class.strip(),
                "discord_username": discord_username.strip(),
                "student_id": student_id.strip(),
                "notes": notes.strip(),
                "discord_user_id": discord_user_id.strip(),
                "background_color": bg_color,
                "full_row": row
            })
        logger.info(f"Fetched {len(entries)} student entries from Google Sheet")
        return entries
    except Exception as e:
        logger.error(f"Error fetching student entries: {str(e)}")
        return []


# ==================== NOTIFICATIONS & BACKGROUND TASKS ====================

async def send_ntfy_notification(message: str, title: str = None, click_url: str = None):
    """Send notification to Ntfy"""
    if not NTFY_LINK:
        return
    try:
        async with aiohttp.ClientSession() as session:
            auth = aiohttp.BasicAuth(NTFY_USER, NTFY_PASSWORD) if NTFY_USER and NTFY_PASSWORD else None
            headers = {}
            if title:
                headers["Title"] = title.encode('utf-8').decode('latin1') # Ntfy ascii header safeguard
            if click_url:
                headers["Actions"] = f"view, Open Dashboard, {click_url}"
            
            async with session.post(
                NTFY_LINK,
                data=message.encode('utf-8'),
                auth=auth,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to send ntfy notification: {resp.status}")
    except Exception as e:
        logger.error(f"Error sending ntfy notification: {e}")

async def monitor_new_entries():
    """Background task to poll Google Sheets for new entries and verify them"""
    logger.info("Initializing background entry monitor...")
    # Wait briefly for initialization to finish
    await asyncio.sleep(5)
    
    initial_entries = fetch_student_entries()
    last_max_row = max([entry["row_index"] for entry in initial_entries]) if initial_entries else 0
    logger.info(f"Monitor started. Current max row: {last_max_row}")
    
    while True:
        await asyncio.sleep(60)
        try:
            entries = fetch_student_entries()
            if not entries:
                continue
                
            current_max_row = max([e["row_index"] for e in entries])
            if current_max_row > last_max_row:
                new_entries = [e for e in entries if e["row_index"] > last_max_row]
                
                for entry in new_entries:
                    student_id = entry.get("student_id", "").strip()
                    if not student_id:
                        continue
                        
                    # Minimal check: match student ID
                    results = search_dataframes(student_id)
                    total_matches = sum(len(v) for v in results.values())
                    
                    if total_matches >= 1:
                        msg = f"Name: {entry.get('name')}\nEmail: {entry.get('email')}\nStudent ID: {student_id}\nDiscord: {entry.get('discord_username')}"
                        await send_ntfy_notification(
                            msg, 
                            title="New Verification Submission", 
                            click_url="https://verify.iancheung.dev"
                        )
                
                last_max_row = current_max_row
        except Exception as e:
            logger.error(f"Error in monitor_new_entries loop: {e}")

# ==================== API ENDPOINTS ====================

@app.on_event("startup")
async def startup_event():
    """Load all CSV files, initialize Google Sheets API, and initialize Discord bot when the app starts"""
    load_csv_files()
    init_google_sheets()
    asyncio.create_task(monitor_new_entries())
    await init_discord_bot()

@app.get("/config")
async def get_config():
    """Return public configuration values"""
    return {"google_client_id": GOOGLE_CLIENT_ID}

@app.post("/login")
async def login(request: Request):
    """Login endpoint to validate Google credential and create session"""
    if not verify_referer(request):
        raise HTTPException(status_code=403, detail="Access denied")
    client_ip = get_client_ip(request)
    if is_login_locked_out(client_ip):
        logger.warning(f"Login attempt from locked-out IP: {client_ip}")
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 15 minutes.")
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    
    credential = data.get("credential", "")
    if not credential:
        raise HTTPException(status_code=400, detail="Missing Google credential")
        
    try:
        idinfo = id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")
            
        # Check authorization
        manifest = load_manifest()
        authorized_emails = manifest.get("authorizedEmails", [])
        
        is_authorized = email.lower() == "iancheung@berkeley.edu" or email.lower() in [e.lower() for e in authorized_emails]
        
        if is_authorized:
            logger.info(f"Successful login for {email} from IP: {client_ip}")
            failed_login_attempts[client_ip] = []
            session_token = create_session_token(client_ip, email.lower())
            
            response = JSONResponse(content={
                "success": True, 
                "message": "Login successful",
                "email": email
            }, status_code=200)
            response.set_cookie(
                SESSION_COOKIE_NAME,
                session_token,
                max_age=SESSION_COOKIE_MAX_AGE,
                httponly=True,
                secure=True,
                samesite="strict"
            )
            return response
        else:
            failed_login_attempts[client_ip].append(time.time())
            logger.warning(f"Unauthorized email {email} from IP: {client_ip}")
            raise HTTPException(status_code=401, detail="Email not authorized")
            
    except ValueError as e:
        failed_login_attempts[client_ip].append(time.time())
        logger.warning(f"Invalid Google token from IP: {client_ip}: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid token")

@app.post("/logout")
async def logout(request: Request):
    """Logout endpoint to invalidate session"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token and session_token in authenticated_sessions:
        del authenticated_sessions[session_token]
        logger.info(f"Session logged out: {session_token}")
    response = JSONResponse(content={"success": True, "message": "Logged out successfully"}, status_code=200)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serve the main HTML page with password protection"""
    template_path = Path(__file__).parent / "templates" / "index.html"
    if template_path.exists():
        with open(template_path, "r") as f:
            return f.read()
    return "<h1>Template not found</h1>"

@app.post("/search")
async def search(request: Request, q: str):
    """Search endpoint that accepts a query string.
    
    Args:
        q: Query string to search for (case-insensitive substring match)
    
    Returns:
        Dictionary with filename as key and list of matching rows as values, plus file metadata
    """
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        logger.warning(f"Search rate limit exceeded for IP: {client_ip}")
        raise HTTPException(status_code=429, detail="Too many search requests. Please slow down.")
    if not q or len(q.strip()) == 0:
        return {"results": {}, "query": q, "metadata": {}, "total_database_rows": sum(m.get("total_rows", 0) for m in file_metadata.values())}
    results = search_dataframes(q.strip())
    response_results = {}
    for filename, rows in results.items():
        response_results[filename] = {
            "rows": rows,
            "last_modified_iso": file_metadata.get(filename, {}).get("last_modified_iso", "Unknown"),
            "total_rows": file_metadata.get(filename, {}).get("total_rows", 0)
        }
    total_database_rows = sum(m.get("total_rows", 0) for m in file_metadata.values())
    return {
        "results": response_results,
        "query": q,
        "total_matches": sum(len(v["rows"]) for v in response_results.values()),
        "total_database_rows": total_database_rows
    }

@app.get("/student-entries")
async def get_student_entries(request: Request):
    """Get student verification entries from Google Sheet"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    entries = fetch_student_entries()
    return {
        "entries": entries,
        "total_count": len(entries),
        "success": len(entries) > 0
    }

@app.post("/update-entry-color")
async def update_entry_color(request: Request):
    """Update the background color of columns B-F for a student entry"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    row_index = data.get("row_index")
    color_hex = data.get("color_hex")
    if row_index is None or not isinstance(row_index, int) or row_index < 1:
        raise HTTPException(status_code=400, detail="Invalid row index")
    if not color_hex or not isinstance(color_hex, str) or not color_hex.startswith("#"):
        raise HTTPException(status_code=400, detail="Invalid color format")
    try:
        if not sheets_service:
            raise Exception("Google Sheets API not initialized")
        hex_color = color_hex.lstrip("#")
        rgb = tuple(int(hex_color[i:i+2], 16) / 255 for i in (0, 2, 4))
        color_dict = {
            "red": rgb[0],
            "green": rgb[1],
            "blue": rgb[2]
        }
        requests_list = []
        for col_idx in range(1, 6):  # Columns B, C, D, E, F (indices 1-5)
            requests_list.append({
                "updateCells": {
                    "range": {
                        "sheetId": sheets_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1
                    },
                    "rows": [{
                        "values": [{
                            "userEnteredFormat": {
                                "backgroundColor": color_dict
                            }
                        }]
                    }],
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })
        body = {"requests": requests_list}
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=STUDENT_SHEET_ID,
            body=body
        ).execute()
        logger.info(f"Updated color for row {row_index} to {color_hex}")
        return {"success": True, "message": "Color updated successfully"}
    except Exception as e:
        logger.error(f"Error updating entry color: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update color")

@app.post("/update-entry-notes")
async def update_entry_notes(request: Request):
    """Update the notes (column H) for a student entry"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    row_index = data.get("row_index")
    notes_text = data.get("notes_text", "")
    if row_index is None or not isinstance(row_index, int) or row_index < 1:
        raise HTTPException(status_code=400, detail="Invalid row index")
    try:
        if not sheets_service:
            raise Exception("Google Sheets API not initialized")
        cell_range = f"'Form Responses 1'!H{row_index + 1}" # Column H is index 7, row_index needs to be converted from 0-indexed to 1-indexed
        sheets_service.spreadsheets().values().update(
            spreadsheetId=STUDENT_SHEET_ID,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[notes_text]]}
        ).execute()
        logger.info(f"Updated notes for row {row_index}")
        return {"success": True, "message": "Notes updated successfully"}
    except Exception as e:
        logger.error(f"Error updating entry notes: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update notes")
    
@app.post("/update-entry-discord-username")
async def update_entry_discord_username(request: Request):
    """Update the discord username (column F) for a student entry"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    row_index = data.get("row_index")
    discord_username = data.get("discord_username", "")
    if row_index is None or not isinstance(row_index, int) or row_index < 1:
        raise HTTPException(status_code=400, detail="Invalid row index")
    try:
        if not sheets_service:
            raise Exception("Google Sheets API not initialized")
        cell_range = f"'Form Responses 1'!F{row_index + 1}"  # Column F = Discord Username
        sheets_service.spreadsheets().values().update(
            spreadsheetId=STUDENT_SHEET_ID,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[discord_username]]}
        ).execute()
        logger.info(f"Updated discord username for row {row_index} to '{discord_username}'")
        return {"success": True, "message": "Discord username updated successfully"}
    except Exception as e:
        logger.error(f"Error updating discord username: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update discord username")


@app.post("/update-entry-discord-id")
async def update_entry_discord_id(request: Request):
    """Update the discord user ID (column I) for a student entry"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    row_index = data.get("row_index")
    discord_user_id = data.get("discord_user_id", "")
    if row_index is None or not isinstance(row_index, int) or row_index < 1:
        raise HTTPException(status_code=400, detail="Invalid row index")
    try:
        if not sheets_service:
            raise Exception("Google Sheets API not initialized")
        cell_range = f"'Form Responses 1'!I{row_index + 1}"  # Column I = Discord User ID
        sheets_service.spreadsheets().values().update(
            spreadsheetId=STUDENT_SHEET_ID,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[discord_user_id]]}
        ).execute()
        logger.info(f"Updated discord user ID for row {row_index} to '{discord_user_id}'")
        return {"success": True, "message": "Discord user ID updated successfully"}
    except Exception as e:
        logger.error(f"Error updating discord user ID: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to update discord user ID")


@app.get("/roles")
async def get_roles(request: Request):
    """Get list of authorized emails"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    manifest = load_manifest()
    return {"success": True, "emails": manifest.get("authorizedEmails", [])}

@app.post("/roles")
async def add_role(request: Request):
    """Add a new authorized email"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
        
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")
        
    if email == "iancheung@berkeley.edu":
        return {"success": True, "message": "Email is already permanently authorized"}
        
    manifest = load_manifest()
    emails = manifest.get("authorizedEmails", [])
    
    if email not in emails:
        emails.append(email)
        manifest["authorizedEmails"] = emails
        save_manifest(manifest)
        logger.info(f"Added authorized email: {email}")
        
    return {"success": True, "message": "Email authorized successfully"}

@app.delete("/roles")
async def remove_role(request: Request):
    """Remove an authorized email"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
        
    email = data.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")
        
    if email == "iancheung@berkeley.edu":
        raise HTTPException(status_code=403, detail="Cannot remove the master administrator")
        
    manifest = load_manifest()
    emails = manifest.get("authorizedEmails", [])
    
    if email in emails:
        emails.remove(email)
        manifest["authorizedEmails"] = emails
        save_manifest(manifest)
        logger.info(f"Removed authorized email: {email}")
        
    # Also invalidate any active sessions for this email
    tokens_to_remove = []
    for token, session_data in authenticated_sessions.items():
        if session_data.get("email") == email:
            tokens_to_remove.append(token)
    for token in tokens_to_remove:
        del authenticated_sessions[token]
        
    return {"success": True, "message": "Email removed successfully"}

# ==================== DISCORD ENDPOINTS ====================

@app.post("/discord/user-profile")
async def discord_user_profile(request: Request):
    """Fetch Discord user profile by username"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    discord_username = data.get("discord_username", "").strip().lower()
    if not discord_username:
        raise HTTPException(status_code=400, detail="Discord username required")
    try:
        from bot import client
        if not client or not client.is_ready():
            logger.warning("Discord user-profile request but bot not ready")
            return {"success": False, "error": "Bot not ready", "message": "Discord bot is still connecting. Please try again in a moment."}
        member = await get_discord_user_by_username(discord_username)
        if not member:
            logger.info(f"Discord user not found: {discord_username}")
            return {"success": False, "error": "User not found", "message": f"Could not find Discord user '{discord_username}' in the server"}
        profile = await get_discord_user_profile(member.id)
        if not profile:
            return {"success": False, "error": "Profile fetch failed", "message": "Failed to fetch user profile"}
        return {"success": True, "profile": profile}
    except Exception as e:
        logger.error(f"Error fetching Discord profile: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch Discord profile")

@app.post("/discord/assign-role")
async def discord_assign_role(request: Request):
    """Assign a class role to a Discord user"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    user_id = data.get("user_id")
    role_name = data.get("role_name", "").strip()
    send_welcome_msg = data.get("send_welcome_msg", True)
    
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid user ID")
    try:
        user_id = int(user_id) if isinstance(user_id, str) else user_id
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user ID format")
    valid_roles = [
        "Class of 2030", 
        "Class of 2029", 
        "Class of 2028", 
        "Class of 2027", 
        "Class of 2026",
        "Class of 2025", 
        "Class of 2024", 
        "Class of 2023", 
        "Transfer Class of 2028", 
        "Transfer Class of 2027", 
        "Transfer Class of 2026", 
        "Transfer Class of 2025",
        "Transfer Class of 2024", 
        "Grad & Professional Student", 
        "UCEAP"
    ]
    if role_name not in valid_roles:
        raise HTTPException(status_code=400, detail="Invalid role name")
    try:
        from bot import client
        if not client or not client.is_ready():
            logger.warning("Discord assign-role request but bot not ready")
            return {"success": False, "error": "Bot not ready", "message": "Discord bot is not connected. Role assignment unavailable."}
        
        success = await assign_role_to_user(user_id, role_name, send_welcome_msg)
        
        if success:
            logger.info(f"Successfully assigned role '{role_name}' to user {user_id}")
            return {"success": True, "message": f"Role '{role_name}' assigned successfully"}
        else:
            logger.warning(f"Failed to assign role to user {user_id}")
            return {"success": False, "error": "Assignment failed", "message": "Failed to assign role to user"}
    except Exception as e:
        logger.error(f"Error assigning role: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to assign role")

# ==================== CSV MANAGEMENT ENDPOINTS ====================

@app.post("/link-sheet")
async def link_sheet(request: Request):
    """Link a Google Sheet and access its first tab"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    
    sheet_url = data.get("sheet_url", "").strip()
    if not sheet_url:
        raise HTTPException(status_code=400, detail="Sheet URL required")
    
    # Validate URL format
    if "docs.google.com/spreadsheets" not in sheet_url:
        raise HTTPException(status_code=400, detail="Invalid Google Sheets URL")
    
    # Extract sheet ID from URL
    sheet_id_match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
    if not sheet_id_match:
        raise HTTPException(status_code=400, detail="Invalid Google Sheets URL format. Expected: https://docs.google.com/spreadsheets/d/[SHEET_ID]/...")
    
    sheet_id = sheet_id_match.group(1)
    
    # Extract gid (tab ID) from URL - defaults to "0" if not provided
    gid_match = re.search(r'[?#&]gid=(\d+)', sheet_url)
    gid = gid_match.group(1) if gid_match else "0"
    
    try:
        # Initialize Google Drive and Sheets APIs
        creds_file = Path(__file__).parent / "service-account-key.json"
        if not creds_file.exists():
            raise HTTPException(status_code=500, detail="Service account credentials not found")
        
        creds = Credentials.from_service_account_file(
            creds_file,
            scopes=['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']
        )
        drive_service = build('drive', 'v3', credentials=creds)
        sheets_service = build('sheets', 'v4', credentials=creds)
        
        # Download the sheet with specified gid
        csv_content, spreadsheet_name, sheet_name = download_sheet_as_csv(drive_service, sheets_service, sheet_id, gid)
        if not csv_content or not sheet_name:
            raise HTTPException(status_code=400, detail="Failed to access sheet. Ensure the file is a native Google Sheet or XLSX file, and is shared with calverify@calverify.iam.gserviceaccount.com using Viewer access.")
        
        # Create filename: "[Spreadsheet Name].csv"
        filename = f"{spreadsheet_name}.csv"
        
        # Save to data folder
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        output_path = data_dir / filename
        
        # Check for duplicates - if same sheet_id, it's an update; otherwise, add numbering
        manifest = load_manifest()
        if output_path.exists():
            existing_sheet_info = manifest.get("sheetFiles", {}).get(filename)
            
            if existing_sheet_info and existing_sheet_info.get("sheetId") != sheet_id:
                # Different sheet_id, need to add numbering
                counter = 2
                while True:
                    new_filename = f"{spreadsheet_name} {counter}.csv"
                    new_path = data_dir / new_filename
                    new_sheet_info = manifest.get("sheetFiles", {}).get(new_filename)
                    
                    if not new_path.exists() or (new_sheet_info and new_sheet_info.get("sheetId") == sheet_id):
                        filename = new_filename
                        output_path = new_path
                        break
                    counter += 1
        
        with open(output_path, 'w') as f:
            f.write(csv_content)
        
        logger.info(f"Successfully linked sheet: {filename}")
        
        # Update manifest
        manifest = load_manifest()
        manifest["sheetFiles"][filename] = {
            "sheetId": sheet_id,
            "gid": gid,
            "sheetName": sheet_name,
            "spreadsheetName": spreadsheet_name,
            "uploadedAt": datetime.now(tz=timezone.utc).isoformat()
        }
        save_manifest(manifest)
        
        # Reload CSV cache
        reload_csv_files()
        
        return {
            "success": True,
            "message": f"Sheet linked successfully",
            "filename": filename,
            "sheetName": sheet_name
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading CSV: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to link sheet: {str(e)}")

@app.delete("/delete-sheet")
async def delete_sheet(request: Request):
    """Unlink a Google Sheet and remove it from the database"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    
    filename = data.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Filename required")
    
    # Validate filename (prevent path traversal)
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    try:
        # Check if file exists
        data_dir = Path(__file__).parent / "data"
        file_path = data_dir / filename
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        # Delete file
        file_path.unlink()
        logger.info(f"Unlinked sheet: {filename}")
        
        # Update manifest
        manifest = load_manifest()
        if filename in manifest.get("sheetFiles", {}):
            del manifest["sheetFiles"][filename]
            save_manifest(manifest)
        
        # Reload CSV cache
        reload_csv_files()
        
        return {
            "success": True,
            "message": f"Sheet unlinked successfully",
            "filename": filename
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting CSV: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to unlink sheet")

@app.post("/rename-sheet")
async def rename_sheet(request: Request):
    """Rename a linked Google Sheet"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip, search_requests, SEARCH_RATE_LIMIT, SEARCH_RATE_WINDOW):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid request")
    
    filename = data.get("filename", "").strip()
    new_name = data.get("newName", "").strip()
    
    if not filename:
        raise HTTPException(status_code=400, detail="Filename required")
    if not new_name:
        raise HTTPException(status_code=400, detail="New name required")
    
    # Validate filenames (prevent path traversal)
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if "/" in new_name or "\\" in new_name or new_name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid new name")
    
    try:
        # Check if file exists
        data_dir = Path(__file__).parent / "data"
        file_path = data_dir / filename
        new_filename = f"{new_name}.csv"
        new_file_path = data_dir / new_filename
        
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        
        if new_file_path.exists() and new_filename != filename:
            raise HTTPException(status_code=400, detail="A file with that name already exists")
        
        # Rename file
        file_path.rename(new_file_path)
        logger.info(f"Renamed sheet: {filename} -> {new_filename}")
        
        # Update manifest
        manifest = load_manifest()
        if filename in manifest.get("sheetFiles", {}):
            file_metadata = manifest["sheetFiles"].pop(filename)
            manifest["sheetFiles"][new_filename] = file_metadata
            save_manifest(manifest)
        
        # Reload CSV cache
        reload_csv_files()
        
        return {
            "success": True,
            "message": f"Sheet renamed successfully",
            "oldFilename": filename,
            "newFilename": new_filename
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming CSV: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to rename sheet")

@app.get("/sheet-manifest")
async def get_sheet_manifest(request: Request):
    """Get the manifest of all linked Google Sheets and their metadata"""
    if not verify_authentication(request):
        raise HTTPException(status_code=401, detail="Not authenticated. Please login first.")
    
    manifest = load_manifest()
    return {
        "success": True,
        "sheetFiles": manifest.get("sheetFiles", {}),
        "total": len(manifest.get("sheetFiles", {}))
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4187)
