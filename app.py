import io
import os
import csv
import math
import time
import smtplib
import logging
import io as _io
import traceback
import functools
import atexit
import mysql.connector
from decimal import Decimal
from PIL import Image
from datetime import datetime as dt
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from werkzeug.utils import secure_filename
from email.mime.multipart import MIMEMultipart
from werkzeug.datastructures import FileStorage
from flask import flash, get_flashed_messages
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, send_from_directory


app = Flask(__name__)
app.secret_key = 'secretKey'

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
MAX_FILE_SIZE = 1 * 1024 * 1024
MAX_TOTAL_SIZE = 3 * 1024 * 1024
MAX_FILES = 3
IMAGE_MAX_DIM = 1920
IMAGE_QUALITY = 85

IMAGE_MAX_RAW_PER_FILE = 10 * 1024 * 1024
MAX_CONTENT_LENGTH = (IMAGE_MAX_RAW_PER_FILE * MAX_FILES) + (1 * 1024 * 1024)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['PERMANENT_SESSION_LIFETIME'] = 1800  # 30 minutes

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(
        host='192.168.110.99',
        user='root',
        password='ERMsql999$',
        database='hr_database'
    )

def query_db(query, args=(), one=False):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(query, args)
    rv = cursor.fetchall()
    cursor.close()
    conn.close()
    return (rv[0] if rv else None) if one else rv

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_file_type(file):
    allowed_mime_types = {'application/pdf', 'image/png', 'image/jpeg'}
    return file.mimetype in allowed_mime_types

def compress_image(file_storage):
    img = Image.open(file_storage.stream)
 
    # Convert palette/RGBA modes to RGB for JPEG compatibility
    if img.mode in ('RGBA', 'P', 'LA'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
 
    # Resize if needed
    w, h = img.size
    if w > IMAGE_MAX_DIM or h > IMAGE_MAX_DIM:
        img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM), Image.LANCZOS)
 
    output = _io.BytesIO()
    img.save(output, format='JPEG', quality=IMAGE_QUALITY, optimize=True)
    output.seek(0)
    return output

def process_and_save_file(file, requester, start_date_str, department, leave_id, index=1):
    if not allowed_file(file.filename):
        raise ValueError(f'Invalid file extension for "{file.filename}". Allowed: PDF, PNG, JPG.')
    if not allowed_file_type(file):
        raise ValueError(f'Invalid file type for "{file.filename}".')
 
    ext = file.filename.rsplit('.', 1)[1].lower()

    # Read file into memory for size check
    file.seek(0)
    raw_data = file.read()
    raw_size = len(raw_data)
    
    if ext == 'pdf':
        if raw_size > MAX_FILE_SIZE:
            raise ValueError(
                f'"{file.filename}" ({raw_size/1024/1024:.2f}MB) exceeds the 1MB PDF limit. '
                f'Please reduce the PDF size before uploading.'
            )
 
    is_image = ext in ('jpg', 'jpeg', 'png')
    req_dept = (department or 'General').replace(' ', '_').replace('/', '-').replace('(', '').replace(')', '')
    yymmdd   = datetime.strptime(start_date_str, "%Y-%m-%d").strftime("%y%m%d")
    
    # Format: 260505_AD_ER0556_42_1.jpg
    raw_name = f"{yymmdd}_{req_dept}_{requester}_{leave_id}_{index}.{'jpg' if is_image else ext}"
    filename = secure_filename(raw_name)
 
    if not filename or filename == f".{ext}":
        filename = f"{yymmdd}_{requester}_{leave_id}_{index}.{'jpg' if is_image else ext}"
 
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
 
    if is_image:
        file.seek(0)
        compressed = compress_image(file)
        compressed_data = compressed.read()
        saved_size = len(compressed_data)
        # After compression, verify it's within limit
        if saved_size > MAX_FILE_SIZE:
            raise ValueError(
                f'"{file.filename}" is still {saved_size/1024/1024:.2f}MB after compression. '
                f'Please use a smaller image.'
            )
        with open(file_path, 'wb') as f:
            f.write(compressed_data)
    else:
        saved_size = raw_size
        with open(file_path, 'wb') as f:
            f.write(raw_data)

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        raise ValueError(f'File "{file.filename}" failed to save. Please try again.')

    return filename, saved_size


def save_leave_documents(conn, cursor, leave_id, filenames_and_sizes, original_names):
    for i, (filename, size) in enumerate(filenames_and_sizes):
        orig_name = original_names[i] if i < len(original_names) else filename
        cursor.execute("""
            INSERT INTO leave_documents (leave_id, filename, original_name, file_size)
            VALUES (%s, %s, %s, %s)
        """, (leave_id, filename, orig_name, size))
 
 
def delete_leave_documents(leave_id):
    docs = query_db(
        "SELECT filename FROM leave_documents WHERE leave_id = %s", (leave_id,)
    )
    for doc in docs:
        file_path = os.path.join(UPLOAD_FOLDER, doc['filename'])
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error deleting file {doc['filename']}: {e}")
 
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM leave_documents WHERE leave_id = %s", (leave_id,))
        conn.commit()
    finally:
        cursor.close()
        conn.close()
 
 
def check_duplicate_leave(requester, start_date_str, end_date_str, exclude_leave_id=None):
    query = """
        SELECT leave_id, start_date, end_date, status
        FROM leave_request
        WHERE requester = %s
          AND status IN ('Pending', 'Approved')
          AND start_date <= %s
          AND end_date >= %s
    """
    params = [requester, end_date_str, start_date_str]
    if exclude_leave_id:
        query += " AND leave_id != %s"
        params.append(exclude_leave_id)
    return query_db(query, params, one=True)

# ─────────────────────────────────────────────
# Role & Permission Config
# ─────────────────────────────────────────────
ROLE_PERMISSIONS = {
    'Employee':     ['leave.apply', 'leave.view', 'leave.apply_behalf'],
    'Sub-verifier': ['leave.apply', 'leave.view', 'leave.acknowledge', 'leave.apply_behalf'],
    'Verifier':     ['leave.apply', 'leave.view', 'leave.acknowledge', 'leave.apply_behalf'],
    'Approver':     ['leave.approve', 'leave.view'],
    'HR':           ['user.manage', 'leave.view', 'reports.view'],
    'Admin':        ['user.manage', 'leave.view', 'reports.view', 'system.manage'],
}

# ─────────────────────────────────────────────
# verifier_group helpers
# ─────────────────────────────────────────────
def get_verifier_for(employee_no, role='Sub-verifier'):
    row = query_db(
        """ 
            SELECT verifier_no FROM verifier_group 
            WHERE employee_no = %s AND verifier_role = %s LIMIT 1
        """,
        (employee_no, role), one=True
    )
    return row['verifier_no'] if row else None


def get_subordinates_for(employee_no):
    user = query_db(
        "SELECT roles FROM auth_user WHERE employee_no = %s LIMIT 1",
        (employee_no,), one=True
    )
    roles = user.get('roles', '') if user else ''

    if 'Approver' in roles and 'Verifier' not in roles and 'Sub-verifier' not in roles:
        return []

    if 'Verifier' in roles:
        rows = query_db(
            """SELECT vg.employee_no FROM verifier_group vg
               LEFT JOIN auth_user au ON au.employee_no = vg.employee_no
               WHERE vg.verifier_no = %s
                 AND vg.verifier_role = 'Verifier'
                 AND (au.roles IS NULL OR au.roles NOT LIKE %s)""",
            (employee_no, '%Approver%')
        )
        return [r['employee_no'] for r in rows] if rows else []

    if 'Sub-verifier' in roles:
        rows = query_db(
            """SELECT vg.employee_no FROM verifier_group vg
               LEFT JOIN auth_user au ON au.employee_no = vg.employee_no
               WHERE vg.verifier_no = %s
                 AND vg.verifier_role = 'Sub-Verifier'
                 AND (au.roles IS NULL OR (
                     au.roles NOT LIKE %s
                     AND au.roles NOT LIKE %s
                 ))""",
            (employee_no, '%Verifier%', '%Approver%')
        )
        return [r['employee_no'] for r in rows] if rows else []

    # Regular employee — same-dept teammates, excluding privileged roles
    emp = query_db(
        "SELECT Department FROM employee WHERE Employee_no = %s AND status = 'Active' LIMIT 1",
        (employee_no,), one=True
    )
    if not emp:
        return []

    rows = query_db(
        """SELECT e.Employee_no FROM employee e
           LEFT JOIN auth_user au ON au.employee_no = e.Employee_no
           WHERE e.Department = %s
             AND e.Employee_no != %s
             AND e.status = 'Active'
             AND (au.roles IS NULL OR (
                 au.roles NOT LIKE %s
                 AND au.roles NOT LIKE %s
                 AND au.roles NOT LIKE %s
             ))""",
        (emp['Department'], employee_no, '%Sub-verifier%', '%Verifier%', '%Approver%')
    )
    return [r['Employee_no'] for r in rows] if rows else []


def can_apply_on_behalf(applier_no, requester_no):
    if applier_no == requester_no:
        return False

    # Get both roles
    applier_user = query_db(
        "SELECT roles FROM auth_user WHERE employee_no = %s LIMIT 1",
        (applier_no,), one=True
    )
    requester_user = query_db(
        "SELECT roles FROM auth_user WHERE employee_no = %s LIMIT 1",
        (requester_no,), one=True
    )

    applier_roles  = applier_user.get('roles', '')  if applier_user  else ''
    requester_roles = requester_user.get('roles', '') if requester_user else ''

    def has_role(roles_str, role):
        return role in roles_str

    def is_subordinate(verifier_no, employee_no, verifier_role=None):
        """Check if employee_no is a subordinate of verifier_no.
        Optionally filter by verifier_role ('Sub-Verifier' or 'Verifier').
        """
        if verifier_role:
            row = query_db(
                """SELECT id FROM verifier_group
                   WHERE verifier_no = %s
                     AND employee_no = %s
                     AND verifier_role = %s
                   LIMIT 1""",
                (verifier_no, employee_no, verifier_role), one=True
            )
        else:
            row = query_db(
                """SELECT id FROM verifier_group
                   WHERE verifier_no = %s
                     AND employee_no = %s
                   LIMIT 1""",
                (verifier_no, employee_no), one=True
            )
        return row is not None

    def is_same_department(emp_a, emp_b):
        row = query_db(
            """SELECT 1 FROM employee e1
               JOIN employee e2 ON e1.department = e2.department
               WHERE e1.employee_no = %s
                 AND e2.employee_no = %s
                 AND e1.employee_no != e2.employee_no
               LIMIT 1""",
            (emp_a, emp_b), one=True
        )
        return row is not None

    # Approver: cannot apply on behalf of anyone
    if has_role(applier_roles, 'Approver'):
        return False

    # Verifier: can apply for their subordinates
    if has_role(applier_roles, 'Verifier'):
        if has_role(requester_roles, 'Approver'):
            return False
        return is_subordinate(applier_no, requester_no, verifier_role='Verifier')

    # Sub-verifier: can apply for their subordinates,
    if has_role(applier_roles, 'Sub-verifier'):
        if has_role(requester_roles, 'Verifier') or has_role(requester_roles, 'Approver'):
            return False
        return is_subordinate(applier_no, requester_no, verifier_role='Sub-Verifier')

    # Regular employee: same-dept teammates,
    if any(has_role(requester_roles, r) for r in ['Sub-verifier', 'Verifier', 'Approver']):
        return False

    return is_same_department(applier_no, requester_no)


# ─────────────────────────────────────────────
# Access Control
# ─────────────────────────────────────────────
@app.before_request
def restrict_access():
    restricted_routes = ['/apply_leave', '/view_leaves', '/approve_leave', '/hr']
    if any(request.path.startswith(route) for route in restricted_routes):
        if 'employee_no' not in session:
            flash('Unauthorized access!', 'danger')
            return redirect('/login')
    session['last_active'] = time.time()


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if 'employee_no' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(*required_roles):
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            user_roles = session.get('roles', [])
            if not any(role in user_roles for role in required_roles):
                flash('Unauthorized access!', 'danger')
                return redirect('/dashboard')
            return f(*args, **kwargs)
        return wrapped
    return decorator


def permission_required(*required_perms):
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            user_perms = session.get('permissions', [])
            if not any(p in user_perms for p in required_perms):
                flash('Unauthorized access!', 'danger')
                return redirect('/dashboard')
            return f(*args, **kwargs)
        return wrapped
    return decorator


def hr_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if 'HR' not in session.get('roles', []) and 'Admin' not in session.get('roles', []):
            flash('HR access required!', 'danger')
            return redirect('/dashboard')
        return f(*args, **kwargs)
    return wrapped


# ─────────────────────────────────────────────
# Context Processors
# ─────────────────────────────────────────────
@app.context_processor
def inject_user_data():
    return {
        'permissions': session.get('permissions', []),
        'roles': session.get('roles', []),
        'employee_name': session.get('employee_name', 'Guest'),
        'employee_no': session.get('employee_no'),
        'department': session.get('department')
    }


@app.context_processor
def inject_manage_leave_badge():
    if 'employee_no' not in session:
        return dict(has_pending_manage_leave=False)

    roles = session.get('roles', [])
    employee_no = session.get('employee_no')
    has_pending = False
    subordinates = []

    # Sub-verifier: check V1 queue
    if 'Sub-verifier' in roles and not has_pending:
        subordinates = get_subordinates_for(employee_no)
        if subordinates:
            fmt = ','.join(['%s'] * len(subordinates))
            row = query_db(f"""
                SELECT 1 FROM leave_request
                WHERE status = 'Pending'
                    AND requester IN ({fmt})
                    AND recommender = %s
                    AND recommender_status = 'Pending'
                LIMIT 1
            """, (*subordinates, employee_no), one=True)
            if row:
                has_pending = True

    # Verifier: check both V1 and V2 queues separately
    if 'Verifier' in roles and not has_pending:
        subordinates = get_subordinates_for(employee_no)

        # V2 (Verifier as second acknowledger)
        row = query_db("""
            SELECT 1 FROM leave_request
            WHERE status = 'Pending'
                AND recommender_2 = %s
                AND recommender_2_status = 'Pending'
                AND (
                    recommender_status = 'Acknowledged'
                    OR recommender IS NULL
                    OR recommender_status = ''
                )
            LIMIT 1
        """, (employee_no,), one=True)
        if row:
            has_pending = True

        # V1 
        if not has_pending and subordinates:
            fmt = ','.join(['%s'] * len(subordinates))
            row = query_db(f"""
                SELECT 1 FROM leave_request
                WHERE status = 'Pending'
                    AND requester IN ({fmt})
                    AND recommender = %s
                    AND recommender_status = 'Pending'
                LIMIT 1
            """, (*subordinates, employee_no), one=True)
            if row:
                has_pending = True

    # Approver
    if 'Approver' in roles and not has_pending:
        row = query_db("""
            SELECT 1 FROM leave_request
            WHERE status = 'Pending'
                AND approver_status = 'Pending'
                AND (
                    (recommender IS NULL AND recommender_2 IS NULL)
                    OR (
                       recommender IS NOT NULL
                       AND recommender_status = 'Acknowledged'
                       AND (recommender_2 IS NULL OR recommender_2_status = 'Acknowledged')
                    )
                )
            LIMIT 1
        """, one=True)
        if row:
            has_pending = True

    return dict(has_pending_manage_leave=has_pending)


# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────
def calculate_leave_days(start_date, end_date, remark, start_remark, end_remark):
    s = datetime.strptime(start_date, "%Y-%m-%d").date()
    e = datetime.strptime(end_date, "%Y-%m-%d").date()

    def is_half_day(val):
        if not val:
            return False
        val_lower = str(val).lower()
        return 'half day' in val_lower or '0.5' in val_lower

    holiday_rows = query_db("""
        SELECT start_date, end_date FROM public_holidays
        WHERE status = 'Active'
          AND end_date >= %s AND start_date <= %s
    """, (start_date, end_date))

    public_holiday_dates = set()
    for row in holiday_rows:
        current = row['start_date']
        while current <= row['end_date']:
            public_holiday_dates.add(current)
            current += timedelta(days=1)

    count = 0
    current = s
    while current <= e:
        if current.weekday() < 5 and current not in public_holiday_dates:
            count += 1
        current += timedelta(days=1)

    if s == e:
        if count == 0:
            return 0.0
        return 0.5 if is_half_day(remark) else 1.0

    start_val = 0.5 if is_half_day(start_remark) else 1.0
    end_val = 0.5 if is_half_day(end_remark) else 1.0
    middle_days = max(count - 2, 0)
    return float(middle_days + start_val + end_val)


def is_on_leave(employee_no):
    row = query_db("""
        SELECT 1 FROM leave_request
        WHERE requester = %s
          AND status = 'Approved'
          AND CURDATE() BETWEEN start_date AND end_date
        LIMIT 1
    """, (employee_no,), one=True)
    return row is not None


def is_primary_approver_on_leave():
    return is_on_leave(PRIMARY_APPROVER)

def get_active_approver_employee_no():
    if not is_primary_approver_on_leave():
        return PRIMARY_APPROVER
    return None

def is_backup_approver(employee_no):
    return employee_no in BACKUP_APPROVERS


# ─────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────
EMAIL_ADDRESS = 'esystem@excel-rim.com'
EMAIL_PASSWORD = 'Ersystem@2024$$'
SMTP_SERVER = 'mail.excel-rim.com'
SMTP_PORT = 587
SYSTEM_LINK = "http://192.168.110.99:916/calendar"


# ─────────────────────────────────────────────
# Backup Approver Config
# ─────────────────────────────────────────────
PRIMARY_APPROVER  = 'ER0001' 
BACKUP_APPROVERS  = ['ER0948', 'ER0742'] 

def send_email(sender, recipients, subject, body):
    try:
        if isinstance(recipients, str):
            recipients = [recipients]
        msg = MIMEMultipart()
        msg['From'] = sender
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(sender, recipients, msg.as_string())
        logging.info(f"Email sent to {', '.join(recipients)}")
    except Exception as e:
        logging.error(f"Failed to send email: {str(e)}")
        traceback.print_exc()
        return False


def build_leave_email(requester, requester_name, leave_type, leave_reason, start_date, end_date, no_days, role_label="Verifier"):
    return f"""
        <html>
            <body style="font-family: Times New Roman; font-size: 12px; color: #000;">
                <p>Dear {role_label},</p>
                <p>A leave request has been submitted and requires your attention.</p>
                <p style="line-height: 1.8;">
                    Requester&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <strong>{requester} - {requester_name}</strong><br>
                    Leave Type&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <strong>{leave_type}</strong><br>
                    Leave Reason&nbsp;: <strong>{leave_reason}</strong><br>
                    Date&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: <strong>{start_date} to {end_date}</strong><br>
                    No. of Days&nbsp;&nbsp;&nbsp;&nbsp;: <strong>{no_days}</strong>
                </p>

                <p>
                    Please click 
                    <a href="{SYSTEM_LINK}" style="color: #007bff; font-weight: bold;">
                        here
                    </a> 
                    to review and acknowledge the request.
                </p>

                <br>

                <p>
                    Thanks & Regards<br>
                    Excel Rim
                </p>

                <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">

                <p style="font-size: 12px; color: #999;">
                    This email is auto-generated by system. Please do not reply to this email.
                </p>

            </body>
        </html>
    """


def build_pending_approval_email(leaves, backup_mode=False):
    rows_html = ""
    for i, leave in enumerate(leaves, 1):
        bg = '#f9f9f9' if i % 2 == 0 else '#ffffff'
        # Tag pre-approved leaves differently
        pre_status = leave.get('pre_approver_status') or ''
        pre_badge = (
            '<span style="background:#28a745;color:#fff;padding:2px 7px;'
            'border-radius:10px;font-size:11px;margin-left:6px;">Pre-Approved</span>'
            if pre_status == 'Pre-Approved' else ''
        )
        rows_html += f"""
            <tr style="background-color: {bg};">
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{i}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['requester']}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['requester_name']}{pre_badge}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['department'] or '-'}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['leave_type']}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['leave_reason']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['start_date']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['end_date']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['no_days']}</td>
            </tr>
        """

    backup_notice = ""
    if backup_mode:
        backup_notice = """
            <div style="background:#fff3cd;border:1px solid #ffc107;padding:10px 16px;
                        border-radius:6px;margin-bottom:16px;font-size:13px;">
                <strong>Backup Approver:</strong> The primary approver is currently on leave.
                Backup approvers may pre-approve requests. Primary approver will still need to
                give final approval upon return.
            </div>
        """

    action_label = "pre-approve" if backup_mode else "review and process"

    return f"""
    <html>
        <body style="font-family: Times New Roman, sans-serif; color: #333;">
            <p>Dear Approver,</p>
            {backup_notice}
            <p>The following leaves have been acknowledged and are currently awaiting your approval.</p>
            <table style="border-collapse: collapse; width: 100%; font-size: 13px;">
                <thead>
                    <tr style="background:#f2f2f2;">
                        <th style="padding: 10px; border: 1px solid #ddd;">No.</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Emp. No</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Emp. Name</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Dept.</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Leave Type</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Leave Reason</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Start Date</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">End Date</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Day(s)</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
            <br>
            <p>Please click <a href="{SYSTEM_LINK}" style="color: #007bff; font-weight: bold;">here</a>
               to {action_label} the requests.</p>
            <br>
            <p>Thanks & Regards,<br><strong>Excel Rim HR System</strong></p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="color: #999; font-size: 12px;">This email is auto-generated by system. Please do not reply to this email.</p>
        </body>
    </html>
    """


def send_pending_approval_email():
    pending_leaves = query_db("""
        SELECT lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.leave_reason, lr.start_date, lr.end_date, lr.no_days, 
               lr.pre_approver_status
        FROM leave_request lr
        WHERE lr.recommender_status IN ('Acknowledged', '')
          AND (lr.recommender_2_status IN ('Acknowledged', '') OR lr.recommender_2 IS NULL)
          AND lr.approver_status = 'Pending'
          AND lr.status = 'Pending'
        ORDER BY lr.created_at ASC
    """)
    if not pending_leaves:
        return

    primary_on_leave = is_primary_approver_on_leave()

    primary_row = query_db("""
        SELECT e.Email FROM employee e
        JOIN auth_user au ON e.Employee_no = au.employee_no
        WHERE au.employee_no = %s AND au.status = 'Active'
        LIMIT 1
    """, (PRIMARY_APPROVER,), one=True)

    if not primary_row or not primary_row['Email']:
        return

    recipients = [primary_row['Email']]

    if primary_on_leave:
        # Add backup approvers as additional recipients
        for backup_no in BACKUP_APPROVERS:
            backup_row = query_db("""
                SELECT e.Email FROM employee e
                JOIN auth_user au ON e.Employee_no = au.employee_no
                WHERE e.Employee_no = %s AND au.status = 'Active'
                LIMIT 1
            """, (backup_no,), one=True)
            if backup_row and backup_row['Email'] and not is_on_leave(backup_no):
                recipients.append(backup_row['Email'])

    body = build_pending_approval_email(pending_leaves, primary_on_leave)
    subject = (
        f"[Daily Reminder] {len(pending_leaves)} Leave Request(s) Pending Approval"
        if not primary_on_leave
        else f"[Daily Reminder] {len(pending_leaves)} Leave Request(s) Pending Pre-Approval (Backup Mode)"
    )
    send_email(EMAIL_ADDRESS, recipients, subject, body)


# ─────────────────────────────────────────────
# Unified Leave Email Sender
# ─────────────────────────────────────────────
def notify_leave_submitted(leave_id=None, requester=None, requester_name=None,
                            leave_type=None, reason=None,
                            start_date=None, end_date=None, no_days=None,
                            recipient_emp_no=None, role_label="Verifier"):
    
    if not recipient_emp_no:
        return

    if is_on_leave(recipient_emp_no):
        return

    rec_row = query_db(
        "SELECT Email FROM employee WHERE Employee_no = %s",
        (recipient_emp_no,), one=True
    )
    if not rec_row or not rec_row['Email']:
        return

    email_body = build_leave_email(
        requester, requester_name,
        leave_type, reason,
        start_date, end_date, no_days,
        role_label=role_label
    )
    subject = f"Leave Request Submitted - {requester_name}"
    send_email(EMAIL_ADDRESS, rec_row['Email'], subject, email_body)

# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone='Asia/Kuala_Lumpur')
scheduler.add_job(
    send_pending_approval_email,
    trigger='cron',
    day_of_week='mon-fri',
    hour='8,14',
    minute=0,
    id='pending_approval_digest',
    replace_existing=True
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# ─────────────────────────────────────────────
# Auto-create leave (holidays)
# ─────────────────────────────────────────────
def auto_create_leave_requests(holiday_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM public_holidays WHERE holiday_id = %s AND auto_deduct = TRUE", (holiday_id,))
        holiday = cursor.fetchone()
        if not holiday:
            return {'success': False, 'message': 'Holiday not found or auto-deduct disabled.'}

        cursor.execute("SELECT * FROM recommended_leave WHERE holiday_id = %s ORDER BY leave_date ASC", (holiday_id,))
        recommended_dates = cursor.fetchall()
        if not recommended_dates:
            return {'success': False, 'message': 'No recommended dates found'}

        cursor.execute("SELECT Employee_no, Employee_name, Register_date FROM employee WHERE status = 'Active'")
        employees = cursor.fetchall()

        results = {'success': True, 'total_employees': len(employees), 'total_dates': len(recommended_dates),
                   'created_requests': 0, 'skipped': 0, 'errors': []}
        current_year = date.today().year

        for emp in employees:
            employee_no = emp['Employee_no']
            employee_name = emp['Employee_name']
            register_date = emp['Register_date']
            al_balance = calculate_employee_al_balance(employee_no, register_date, current_year, cursor)

            for rec_date in recommended_dates:
                leave_date = rec_date['leave_date']
                leave_priority = rec_date['leave_priority']

                cursor.execute("""
                    SELECT request_id FROM leave_request
                    WHERE requester = %s AND start_date <= %s AND end_date >= %s
                """, (employee_no, leave_date, leave_date))
                if cursor.fetchone():
                    results['skipped'] += 1
                    continue

                leave_type, days_to_deduct = determine_leave_type(leave_priority, al_balance, 1.0)

                try:
                    cursor.execute("""
                        INSERT INTO leave_request
                        (requester, requester_name, leave_type, start_date, end_date, no_days, leave_reason, status, applied_date, auto_created, related_holiday_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), TRUE, %s)
                    """, (employee_no, employee_name, leave_type, leave_date, leave_date, 1.0,
                          f"Auto-created for {holiday['holiday_name']} - Company advised leave",
                          'Approved', holiday_id))
                    results['created_requests'] += 1
                    if leave_type == 'Annual Leave':
                        al_balance -= 1.0
                except Exception as e:
                    results['errors'].append(f"{employee_name}: {str(e)}")

        conn.commit()
        return results
    except Exception as e:
        conn.rollback()
        return {'success': False, 'message': str(e)}
    finally:
        cursor.close()
        conn.close()


def calculate_employee_al_balance(employee_no, register_date, current_year, cursor):
    today = date.today()
    years_of_service = (today - register_date).days / 365.25
    if years_of_service < 2:
        annual_leave_per_year = 10
    elif years_of_service < 5:
        annual_leave_per_year = 14
    else:
        annual_leave_per_year = 18

    monthly_accrual_rate = annual_leave_per_year / 12
    if register_date.year == current_year:
        months_worked = (today.month - register_date.month) + 1
    else:
        months_worked = today.month
    accrued_al = round(monthly_accrual_rate * months_worked, 1)

    cursor.execute("""
        SELECT carried_days FROM leave_carryforward
        WHERE employee_no = %s AND year = %s AND status = 'Active'
    """, (employee_no, current_year - 1))
    carryforward = cursor.fetchone()
    carryforward_balance = float(carryforward['carried_days']) if carryforward else 0
    total_entitlement = accrued_al + carryforward_balance

    cursor.execute("""
        SELECT COALESCE(SUM(no_days), 0) as used_days FROM leave_request
        WHERE requester = %s AND leave_type = 'Annual Leave'
          AND status = 'Approved' AND YEAR(start_date) = %s
    """, (employee_no, current_year))
    used = cursor.fetchone()
    used_days = float(used['used_days']) if used else 0
    return max(0, total_entitlement - used_days)


def determine_leave_type(leave_priority, al_balance, days_needed):
    if leave_priority == 'AL_ONLY':
        return ('Annual Leave', days_needed)
    elif leave_priority == 'UPL_ONLY':
        return ('Unpaid Leave', days_needed)
    elif leave_priority == 'AL_FIRST':
        if al_balance >= days_needed:
            return ('Annual Leave', days_needed)
        else:
            return ('Unpaid Leave', days_needed)
    return ('Unpaid Leave', days_needed)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@app.route('/')
def home():
    if 'employee_no' in session:
        return redirect('/dashboard')
    return redirect('/calendar')


@app.route('/calendar')
def calendar():
    is_logged_in = 'employee_no' in session
    employee_name = session.get('employee_name', '')
    return render_template('calendar.html', is_logged_in=is_logged_in, employee_name=employee_name)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()

        if not username or not password:
            flash('Please enter username and password', 'danger')
            return redirect('/login')

        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("""
            SELECT au.ID, au.username, au.password, au.employee_no, au.roles, au.department,
                   e.Employee_name
            FROM auth_user au
            LEFT JOIN employee e ON e.Employee_no = au.employee_no
            WHERE au.username = %s AND au.status = 'Active'
        """, (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and user['password'] == password:
            session.clear()
            session.permanent = True
            session['user_id'] = user['ID']
            session['username'] = user['username']
            session['employee_no'] = user['employee_no']

            roles = [r.strip() for r in user['roles'].split(',')] if user['roles'] else []
            session['roles'] = roles

            permissions = set()
            for role in roles:
                permissions.update(ROLE_PERMISSIONS.get(role, []))
            session['permissions'] = list(permissions)

            session['department'] = user['department']
            session['employee_name'] = user.get('Employee_name') or user['username']

            flash(f'Welcome back, {user["username"]}!', 'success')
            return redirect('/dashboard')

        flash('Invalid username or password', 'danger')
        return redirect('/login')

    return render_template('calendar.html', messages=get_flashed_messages(with_categories=True))


@app.route('/dashboard')
@login_required
def dashboard():
    employee_no = session['employee_no']
    username = session['username']
    roles = session.get('roles', [])
    employee_name = session.get('employee_name', username)

    per_page = 7
    current_page = request.args.get('page', 1, type=int)
    filter_status = request.args.get('status', '').strip()
    filter_leave_type = request.args.get('leave_type', '').strip()
    filter_month = request.args.get('month', '', type=int)
    filter_year = request.args.get('year', '', type=int)
    filter_date_from = request.args.get('date_from', '').strip()
    filter_date_to = request.args.get('date_to', '').strip()

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT *, COALESCE(no_days, 0) AS no_days
        FROM leave_request
        WHERE requester=%s AND status != 'Cancel'
        ORDER BY created_at DESC
    """, (employee_no,))
    leave_requests = cursor.fetchall()

    is_verifier   = 'Verifier' in roles
    is_subverifier = 'Sub-verifier' in roles
    is_approver   = 'Approver' in roles

    for leave in leave_requests:
        can_edit = False

        if is_approver:
                can_edit = leave.get('approver_status') == 'Pending'
        elif is_verifier and not is_subverifier:
            can_edit = leave.get('display_status') == 'Acknowledged'
        else:
            # Employee, Sub-verifier editing own leave
            can_edit = leave.get('recommender_status') == 'Pending'

        leave['can_edit'] = can_edit

        # Display status
        if leave['status'] == 'Pending':
            if leave.get('recommender_status') == 'Rejected':
                leave['display_status'] = 'Rejected'
            elif leave.get('recommender_status') in ('Acknowledged', ''):
                leave['display_status'] = 'Acknowledged'
            else:
                leave['display_status'] = 'Pending'
        else:
            leave['display_status'] = leave['status']

    filtered_leaves = leave_requests
    if filter_status:
        filtered_leaves = [l for l in filtered_leaves if l['display_status'] == filter_status]
    if filter_leave_type:
        filtered_leaves = [l for l in filtered_leaves if l.get('leave_type') == filter_leave_type]
    if filter_month:
        filtered_leaves = [l for l in filtered_leaves
                           if isinstance(l.get('start_date'), date) and l['start_date'].month == filter_month]
    if filter_year:
        filtered_leaves = [l for l in filtered_leaves
                           if isinstance(l.get('start_date'), date) and l['start_date'].year == filter_year]
    if filter_date_from:
        try:
            date_from = datetime.strptime(filter_date_from, '%Y-%m-%d').date()
            filtered_leaves = [l for l in filtered_leaves
                                if isinstance(l.get('start_date'), date) and l['start_date'] >= date_from]
        except ValueError:
            pass
    if filter_date_to:
        try:
            date_to = datetime.strptime(filter_date_to, '%Y-%m-%d').date()
            filtered_leaves = [l for l in filtered_leaves
                                if isinstance(l.get('start_date'), date) and l['start_date'] <= date_to]
        except ValueError:
            pass

    total_leaves = len(filtered_leaves)
    total_pages = max(1, (total_leaves + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    start_idx = (current_page - 1) * per_page
    paginated_leaves = filtered_leaves[start_idx:start_idx + per_page]

    today = date.today()
    upcoming_leaves = [l for l in leave_requests if l['status'] == 'Approved' and l['start_date'] >= today]

    cursor.close()
    conn.close()

    return render_template(
        'dashboard.html',
        employee_name=employee_name,
        username=username,
        roles=roles,
        leave_requests=leave_requests,
        default_leaves=filtered_leaves,
        paginated_leaves=paginated_leaves,
        upcoming_leaves=upcoming_leaves,
        selected_status=[filter_status] if filter_status else [],
        current_page=current_page,
        total_pages=total_pages,
        total_leaves=total_leaves,
        per_page=per_page,
        current_year=datetime.now().year,
        filter_status=filter_status,
        filter_leave_type=filter_leave_type,
        filter_month=filter_month,
        filter_year=filter_year,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to
    )


@app.route('/view_leave')
@login_required
@role_required('Sub-verifier', 'Verifier', 'Approver')
def view_leave():
    roles = session.get('roles', [])
    employee_no = session.get('employee_no')

    verifier_leaves     = []
    approver_leaves     = []
    pre_approved_leaves = []
    primary_on_leave    = is_primary_approver_on_leave()

    # Sub-verifier / Verifier
    if 'Sub-verifier' in roles or 'Verifier' in roles:
        subordinates = get_subordinates_for(employee_no)

        v2_leaves = query_db("""
            SELECT lr.*, e.Employee_name AS requester_name
            FROM leave_request lr
            JOIN employee e ON lr.requester = e.Employee_no
            WHERE lr.status = 'Pending'
                AND lr.recommender_2 = %s
                AND lr.recommender_2_status = 'Pending'
                AND (
                    lr.recommender_status = 'Acknowledged'
                    OR lr.recommender IS NULL
                    OR lr.recommender_status = ''
                )
            ORDER BY lr.created_at DESC
        """, (employee_no,))

        v1_leaves = []
        if subordinates:
            fmt = ','.join(['%s'] * len(subordinates))
            v1_leaves = query_db(f"""
                SELECT lr.*, e.Employee_name AS requester_name
                FROM leave_request lr
                JOIN employee e ON lr.requester = e.Employee_no
                WHERE lr.status = 'Pending'
                    AND lr.requester IN ({fmt})
                    AND lr.recommender = %s
                    AND lr.recommender_status = 'Pending'
                ORDER BY lr.created_at DESC
            """, (*subordinates, employee_no))

        seen = set()
        verifier_leaves = []
        for leave in v1_leaves + v2_leaves:
            if leave['leave_id'] not in seen:
                seen.add(leave['leave_id'])
                verifier_leaves.append(leave)

    # Approver
    if 'Approver' in roles:
        approver_dept = session.get('department')

        if is_backup_approver(employee_no):
            backup_dept = session.get('department')
            all_approver_leaves = query_db("""
                SELECT lr.*, e.Employee_name AS requester_name
                FROM leave_request lr
                JOIN employee e ON lr.requester = e.Employee_no
                WHERE lr.approver_status = 'Pending'
                    AND lr.status NOT IN ('Rejected', 'Cancel')
                    AND lr.recommender_status IN ('Acknowledged', '')
                    AND (lr.recommender_2_status IN ('Acknowledged', '') OR lr.recommender_2 IS NULL)
                    AND (lr.pre_approver_status IS NULL OR lr.pre_approver_status = '')
                ORDER BY lr.created_at DESC
            """)
            approver_leaves = [
                l for l in all_approver_leaves
                if l['requester'] != employee_no
                and l.get('department') != backup_dept
            ]

        elif employee_no == PRIMARY_APPROVER or not is_backup_approver(employee_no):
            all_approver_leaves = query_db("""
                SELECT lr.*, e.Employee_name AS requester_name,
                       ep.Employee_name AS pre_approved_by_name
                FROM leave_request lr
                JOIN employee e ON lr.requester = e.Employee_no
                LEFT JOIN employee ep ON lr.pre_approved_by = ep.Employee_no
                WHERE lr.approver_status = 'Pending'
                    AND lr.status NOT IN ('Rejected', 'Cancel')
                    AND lr.recommender_status IN ('Acknowledged', '')
                    AND (lr.recommender_2_status IN ('Acknowledged', '') OR lr.recommender_2 IS NULL)
                ORDER BY lr.created_at DESC
            """)

            if employee_no == PRIMARY_APPROVER:
                all_filtered = all_approver_leaves
            else:
                all_filtered = [
                    l for l in all_approver_leaves
                    if l['requester'] != employee_no
                    and l.get('department') != approver_dept
                ]

            approver_leaves     = [l for l in all_filtered if not l.get('pre_approver_status')]
            pre_approved_leaves = [l for l in all_filtered if l.get('pre_approver_status') == 'Pre-Approved']

    all_leave_ids = [l['leave_id'] for l in verifier_leaves + approver_leaves + pre_approved_leaves]
    leaves_with_docs = set()
    if all_leave_ids:
        fmt = ','.join(['%s'] * len(all_leave_ids))
        doc_rows = query_db(
            f"SELECT DISTINCT leave_id FROM leave_documents WHERE leave_id IN ({fmt})",
            all_leave_ids
        )
        leaves_with_docs = {r['leave_id'] for r in doc_rows}
    # ────────────────────────────────────────────────────────────────────────

    return render_template(
        'manage_request.html',
        verifier_leaves=verifier_leaves,
        approver_leaves=approver_leaves,
        pre_approved_leaves=pre_approved_leaves,
        roles=roles,
        employee_no=employee_no,
        primary_on_leave=primary_on_leave,
        is_backup_approver=is_backup_approver(employee_no),
        PRIMARY_APPROVER=PRIMARY_APPROVER,
        BACKUP_APPROVERS=BACKUP_APPROVERS,
        leaves_with_docs=leaves_with_docs,
    )


@app.route('/apply_leave', methods=['GET', 'POST'])
@login_required
def apply_leave():
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])
    department = session.get('department')
    employee_name = session.get('employee_name')

    can_apply_behalf = 'leave.apply_behalf' in session.get('permissions', [])

    # Fetch subordinates this user can apply on behalf of
    subordinates = []
    if can_apply_behalf:
        subordinate_nos = get_subordinates_for(employee_no)
        if subordinate_nos:
            fmt = ','.join(['%s'] * len(subordinate_nos))
            subordinates = query_db(f"""
                SELECT e.Employee_no, e.Employee_name
                FROM employee e
                WHERE e.Employee_no IN ({fmt}) AND e.status = 'Active'
                ORDER BY e.Employee_name
            """, tuple(subordinate_nos))

    # Fetch Approver
    approver_row = query_db(
        "SELECT employee_no FROM auth_user WHERE roles LIKE %s AND status = 'Active' LIMIT 1",
        ('%Approver%',), one=True
    )
    approver = approver_row['employee_no'] if approver_row else None

    if request.method == 'GET':
        return render_template(
            'apply_leave.html',
            employee_name=employee_name,
            roles=roles,
            subordinates=subordinates,
            can_apply_behalf=can_apply_behalf,
            max_files=MAX_FILES,
            max_file_mb=MAX_FILE_SIZE // (1024 * 1024),
            max_total_mb=MAX_TOTAL_SIZE // (1024 * 1024),
        )

    # ── POST ──
    requester = request.form.get('requester_emp_no') if can_apply_behalf else employee_no
    if not requester:
        requester = employee_no
    is_self_apply = (requester == employee_no)

    # Validate on-behalf permission
    if not is_self_apply and not can_apply_on_behalf(employee_no, requester):
        flash('You are not authorized to apply leave on behalf of this employee.', 'danger')
        return redirect('/apply_leave')

    # Get requester info
    emp_row = query_db(
        "SELECT Employee_name, Department FROM employee WHERE Employee_no = %s AND status = 'Active'",
        (requester,), one=True
    )
    if not emp_row:
        flash('Employee information not found.', 'danger')
        return redirect('/apply_leave')
    
    requester_name = emp_row['Employee_name']
    department = emp_row['Department']
 
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Please provide a leave reason.', 'danger')
        return redirect('/apply_leave')
 
    start_date_str = request.form.get('start_date', '').strip()
    end_date_str   = request.form.get('end_date', '').strip()
    if not start_date_str or not end_date_str:
        flash('Please provide both start and end dates.', 'danger')
        return redirect('/apply_leave')
 
    duplicate = check_duplicate_leave(requester, start_date_str, end_date_str)
    if duplicate:
        flash(
            f'A leave request already exists for this period '
            f'({duplicate["start_date"]} to {duplicate["end_date"]}, '
            f'Status: {duplicate["status"]}). '
            f'Please cancel the existing request first.',
            'danger'
        )
        return redirect('/apply_leave')
 
    start_remark = request.form.get('start_remark') or None
    end_remark   = request.form.get('end_remark') or None
 
    # Multiple file upload with size validation and compression
    files = request.files.getlist('upload_documents')
    files = [f for f in files if f and f.filename]
 
    if len(files) > MAX_FILES:
        flash(f'Maximum {MAX_FILES} files allowed per request.', 'danger')
        return redirect('/apply_leave')
 
    for f in files:
            f.seek(0)
            raw = f.read()
            f.seek(0)
            ext = f.filename.rsplit('.', 1)[1].lower()
            if ext == 'pdf' and len(raw) > MAX_FILE_SIZE:
                flash(
                    f'"{f.filename}" ({len(raw)/1024/1024:.2f}MB) exceeds the 1MB PDF limit.',
                    'danger'
                )
                return redirect('/apply_leave')
 
    no_days = calculate_leave_days(
        start_date_str, end_date_str,
        request.form.get('remark'), start_remark, end_remark
    )
 
    recommender = None
    recommender_status = ''
    recommender_2 = None
    recommender_2_status = ''
    approver_status = 'Pending'
    status = 'Pending'

    assigned_verifier = get_verifier_for(requester)

    if is_self_apply: 
        sv = get_verifier_for(requester, role='Sub-verifier')
        v  = get_verifier_for(requester, role='Verifier')

        recommender = sv if sv else v         # V1: Sub-verifier if exists, else Verifier
        recommender_status = 'Pending' if recommender else ''
        recommender_2 = v if sv else None        # V2: Verifier, only if SV exists
        recommender_2_status = 'Pending' if recommender_2 else ''
 
    elif not is_self_apply:
        # Apply on behalf
        if 'Verifier' in roles and 'Sub-verifier' not in roles:
            # Verifier applying for subordinate → skip V1, Verifier becomes V2 directly
            recommender = employee_no
            recommender_status = 'Pending'
            recommender_2 = None
            recommender_2_status = ''

        elif assigned_verifier == employee_no:
            # Sub-verifier IS the requester's assigned verifier
            # → V1 = applier (sub-verifier), V2 = verifier 
            recommender = employee_no
            recommender_status = 'Pending'
            recommender_2 = get_verifier_for(employee_no, role='Verifier')
            recommender_2_status = 'Pending' if recommender_2 else ''

        else:
            # Regular employee applying for same-dept teammate
            sv = get_verifier_for(requester, role='Sub-verifier')
            v  = get_verifier_for(requester, role='Verifier')
            recommender          = sv if sv else v
            recommender_status   = 'Pending' if recommender else ''
            recommender_2        = v if sv else None
            recommender_2_status = 'Pending' if recommender_2 else ''
 
    # Insert leave request
    conn = get_db()
    cursor = conn.cursor()
    processed_files = []
    try:
        cursor.execute("""
            INSERT INTO leave_request
                (requester, requester_name, applied_by, department, created_at, leave_type, leave_reason,
                start_date, end_date, no_days, remark, start_remark, end_remark, 
                recommender, recommender_status, recommender_2, recommender_2_status,
                approver, approver_status, status, status_update)
            VALUES (%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,NOW())
        """, (
            requester, requester_name, employee_no, department,
            request.form['leave_type'], reason,
            start_date_str, end_date_str,
            no_days, request.form.get('remark'),
            start_remark, end_remark,
            recommender, recommender_status,
            recommender_2, recommender_2_status,
            approver, approver_status, status
        ))
        leave_id = cursor.lastrowid
 
        # Save documents to leave_documents table
        original_names = []
        for i, file in enumerate(files, start=1):
            filename, size = process_and_save_file(
                file, requester, start_date_str, department,
                leave_id=leave_id, index=i
            )
            processed_files.append((filename, size))
            original_names.append(file.filename)

        if processed_files:
            save_leave_documents(conn, cursor, leave_id, processed_files, original_names)
 
        conn.commit()
        flash('Leave request submitted successfully!', 'success')
 
        # Email notification
        leave_type_submitted = request.form['leave_type']

        # Scenario 1: Applied on behalf → no email
        if not is_self_apply:
            pass

        # Scenario 2: Verifier self-apply → email Approver directly
        elif is_self_apply and 'Verifier' in roles and 'Sub-verifier' not in roles:
            notify_leave_submitted(
                requester=requester, requester_name=requester_name,
                leave_type=leave_type_submitted, reason=reason,
                start_date=start_date_str, end_date=end_date_str, no_days=no_days,
                recipient_emp_no=approver,
                role_label="Approver"
            )

        # Scenario 3: Sub-verifier self-apply → email their Verifier
        elif is_self_apply and 'Sub-verifier' in roles:
            notify_leave_submitted(
                requester=requester, requester_name=requester_name,
                leave_type=leave_type_submitted, reason=reason,
                start_date=start_date_str, end_date=end_date_str, no_days=no_days,
                recipient_emp_no=recommender,
                role_label="Verifier"
            )

        # Scenario 4: Regular employee self-apply → email Sub-verifier/Verifier
        elif is_self_apply and 'Employee' in roles:
            if recommender:
                rec_role_row = query_db(
                    "SELECT roles FROM auth_user WHERE employee_no = %s",
                    (recommender,), one=True
                )
                role_label = 'Sub-verifier' if rec_role_row and 'Sub-verifier' in (rec_role_row['roles'] or '') else 'Verifier'
                notify_leave_submitted(
                    requester=requester, requester_name=requester_name,
                    leave_type=leave_type_submitted, reason=reason,
                    start_date=start_date_str, end_date=end_date_str, no_days=no_days,
                    recipient_emp_no=recommender,
                    role_label=role_label
                )

    except Exception as e:
        conn.rollback()
        # Clean up saved files on DB error
        for fname, _ in processed_files:
            fp = os.path.join(UPLOAD_FOLDER, fname)
            if os.path.exists(fp):
                os.remove(fp)
        flash(f'Error submitting leave request: {str(e)}', 'danger')
        return redirect('/apply_leave')
    finally:
        cursor.close()
        conn.close()
 
    return redirect('/dashboard')


@app.route('/check_date', methods=['GET'])
@login_required
def check_date():
    employee_no = request.args.get('employee_no') or session.get('employee_no')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    exclude_leave_id = request.args.get('exclude_leave_id', type=int)

    if not start_date or not end_date:
        return jsonify({'clash': False})

    duplicate = check_duplicate_leave(employee_no, start_date, end_date, exclude_leave_id)

    if duplicate:
        return jsonify({
            'clash': True,
            'message': (
                f"Leave request already exists: {duplicate['start_date']} to {duplicate['end_date']}.<br>"
                f"Please insert another date or update the existing leave request."
            )
        })

    return jsonify({'clash': False})


@app.route('/update_leave/<int:leave_id>', methods=['POST'])
@login_required
def update_leave(leave_id):
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])
 
    leave_type = request.form.get('leave_type')
    leave_reason = request.form.get('leave_reason', '').strip()
    if not leave_reason:
        flash('Please provide a leave reason.', 'danger')
        return redirect('/dashboard')
 
    start_date = request.form.get('start_date')
    end_date = request.form.get('end_date')
    remark = request.form.get('remark', '')
    start_remark = request.form.get('start_remark', '')
    end_remark = request.form.get('end_remark', '')
    no_days = calculate_leave_days(start_date, end_date, remark, start_remark, end_remark)
 
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    processed_files = []
 
    try:
        cursor.execute("""
            SELECT recommender_status, approver_status, requester, department
            FROM leave_request
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))
        row = cursor.fetchone()
 
        if not row:
            flash('Only Pending leave requests can be edited.', 'warning')
            return redirect('/dashboard')
 
        # Permission check
        can_edit = False
        if 'Approver' in roles:
            can_edit = row.get('approver_status') == 'Pending'
        elif row['requester'] == employee_no:
            if 'Verifier' in roles and 'Sub-verifier' not in roles:
                # Verifier editing own leave — only when Acknowledged
                can_edit = (
                    row.get('recommender_status') in ('Acknowledged', '')
                    and row.get('approver_status') == 'Pending'
                )
            else:
                # Employee / Sub-verifier / editing own leave — only when Pending
                can_edit = row.get('recommender_status') == 'Pending'
 
        if not can_edit:
            flash('You do not have permission to edit this leave request.', 'danger')
            return redirect('/dashboard')
 
        # Duplicate check (exclude current leave)
        duplicate = check_duplicate_leave(row['requester'], start_date, end_date, exclude_leave_id=leave_id)
        if duplicate:
            flash(
                f'A leave request already exists for this period '
                f'({duplicate["start_date"]} to {duplicate["end_date"]}, '
                f'Status: {duplicate["status"]}). '
                f'Please cancel the existing request first.',
                'danger'
            )
            return redirect('/dashboard')
 
        # Handle new file uploads
        files = request.files.getlist('upload_documents')
        files = [f for f in files if f and f.filename]
 
        # Count existing docs for this leave
        cursor.execute("SELECT COUNT(*) as cnt FROM leave_documents WHERE leave_id = %s", (leave_id,))
        existing_count = cursor.fetchone()['cnt']

        total_after = existing_count + len(files)
        if total_after > MAX_FILES:
            flash(
                f'Cannot add {len(files)} file(s). '
                f'This request already has {existing_count} document(s). '
                f'Maximum is {MAX_FILES} per request.',
                'danger'
            )
            return redirect('/dashboard')
 
        # Pre-check size
        total_size = 0
        for f in files:
            f.seek(0)
            total_size += len(f.read())
            f.seek(0)
        if total_size > MAX_TOTAL_SIZE:
            flash(
                f'Total new files ({total_size/1024/1024:.2f}MB) '
                f'exceed the {MAX_TOTAL_SIZE//(1024*1024)}MB limit.',
                'danger'
            )
            return redirect('/dashboard')
 
        dept = row.get('department') or session.get('department', 'General')
        original_names = []
        for i, file in enumerate(files, start=existing_count + 1):
            filename, size = process_and_save_file(
                file, row['requester'], start_date, dept,
                leave_id=leave_id, index=i
            )
            processed_files.append((filename, size))
            original_names.append(file.filename)
 
        # Reset recommender status only if employee editing own pending leave
        old_recommender_status = row['recommender_status']
        new_recommender_status = old_recommender_status
        if row['requester'] == employee_no and old_recommender_status == 'Pending':
            new_recommender_status = 'Pending'
 
        cursor.execute("""
            UPDATE leave_request
            SET leave_type=%s, leave_reason=%s, start_date=%s, end_date=%s, no_days=%s,
                remark=%s, start_remark=%s, end_remark=%s,
                recommender_status=%s, status_update=NOW()
            WHERE leave_id=%s AND status='Pending'
        """, (
            leave_type, leave_reason, start_date, end_date, no_days,
            remark, start_remark, end_remark,
            new_recommender_status, leave_id
        ))
 
        # Save new documents
        if processed_files:
            save_leave_documents(conn, cursor, leave_id, processed_files, original_names)
 
        conn.commit()
        flash('Leave request updated successfully!', 'success')
 
    except Exception as e:
        conn.rollback()
        for fname, _ in processed_files if 'processed_files' in dir() else []:
            fp = os.path.join(UPLOAD_FOLDER, fname)
            if os.path.exists(fp):
                os.remove(fp)
        flash(f'Error updating leave request: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()
 
    return redirect('/dashboard')


@app.route('/cancel_leave/<int:leave_id>', methods=['POST'])
@login_required
def cancel_leave(leave_id):
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])
 
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
 
    try:
        cursor.execute("""
            SELECT recommender_status, approver_status, requester, recommender
            FROM leave_request
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))
        row = cursor.fetchone()
 
        if not row:
            return jsonify(success=False, message='Only Pending leave requests can be cancelled.')
 
        can_cancel = False
        if 'Approver' in roles:
            can_cancel = row.get('approver_status') == 'Pending'
        elif 'Verifier' in roles or 'Sub-verifier' in roles:
            if row.get('recommender') == employee_no:
                can_cancel = row.get('approver_status') == 'Pending'
        elif row['requester'] == employee_no:
            can_cancel = row.get('recommender_status') == 'Pending'
 
        if not can_cancel:
            return jsonify(success=False, message='You do not have permission to cancel this leave request.')
 
        # Get all documents for this leave before cancelling
        cursor.execute("SELECT filename FROM leave_documents WHERE leave_id = %s", (leave_id,))
        docs = cursor.fetchall()
 
        cursor.execute("""
            UPDATE leave_request SET status = 'Cancel', status_update = NOW()
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))
 
        # Delete leave documents
        cursor.execute("DELETE FROM leave_documents WHERE leave_id = %s", (leave_id,))
        conn.commit()

        # Delete physical files
        for doc in docs:
            fp = os.path.join(UPLOAD_FOLDER, doc['filename'])
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception as e:
                    print(f"Error deleting file: {e}")
 
        return jsonify(success=True, message='Leave request cancelled successfully!')
 
    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()


@app.route('/uploads/<filename>')
@login_required
def view_document(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/leave_documents/<int:leave_id>')
@login_required
def get_leave_documents(leave_id):
    """Return JSON list of documents for a leave request."""
    docs = query_db(
        "SELECT id, filename, original_name, file_size, uploaded_at FROM leave_documents WHERE leave_id = %s",
        (leave_id,)
    )
    result = []
    for doc in docs:
        result.append({
            'id':            doc['id'],
            'filename':      doc['filename'],
            'original_name': doc['original_name'] or doc['filename'],
            'file_size':     doc['file_size'],
            'url':           f"/uploads/{doc['filename']}",
            'uploaded_at':   str(doc['uploaded_at'])
        })
    return jsonify(result)


@app.route('/leave_document/delete/<int:doc_id>', methods=['POST'])
@login_required
def delete_leave_document(doc_id):
    """Delete a single document from leave_documents (only if leave is still Pending)."""
    employee_no = session.get('employee_no')
    roles       = session.get('roles', [])
 
    doc = query_db("""
        SELECT ld.filename, ld.leave_id, lr.requester, lr.status, lr.recommender_status
        FROM leave_documents ld
        JOIN leave_request lr ON ld.leave_id = lr.leave_id
        WHERE ld.id = %s
    """, (doc_id,), one=True)
 
    if not doc:
        return jsonify(success=False, message='Document not found.')
 
    if doc['status'] != 'Pending':
        return jsonify(success=False, message='Cannot delete documents from non-pending leave.')
 
    # Only requester or their verifier or Approver/HR can delete
    can_delete = (
        doc['requester'] == employee_no
        or 'Approver' in roles
        or 'HR' in roles
        or 'Admin' in roles
    )
    if not can_delete:
        return jsonify(success=False, message='Unauthorized.')
 
    conn   = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM leave_documents WHERE id = %s", (doc_id,))
        conn.commit()
 
        fp = os.path.join(UPLOAD_FOLDER, doc['filename'])
        if os.path.exists(fp):
            os.remove(fp)
 
        return jsonify(success=True, message='Document deleted.')
    except Exception as e:
        conn.rollback()
        return jsonify(success=False, message=str(e))
    finally:
        cursor.close()
        conn.close()

@app.route('/acknowledge_leave/<int:leave_id>/<action>', methods=['POST'])
@login_required
@role_required('Sub-verifier', 'Verifier')
def acknowledge_leave(leave_id, action):
    employee_no = session.get('employee_no')
    reject_reason = request.form.get('reject_reason')

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT requester, requester_name, leave_type, leave_reason, start_date, end_date, no_days,
                   recommender, recommender_status, recommender_2, recommender_2_status, approver
            FROM leave_request
            WHERE leave_id = %s
        """, (leave_id,))
        leave = cursor.fetchone()

        if not leave:
            flash('Leave request not found.', 'danger')
            return redirect(url_for('view_leave'))

        is_v1 = leave['recommender'] == employee_no
        is_v2 = leave['recommender_2'] == employee_no

        if not is_v1 and not is_v2:
            flash('You are not a verifier for this leave request.', 'danger')
            return redirect(url_for('view_leave'))

        if action == 'acknowledge':
            if is_v1:
                cursor.execute("""
                    UPDATE leave_request
                    SET recommender_status='Acknowledged', status_update=NOW()
                    WHERE leave_id=%s
                """, (leave_id,))
                conn.commit()
                flash('Leave request acknowledged successfully.', 'success')

                # If V2 exists → notify Verifier after Sub-verifier acknowledges
                if leave['recommender_2']:
                    notify_leave_submitted(
                        requester=leave['requester'],
                        requester_name=leave['requester_name'],
                        leave_type=leave['leave_type'],
                        reason=leave['leave_reason'],
                        start_date=leave['start_date'],
                        end_date=leave['end_date'],
                        no_days=leave['no_days'],
                        recipient_emp_no=leave['recommender_2'],
                        role_label="Verifier"
                    )
                # If NO V2 → V1 is final acknowledger, Approver will be notified via scheduled digest

            elif is_v2:
                cursor.execute("""
                    UPDATE leave_request
                    SET recommender_2_status='Acknowledged', status_update=NOW()
                    WHERE leave_id=%s
                """, (leave_id,))
                conn.commit()
                flash('Leave request acknowledged successfully.', 'success')
        # V2 is final acknowledger, Approver will be notified via scheduled digest

        elif action == 'reject':
            if not reject_reason or not reject_reason.strip():
                flash('Please provide a reason for rejection.', 'warning')
                return redirect(url_for('view_leave'))
            cursor.execute("""
                UPDATE leave_request
                SET recommender_status='Rejected',
                    recommender_2_status='Rejected',
                    approver_status='Rejected',
                    status='Rejected',
                    reject_reason=%s,
                    status_update=NOW()
                WHERE leave_id=%s
            """, (reject_reason.strip(), leave_id))
            conn.commit()
            flash('Leave request rejected.', 'info')

        elif action == 'reject':
            if not reject_reason or not reject_reason.strip():
                flash('Please provide a reason for rejection.', 'warning')
                return redirect(url_for('view_leave'))
            cursor.execute("""
                UPDATE leave_request
                SET recommender_status='Rejected',
                    recommender_2_status='Rejected',
                    approver_status='Rejected',
                    status='Rejected',
                    reject_reason=%s,
                    status_update=NOW()
                WHERE leave_id=%s
            """, (reject_reason.strip(), leave_id))
            conn.commit()
            flash('Leave request rejected.', 'info')

    except Exception as e:
        conn.rollback()
        flash(f'Error processing leave request: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('view_leave'))


@app.route('/process_leave/<int:leave_id>/<action>', methods=['POST'])
@login_required
@role_required('Approver')
def process_leave(leave_id, action):
    employee_no   = session.get('employee_no')
    reject_reason = request.form.get('reject_reason')
    primary_on_leave = is_primary_approver_on_leave()

    conn   = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT requester, approver, approver_status,
                   recommender, recommender_status,
                   recommender_2, recommender_2_status,
                   department, pre_approver_status
            FROM leave_request WHERE leave_id = %s
        """, (leave_id,))
        leave = cursor.fetchone()

        if not leave:
            flash('Leave request not found.', 'danger')
            return redirect(url_for('view_leave'))

        if leave['requester'] == employee_no:
            flash('You cannot approve your own leave request.', 'danger')
            return redirect(url_for('view_leave'))

        if leave['approver_status'] != 'Pending':
            flash('This leave request has already been processed.', 'warning')
            return redirect(url_for('view_leave'))

        v1_done = leave['recommender_status'] in ('Acknowledged', '') or leave['recommender'] is None
        v2_done = leave['recommender_2_status'] in ('Acknowledged', '') or leave['recommender_2'] is None

        if not (v1_done and v2_done):
            flash('Leave must be acknowledged by all verifiers before approval.', 'warning')
            return redirect(url_for('view_leave'))

        # Backup Approver: Pre-approval
        if is_backup_approver(employee_no) and employee_no != PRIMARY_APPROVER:
            if action == 'approve':
                # Cannot pre-approve own department
                approver_dept = session.get('department')
                if leave.get('department') == approver_dept:
                    flash('You cannot pre-approve your own department.', 'danger')
                    return redirect(url_for('view_leave'))
                
                if leave.get('pre_approver_status') == 'Pre-Approved':
                    flash('This leave has already been pre-approved.', 'warning')
                    return redirect(url_for('view_leave'))
                cursor.execute("""
                    UPDATE leave_request
                    SET pre_approver_status = 'Pre-Approved',
                        pre_approved_by = %s,
                        pre_approved_at = NOW(),
                        status_update = NOW()
                    WHERE leave_id = %s
                """, (employee_no, leave_id))
                flash('Leave request pre-approved successfully.', 'success')

            elif action == 'reject':
                approver_dept = session.get('department')
                if leave.get('department') == approver_dept:
                    flash('You cannot reject your own department.', 'danger')
                    return redirect(url_for('view_leave'))
                
                if not reject_reason or not reject_reason.strip():
                    flash('Please provide a reason for rejection.', 'warning')
                    return redirect(url_for('view_leave'))
                cursor.execute("""
                    UPDATE leave_request
                    SET status = 'Rejected',
                        approver_status = 'Rejected',
                        reject_reason = %s,
                        status_update = NOW()
                    WHERE leave_id = %s
                """, (reject_reason.strip(), leave_id))
                flash('Leave request rejected.', 'info')

        # ── Primary Approver: Full approval ──
        else:
            approver_dept = session.get('department')
            if leave.get('department') == approver_dept and employee_no != PRIMARY_APPROVER:
                flash('You are not authorized to approve leave from your own department.', 'danger')
                return redirect(url_for('view_leave'))

            if action == 'approve':
                cursor.execute("""
                    UPDATE leave_request
                    SET status          = 'Approved',
                        approver_status = 'Approved',
                        status_update   = NOW()
                    WHERE leave_id = %s
                """, (leave_id,))
                flash('Leave request approved successfully.', 'success')

            elif action == 'reject':
                if not reject_reason or not reject_reason.strip():
                    flash('Please provide a reason for rejection.', 'warning')
                    return redirect(url_for('view_leave'))
                cursor.execute("""
                    UPDATE leave_request
                    SET status          = 'Rejected',
                        approver_status = 'Rejected',
                        reject_reason   = %s,
                        status_update   = NOW()
                    WHERE leave_id = %s
                """, (reject_reason.strip(), leave_id))
                flash('Leave request rejected.', 'info')

            else:
                flash('Invalid action.', 'danger')
                return redirect(url_for('view_leave'))

        conn.commit()

    except Exception as e:
        conn.rollback()
        flash(f'Error processing leave request: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('view_leave'))


@app.route('/bulk_approve_leave', methods=['POST'])
@login_required
@role_required('Approver')
def bulk_approve_leave():
    data = request.get_json()
    leave_ids = data.get('leave_ids', [])
    if not leave_ids:
        return {"success": False, "error": "No leave selected"}

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    employee_no = session.get('employee_no')
    approver_dept = session.get('department')

    try:
        approved_count = 0
        skipped_count = 0

        for leave_id in leave_ids:
            cursor.execute("""
                SELECT recommender_status, approver_status, requester, department
                FROM leave_request WHERE leave_id = %s
            """, (leave_id,))
            leave = cursor.fetchone()

            if not leave:
                skipped_count += 1
                continue
            if leave['requester'] == employee_no:
                skipped_count += 1
                continue
            if leave.get('department') == approver_dept and employee_no != 'ER0001':
                skipped_count += 1
                continue
            if leave['approver_status'] != 'Pending':
                skipped_count += 1
                continue
            if leave['recommender_status'] not in ('Acknowledged', ''):
                skipped_count += 1
                continue

            cursor.execute("""
                UPDATE leave_request
                SET status = 'Approved', approver_status = 'Approved', status_update = NOW()
                WHERE leave_id = %s
            """, (leave_id,))
            approved_count += 1

        conn.commit()
        return {"success": True, "approved": approved_count, "skipped": skipped_count}

    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        cursor.close()
        conn.close()


@app.route('/employee_history')
@login_required
@role_required('Sub-verifier', 'Verifier', 'Approver')
def employee_history():
    roles = session.get('roles', [])
    employee_no = session.get('employee_no')
    department = session.get('department')
    is_verifier = 'Verifier' in roles
    is_approver = 'Approver' in roles

    subordinates = []
    subordinate_nos = []

    # Approver sees all employees
    if is_approver and employee_no == PRIMARY_APPROVER:
        all_emps = query_db("""
            SELECT DISTINCT e.Employee_no, e.Employee_name
            FROM employee e
            WHERE e.status = 'Active'
              AND e.Employee_no != %s
            ORDER BY e.Employee_name
        """, (employee_no,))
        subordinates = all_emps
        subordinate_nos = [s['Employee_no'] for s in subordinates]

    # Backup Approver sees only their own department
    elif is_approver and is_backup_approver(employee_no):
        subordinates = query_db("""
            SELECT DISTINCT e.Employee_no, e.Employee_name
            FROM employee e
            WHERE e.Department = %s
              AND e.status = 'Active'
              AND e.Employee_no != %s
            ORDER BY e.Employee_name
        """, (department, employee_no))
        subordinate_nos = [s['Employee_no'] for s in subordinates]

    # Verifier sees entire department
    elif is_verifier:
        subordinate_nos = get_subordinates_for(employee_no)
        
        # Fetch verifier_group regardless of dept
        vg_rows = query_db("""
            SELECT DISTINCT vg.employee_no
            FROM verifier_group vg
            WHERE vg.verifier_no = %s
            AND vg.verifier_role = 'Verifier'
        """, (employee_no,))
        
        vg_nos = [r['employee_no'] for r in vg_rows]
        
        all_nos = list(set(subordinate_nos + vg_nos))
        
        if all_nos:
            fmt = ','.join(['%s'] * len(all_nos))
            subordinates = query_db(f"""
                SELECT DISTINCT e.Employee_no, e.Employee_name
                FROM employee e
                WHERE e.Employee_no IN ({fmt})
                AND e.status = 'Active'
                ORDER BY e.Employee_name
            """, tuple(all_nos))
            subordinate_nos = [s['Employee_no'] for s in subordinates]
        else:
            subordinates = []
            subordinate_nos = []

    # Sub-verifier sees their assigned subordinates only
    else:
        subordinate_nos = get_subordinates_for(employee_no)
        if subordinate_nos:
            fmt = ','.join(['%s'] * len(subordinate_nos))
            subordinates = query_db(f"""
                SELECT DISTINCT e.Employee_no, e.Employee_name
                FROM employee e
                WHERE e.Employee_no IN ({fmt})
                ORDER BY e.Employee_name
            """, tuple(subordinate_nos))

    filter_employee = request.args.get('employee', '')
    filter_status = request.args.get('status', '')
    filter_leave_type = request.args.get('leave_type', '')
    filter_month = request.args.get('month', '')
    filter_date_from = request.args.get('date_from', '')
    filter_date_to = request.args.get('date_to', '')
    current_page = int(request.args.get('page', 1))
    per_page = 10

    if subordinate_nos:
        fmt = ','.join(['%s'] * len(subordinate_nos))
        query = f"""
            SELECT lr.*,
                   e.Employee_name AS requester_name, e.department,
                   v1.Employee_name AS recommender_name,
                   v2.Employee_name AS recommender_2_name,
                   ap.Employee_name AS approver_name
            FROM leave_request lr
            JOIN employee e ON lr.requester = e.Employee_no
            LEFT JOIN employee v1 ON lr.recommender = v1.Employee_no
            LEFT JOIN employee v2 ON lr.recommender_2 = v2.Employee_no
            LEFT JOIN employee ap ON lr.approver = ap.Employee_no
            WHERE lr.requester IN ({fmt})
        """
        params = list(subordinate_nos)
    else:
        query = """
            SELECT lr.*,
                e.Employee_name AS requester_name, e.department,
                v1.Employee_name AS recommender_name,
                v2.Employee_name AS recommender_2_name,
                ap.Employee_name AS approver_name
            FROM leave_request lr
            JOIN employee e  ON lr.requester      = e.Employee_no
            LEFT JOIN employee v1 ON lr.recommender   = v1.Employee_no
            LEFT JOIN employee v2 ON lr.recommender_2 = v2.Employee_no
            LEFT JOIN employee ap ON lr.approver      = ap.Employee_no
            WHERE 1=0
        """
        params = []

    if filter_employee:
        query += " AND lr.requester = %s"
        params.append(filter_employee)
    if filter_status:
        query += " AND lr.status = %s"
        params.append(filter_status)
    if filter_leave_type:
        query += " AND lr.leave_type = %s"
        params.append(filter_leave_type)
    if filter_month:
        query += " AND MONTH(lr.start_date) = %s"
        params.append(filter_month)
    if filter_date_from:
        query += " AND lr.start_date >= %s"
        params.append(filter_date_from)
    if filter_date_to:
        query += " AND lr.end_date <= %s"
        params.append(filter_date_to)

    query += " ORDER BY lr.created_at DESC"

    all_leaves = query_db(query, params)
    for leave in all_leaves:
        for key in ('start_date', 'end_date', 'created_at', 'status_update'):
            if leave.get(key) and hasattr(leave[key], 'strftime'):
                leave[key] = str(leave[key])
    total_leaves = len(all_leaves)
    total_pages = max(1, math.ceil(total_leaves / per_page))
    current_page = max(1, min(current_page, total_pages))
    paginated_leaves = all_leaves[(current_page - 1) * per_page: current_page * per_page]

    leave_types = list({l['leave_type'] for l in all_leaves if l.get('leave_type')})

    return render_template(
        'employee_history.html',
        all_leaves=all_leaves,
        paginated_leaves=paginated_leaves,
        total_leaves=total_leaves,
        total_pages=total_pages,
        current_page=current_page,
        per_page=per_page,
        subordinates=subordinates,
        leave_types=leave_types,
        roles=roles,
        filter_employee=filter_employee,
        filter_status=filter_status,
        filter_leave_type=filter_leave_type,
        filter_month=int(filter_month) if filter_month else '',
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to,
    )

@app.route('/leave_detail/<int:leave_id>')
@login_required
def leave_detail(leave_id):
    leave = query_db("""
        SELECT lr.*,
               e.Employee_name  AS requester_name,
               e.department,
               v1.Employee_name AS recommender_name,
               v2.Employee_name AS recommender_2_name,
               ap.Employee_name AS approver_name
        FROM leave_request lr
        JOIN employee e    ON lr.requester      = e.Employee_no
        LEFT JOIN employee v1 ON lr.recommender   = v1.Employee_no
        LEFT JOIN employee v2 ON lr.recommender_2 = v2.Employee_no
        LEFT JOIN employee ap ON lr.approver      = ap.Employee_no
        WHERE lr.leave_id = %s
    """, (leave_id,), one=True)

    if not leave:
        return jsonify({}), 404

    for key, val in leave.items():
        if hasattr(val, 'isoformat'):
            leave[key] = str(val)

    return jsonify(leave)

@app.route('/profile')
@login_required
def profile():
    employee_no = session['employee_no']
    employee_name = session.get('employee_name', session.get('username'))

    employee = query_db("""
        SELECT e.Employee_no, e.Employee_name, e.Department, e.Section_id,
               e.Position, e.Email, e.Phone, e.Register_date,
               au.roles, s.section_name
        FROM employee e
        LEFT JOIN auth_user au ON e.Employee_no = au.employee_no
        LEFT JOIN section s ON e.Section_id = s.section_id
        WHERE e.Employee_no = %s
    """, (employee_no,), one=True)

    if not employee:
        flash('Employee details not found.', 'danger')
        return redirect('/dashboard')

    today = date.today()
    current_year = today.year

    years_of_service = 0
    if employee['Register_date']:
        years_of_service = (today - employee['Register_date']).days / 365.25

    if years_of_service < 2:
        annual_leave_per_year = 10
    elif years_of_service < 5:
        annual_leave_per_year = 14
    else:
        annual_leave_per_year = 18

    leave_breakdown = query_db("""
        SELECT leave_type, COALESCE(SUM(no_days), 0) AS days_taken
        FROM leave_request
        WHERE requester = %s AND status = 'Approved' AND YEAR(start_date) = %s
        GROUP BY leave_type
    """, (employee_no, current_year))

    leave_breakdown_dict = {row['leave_type']: float(row['days_taken']) for row in leave_breakdown}
    annual_leave_used = leave_breakdown_dict.get('Annual Leave', 0)
    medical_leave_used = leave_breakdown_dict.get('Medical Leave', 0)
    emergency_leave_used = leave_breakdown_dict.get('Emergency Leave', 0)
    unpaid_leave_used = leave_breakdown_dict.get('Unpaid Leave', 0)
    other_leave_used = sum(v for k, v in leave_breakdown_dict.items()
                           if k not in ['Annual Leave', 'Medical Leave', 'Emergency Leave', 'Unpaid Leave'])
    total_used_days = sum(leave_breakdown_dict.values())

    total_entitled = annual_leave_per_year
    days_taken = annual_leave_used

    pending_days_row = query_db("""
        SELECT COALESCE(SUM(no_days), 0) AS pending_days
        FROM leave_request
        WHERE requester = %s AND status = 'Pending' AND YEAR(start_date) = %s
    """, (employee_no, current_year), one=True)
    pending_days = float(pending_days_row['pending_days']) if pending_days_row else 0

    status_counts_raw = query_db("""
        SELECT status, COUNT(*) AS count FROM leave_request
        WHERE requester = %s AND YEAR(start_date) = %s GROUP BY status
    """, (employee_no, current_year))
    status_counts = {row['status']: row['count'] for row in status_counts_raw}

    acknowledged_count = query_db("""
        SELECT COUNT(*) AS count FROM leave_request
        WHERE requester = %s AND status = 'Pending'
          AND recommender_status = 'Acknowledged' AND YEAR(start_date) = %s
    """, (employee_no, current_year), one=True)
    if acknowledged_count and acknowledged_count['count'] > 0:
        status_counts['Acknowledged'] = acknowledged_count['count']
        status_counts['Pending'] = status_counts.get('Pending', 0) - acknowledged_count['count']

    entitled_leaves = {'Annual Leave': total_entitled}
    leave_balance = {'Annual Leave': total_entitled - annual_leave_used}

    recent_leaves = query_db("""
        SELECT * FROM leave_request
        WHERE requester = %s AND YEAR(start_date) = %s
        ORDER BY start_date DESC LIMIT 10
    """, (employee_no, current_year))

    return render_template(
        'profile.html',
        employee=employee,
        employee_name=employee_name,
        total_entitled=total_entitled,
        days_taken=days_taken,
        total_balance=leave_balance['Annual Leave'],
        entitled_leaves=entitled_leaves,
        leave_balance=leave_balance,
        leave_breakdown=leave_breakdown,
        leave_breakdown_dict=leave_breakdown_dict,
        annual_leave_used=annual_leave_used,
        medical_leave_used=medical_leave_used,
        emergency_leave_used=emergency_leave_used,
        unpaid_leave_used=unpaid_leave_used,
        other_leave_used=other_leave_used,
        total_used_days=total_used_days,
        pending_days=pending_days,
        status_counts=status_counts,
        current_year=current_year,
        years_of_service=round(years_of_service, 1),
        annual_leave_per_year=annual_leave_per_year,
        recent_leaves=recent_leaves,
    )


# ─────────────────────────────────────────────
# Calendar / ICS
# ─────────────────────────────────────────────
@app.route('/get_leaves')
def get_leaves():
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        employee_no = session.get('employee_no')
        roles = session.get('roles', [])
        leaves = []

        if employee_no:
            if 'HR' in roles or 'Admin' in roles or ('Approver' in roles and 'Verifier' not in roles and 'Sub-verifier' not in roles):
                cursor.execute("""
                    SELECT requester_name, start_date, end_date, remark, start_remark, end_remark
                    FROM leave_request WHERE status = 'Approved'
                """)
                raw_leaves = cursor.fetchall()
            else:
                cursor.execute("SELECT Department FROM employee WHERE Employee_no = %s", (employee_no,))
                user_info = cursor.fetchone()
                if user_info:
                    cursor.execute("""
                        SELECT requester_name, start_date, end_date, remark, start_remark, end_remark
                        FROM leave_request WHERE status = 'Approved' AND department = %s
                    """, (user_info['Department'],))
                    raw_leaves = cursor.fetchall()
                else:
                    raw_leaves = []

            for leave in raw_leaves:
                current_date = leave["start_date"]
                end_date = leave["end_date"]
                while current_date <= end_date:
                    if current_date.weekday() < 5:
                        if current_date == leave["start_date"] and current_date == end_date:
                            day_remark = leave.get("remark") or ""
                        elif current_date == leave["start_date"]:
                            day_remark = leave.get("start_remark") or ""
                        elif current_date == end_date:
                            day_remark = leave.get("end_remark") or ""
                        else:
                            day_remark = ""
                        leaves.append({
                            "title": leave["requester_name"],
                            "start": current_date.strftime('%Y-%m-%d'),
                            "allDay": True,
                            "className": "employee-leave",
                            "remark": day_remark.lower() if day_remark else ""
                        })
                    current_date += timedelta(days=1)

        cursor.execute("""
            SELECT holiday_name, start_date, end_date FROM public_holidays WHERE status = 'Active'
        """)
        for holiday in cursor.fetchall():
            current_date = holiday["start_date"]
            while current_date <= holiday["end_date"]:
                leaves.append({
                    "title": holiday["holiday_name"],
                    "start": current_date.strftime('%Y-%m-%d'),
                    "allDay": True, "className": "public-holiday"
                })
                current_date += timedelta(days=1)

        cursor.execute("""
            SELECT ph.holiday_name, rl.leave_date FROM recommended_leave rl
            JOIN public_holidays ph ON rl.holiday_id = ph.holiday_id WHERE ph.status = 'Active'
        """)
        for rec in cursor.fetchall():
            leaves.append({
                "title": rec['holiday_name'],
                "start": rec["leave_date"].strftime('%Y-%m-%d'),
                "allDay": True, "className": "recommended-leave"
            })

        cursor.close()
        conn.close()
        return jsonify(leaves)

    except Exception as e:
        print(f"Error in get_leaves: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/get_public_holidays')
def get_public_holidays():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT start_date, end_date, holiday_name FROM public_holidays WHERE status = 'Active'")
    holidays = cursor.fetchall()
    cursor.execute("""
        SELECT rl.leave_date, ph.holiday_name FROM recommended_leave rl
        JOIN public_holidays ph ON rl.holiday_id = ph.holiday_id WHERE ph.status = 'Active'
    """)
    recommended = cursor.fetchall()
    cursor.close()
    conn.close()

    holiday_dates = []
    for holiday in holidays:
        current = holiday['start_date']
        while current <= holiday['end_date']:
            holiday_dates.append({'date': current.strftime('%Y-%m-%d'), 'name': holiday['holiday_name']})
            current += timedelta(days=1)
    for rec in recommended:
        holiday_dates.append({'date': rec['leave_date'].strftime('%Y-%m-%d'), 'name': rec['holiday_name']})
    return jsonify(holiday_dates)


def _escape_ics_text(s):
    if s is None:
        return ''
    s = str(s).replace('\\', '\\\\').replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;')
    return s


@app.route('/calendar.ics')
def calendar_ics():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT leave_id, requester_name, start_date, end_date, leave_type, leave_reason
        FROM leave_request WHERE status = 'Approved' AND end_date >= CURDATE() ORDER BY start_date
    """)
    leaves = cursor.fetchall()
    cursor.execute("""
        SELECT holiday_id, holiday_name, start_date, end_date, description
        FROM public_holidays WHERE status = 'Active' AND end_date >= CURDATE() ORDER BY start_date
    """)
    holidays = cursor.fetchall()
    cursor.close()
    conn.close()

    now_utc = datetime.utcnow()
    cal_lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//YourOrg//HR Leave Calendar//EN",
        "CALSCALE:GREGORIAN", "X-WR-CALNAME:HR Leave Calendar"
    ]
    for r in leaves:
        start = r['start_date'] if not isinstance(r['start_date'], str) else datetime.strptime(r['start_date'], "%Y-%m-%d").date()
        end = r['end_date'] if not isinstance(r['end_date'], str) else datetime.strptime(r['end_date'], "%Y-%m-%d").date()

        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:leave-{r['leave_id']}@{request.host.split(':')[0]}",
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(end + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape_ics_text(r.get('requester_name', 'Employee') + ' - ' + r.get('leave_type', 'Leave'))}",
            f"DESCRIPTION:{_escape_ics_text(r.get('leave_reason', ''))}",
            "TRANSP:OPAQUE",
            "END:VEVENT"
        ])
    for h in holidays:
        start = h['start_date'] if not isinstance(h['start_date'], str) else datetime.strptime(h['start_date'], "%Y-%m-%d").date()
        end = h['end_date'] if not isinstance(h['end_date'], str) else datetime.strptime(h['end_date'], "%Y-%m-%d").date()

        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:holiday-{h['holiday_id']}@{request.host.split(':')[0]}",
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(end + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape_ics_text('🎉 ' + h.get('holiday_name', 'Public Holiday'))}",
            f"DESCRIPTION:{_escape_ics_text(h.get('description', ''))}",
            "TRANSP:OPAQUE",
            "END:VEVENT"
        ])
    cal_lines.append("END:VCALENDAR")
    return Response("\r\n".join(cal_lines) + "\r\n", mimetype='text/calendar; charset=utf-8')


# ─────────────────────────────────────────────
# HR Routes
# ─────────────────────────────────────────────
def get_departments_sections():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT DISTINCT Department FROM employee WHERE Department IS NOT NULL ORDER BY Department")
    departments = [row['Department'] for row in cursor.fetchall()]
    cursor.execute("SELECT section_id, section_name FROM section ORDER BY section_name")
    sections = cursor.fetchall()
    cursor.close()
    conn.close()
    return departments, sections


@app.route('/hr/dashboard')
@login_required
@permission_required('user.manage')
def hr_dashboard():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) as total FROM employee WHERE status = 'Active' OR status IS NULL")
    total_employees = cursor.fetchone().get('total', 0)
    cursor.execute("SELECT COUNT(*) as total FROM auth_user")
    total_users = cursor.fetchone().get('total', 0)
    cursor.execute("SELECT COUNT(*) as total FROM leave_request WHERE status = 'Pending'")
    pending_leaves = cursor.fetchone().get('total', 0)
    cursor.execute("""
        SELECT COUNT(*) as total FROM leave_request
        WHERE status = 'Approved' AND MONTH(start_date) = MONTH(CURDATE()) AND YEAR(start_date) = YEAR(CURDATE())
    """)
    approved_this_month = cursor.fetchone().get('total', 0)
    cursor.execute("""
        SELECT leave_id, requester, requester_name, department, leave_type, leave_reason,
               start_date, end_date, no_days, status
        FROM leave_request
        WHERE status = 'Approved'
          AND MONTH(start_date) = MONTH(CURDATE()) AND YEAR(start_date) = YEAR(CURDATE())
        ORDER BY start_date DESC
    """)
    approved_leaves_monthly = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template(
        'hr_dashboard.html',
        emp_name=session.get('employee_name', 'HR'),
        total_employees=total_employees,
        total_users=total_users,
        pending_leaves=pending_leaves,
        approved_this_month=approved_this_month,
        approved_leaves_monthly=approved_leaves_monthly,
    )


@app.route('/hr/users')
@login_required
@hr_required
def hr_users():
    search = request.args.get('search', '')
    department = request.args.get('department', '')
    role = request.args.get('role', '')

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT e.Employee_no AS emp_no, e.Employee_name AS emp_name,
               e.Position, e.Department, e.Section_id, e.Email, e.Phone, e.Status,
               e.Register_date,
               au.username, au.roles, au.status AS user_status
        FROM employee e
        LEFT JOIN auth_user au ON e.Employee_no = au.employee_no
        WHERE (e.Status = 'Active' OR e.Status IS NULL)
    """
    params = []
    if search:
        query += " AND (e.Employee_no LIKE %s OR e.Employee_name LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    if department:
        query += " AND e.Department = %s"
        params.append(department)
    if role:
        query += " AND au.roles LIKE %s"
        params.append(f"%{role}%")
    query += " ORDER BY e.Employee_name"

    cursor.execute(query, params)
    users = cursor.fetchall()
    departments, _ = get_departments_sections()
    cursor.close()
    conn.close()

    return render_template(
        "hr_users.html",
        employee_name=session.get('employee_name', 'HR'),
        users=users,
        departments=departments,
        search=search,
        selected_department=department,
        selected_role=role
    )


@app.route('/hr/user/<string:emp_no>', methods=['GET', 'POST'])
@app.route('/hr/user/add', defaults={'emp_no': None}, methods=['GET', 'POST'])
@login_required
@hr_required
def hr_user_form(emp_no):
    departments, sections = get_departments_sections()
    employee = None
    users = []

    if emp_no:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT Employee_no AS emp_no, Employee_name AS emp_name, Position AS position,
                   Department, Section_id AS section_id, Email, Phone, Status, Register_date
            FROM employee WHERE Employee_no = %s
        """, (emp_no,))
        employee = cursor.fetchone()
        cursor.execute("""
            SELECT username, password, roles, status, department
            FROM auth_user WHERE employee_no = %s
        """, (emp_no,))
        users = cursor.fetchall()
        cursor.close()
        conn.close()
        if not employee:
            flash("Employee not found!", "danger")
            return redirect(url_for("hr_users"))

    if request.method == 'POST':
        emp_no_form = request.form['emp_no'].strip()
        emp_name = request.form['emp_name'].strip()
        department = request.form['department'].strip()
        section_id = request.form.get('section_control', '').strip()
        position = request.form.get('position', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        register_date = request.form.get('register_date', '').strip()
        status = request.form.get('status', 'Active')
        create_user = request.form.get("create_user") == "yes"
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        roles = request.form.get('roles', 'Employee').strip()

        if not emp_no_form or not emp_name or not department or not register_date:
            flash("Please fill in all required fields.", "danger")
            return redirect(request.url)

        conn = get_db()
        cursor = conn.cursor()
        try:
            if emp_no:
                cursor.execute("""
                    UPDATE employee
                    SET Employee_name=%s, Department=%s, Section_id=%s, Position=%s,
                        Email=%s, Phone=%s, Status=%s, Register_date=%s,
                        Last_edit_date=%s, Last_edit_user=%s
                    WHERE Employee_no=%s
                """, (emp_name, department, section_id or None, position, email, phone,
                      status, register_date, datetime.now(), session.get('employee_name'), emp_no_form))
            else:
                cursor.execute("""
                    INSERT INTO employee
                    (Employee_no, Employee_name, Department, Section_id, Position,
                     Email, Phone, Status, Register_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (emp_no_form, emp_name, department, section_id or None, position,
                      email, phone, status, register_date))

            cursor.execute("SELECT ID FROM auth_user WHERE employee_no = %s", (emp_no_form,))
            existing_user = cursor.fetchone()

            if create_user:
                cursor.execute("""
                    SELECT ID FROM auth_user WHERE username = %s AND employee_no != %s
                """, (username, emp_no_form))
                if cursor.fetchone():
                    conn.rollback()
                    flash(f"Username '{username}' is already taken.", "danger")
                    return redirect(request.url)

                if existing_user:
                    if password:
                        cursor.execute("""
                            UPDATE auth_user SET username=%s, password=%s, roles=%s, department=%s,
                                last_edit_date=%s, last_edit_user=%s
                            WHERE employee_no=%s
                        """, (username, password, roles, department,
                              datetime.now(), session.get('employee_name'), emp_no_form))
                    else:
                        cursor.execute("""
                            UPDATE auth_user SET username=%s, roles=%s, department=%s,
                                last_edit_date=%s, last_edit_user=%s
                            WHERE employee_no=%s
                        """, (username, roles, department,
                              datetime.now(), session.get('employee_name'), emp_no_form))
                else:
                    cursor.execute("""
                        INSERT INTO auth_user (username, password, employee_no, department, roles, status)
                        VALUES (%s, %s, %s, %s, %s, 'Active')
                    """, (username, password, emp_no_form, department, roles))

            conn.commit()
            flash("Employee saved successfully", "success")
            return redirect(url_for("hr_users"))

        except Exception as e:
            conn.rollback()
            flash(f"Error saving employee: {str(e)}", "danger")
            return redirect(request.url)
        finally:
            cursor.close()
            conn.close()

    form_data = employee or {}
    form_data['user_accounts'] = users if users else []
    form_data['has_user_account'] = len(form_data['user_accounts']) > 0
    return render_template(
        "hr_user_form.html",
        employee_name=session.get("employee_name", "HR"),
        user=form_data if emp_no else None,
        departments=departments,
        sections=sections
    )


# ── NEW: HR Verifier Group Management ──
@app.route('/hr/verifier_groups')
@login_required
@hr_required
def hr_verifier_groups():
    section_filter = request.args.get('section', '')

    query = """
        SELECT vg.id, vg.section_id, vg.verifier_no, vg.verifier_role, vg.employee_no,
               ev.Employee_name AS verifier_name,
               ee.Employee_name AS employee_name
        FROM verifier_group vg
        JOIN employee ev ON vg.verifier_no = ev.Employee_no
        JOIN employee ee ON vg.employee_no = ee.Employee_no
    """
    params = []
    if section_filter:
        query += " WHERE vg.section_id = %s"
        params.append(section_filter)
    query += " ORDER BY vg.section_id, vg.verifier_role, ee.Employee_name"

    groups = query_db(query, params)

    sections = query_db("SELECT section_id, section_name FROM section ORDER BY section_name")

    return render_template(
        'hr_verifier_groups.html',
        employee_name=session.get('employee_name', 'HR'),
        groups=groups,
        sections=sections,
        section_filter=section_filter
    )


@app.route('/hr/verifier_group/add', methods=['POST'])
@login_required
@hr_required
def hr_verifier_group_add():
    section_id = request.form.get('section_id', '').strip()
    verifier_no = request.form.get('verifier_no', '').strip()
    verifier_role = request.form.get('verifier_role', '').strip()
    employee_no = request.form.get('employee_no', '').strip()

    if not all([section_id, verifier_no, verifier_role, employee_no]):
        flash('All fields are required.', 'danger')
        return redirect(url_for('hr_verifier_groups'))

    if verifier_role not in ('Sub-verifier', 'Verifier'):
        flash('Invalid verifier role.', 'danger')
        return redirect(url_for('hr_verifier_groups'))

    # Check duplicate
    existing = query_db(
        "SELECT id FROM verifier_group WHERE employee_no = %s AND verifier_no = %s",
        (employee_no, verifier_no), one=True
    )
    if existing:
        flash('This assignment already exists.', 'warning')
        return redirect(url_for('hr_verifier_groups'))

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO verifier_group (section_id, verifier_no, verifier_role, employee_no)
            VALUES (%s, %s, %s, %s)
        """, (section_id, verifier_no, verifier_role, employee_no))
        conn.commit()
        flash('Verifier assignment added successfully.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('hr_verifier_groups'))


@app.route('/hr/verifier_group/delete/<int:group_id>', methods=['POST'])
@login_required
@hr_required
def hr_verifier_group_delete(group_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM verifier_group WHERE id = %s", (group_id,))
        conn.commit()
        flash('Assignment removed.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('hr_verifier_groups'))


@app.route('/hr/leaves')
@login_required
@hr_required
def hr_leaves():
    per_page = 8
    current_page = request.args.get('page', 1, type=int)
    status_filter = request.args.getlist('status')
    department_filter = request.args.get('department', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT lr.leave_id, lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.leave_reason, lr.start_date, lr.end_date,
               lr.no_days, lr.status, lr.recommender, lr.recommender_status, 
               lr.recommender_2, lr.recommender_2_status, lr.approver_status,
               lr.reject_reason, lr.created_at,
               lr.original_doc_received, lr.original_doc_received_by,
               lr.original_doc_received_at, lr.is_exported, lr.exported_at, lr.exported_by
        FROM leave_request lr WHERE 1=1
    """
    params = []
    if status_filter:
        placeholders = ','.join(['%s'] * len(status_filter))
        query += f" AND lr.status IN ({placeholders})"
        params.extend(status_filter)
    if department_filter:
        query += " AND lr.department = %s"; params.append(department_filter)
    if search:
        query += " AND (lr.requester LIKE %s OR lr.requester_name LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
    if date_from:
        query += " AND lr.start_date >= %s"; params.append(date_from)
    if date_to:
        query += " AND lr.end_date <= %s"; params.append(date_to)
    query += " ORDER BY lr.created_at DESC"

    cursor.execute(query, params)
    all_leaves = cursor.fetchall()

    # Collect all unique recommender employee nos
    recommender_nos = set()
    for l in all_leaves:
        if l['recommender']:
            recommender_nos.add(l['recommender'])
        if l['recommender_2']:
            recommender_nos.add(l['recommender_2'])

    # Look up their actual verifier_role from verifier_group
    verifier_role_map = {}
    if recommender_nos:
        fmt = ','.join(['%s'] * len(recommender_nos))
        cursor.execute(
            f"SELECT verifier_no, verifier_role FROM verifier_group WHERE verifier_no IN ({fmt})",
            list(recommender_nos)
        )
        for row in cursor.fetchall():
            verifier_role_map[row['verifier_no']] = row['verifier_role']

    total_leaves = len(all_leaves)
    total_pages = max(1, (total_leaves + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    start_idx = (current_page - 1) * per_page
    paginated_leaves = all_leaves[start_idx:start_idx + per_page]

    cursor.execute("SELECT DISTINCT department FROM leave_request WHERE department IS NOT NULL ORDER BY department")
    departments = [row['department'] for row in cursor.fetchall()]
    cursor.close()
    conn.close()

    # Doc lookup
    leaves_with_docs = set()
    if all_leaves:
        fmt = ','.join(['%s'] * len(all_leaves))
        doc_rows = query_db(
            f"SELECT DISTINCT leave_id FROM leave_documents WHERE leave_id IN ({fmt})",
            [l['leave_id'] for l in all_leaves]
        )
        leaves_with_docs = {r['leave_id'] for r in doc_rows}

    return render_template(
        'hr_leaves.html',
        employee_name=session.get('employee_name', 'HR'),
        leaves=all_leaves,
        paginated_leaves=paginated_leaves,
        departments=departments,
        status_filter=status_filter,
        department_filter=department_filter,
        search=search,
        date_from=date_from,
        date_to=date_to,
        current_page=current_page,
        total_pages=total_pages,
        total_leaves=total_leaves,
        per_page=per_page,
        verifier_role_map=verifier_role_map,
        leaves_with_docs=leaves_with_docs,
    )


@app.route('/hr/leave/edit/<int:leave_id>', methods=['GET', 'POST'])
@login_required
@hr_required
def hr_leave_edit(leave_id):
    # ── GET: fetch and return leave data ──────────────────────────────────────
    if request.method == 'GET':
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT leave_id, requester, requester_name, department,
                       leave_type, leave_reason, start_date, end_date,
                       remark, start_remark, end_remark,
                       no_days, status, recommender_status, reject_reason
                FROM leave_request
                WHERE leave_id = %s
            """, (leave_id,))
            leave = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if not leave:
            return jsonify({'success': False, 'message': 'Leave not found'}), 404

        # Serialise dates for JSON
        leave['start_date'] = leave['start_date'].strftime('%Y-%m-%d') if leave['start_date'] else ''
        leave['end_date']   = leave['end_date'].strftime('%Y-%m-%d')   if leave['end_date']   else ''

        return jsonify({'success': True, 'leave': leave})

    # ── POST: validate and save ───────────────────────────────────────────────
    data          = request.get_json(silent=True) or {}
    leave_type    = (data.get('leave_type')    or '').strip()
    leave_reason  = (data.get('leave_reason')  or '').strip()
    start_date    = (data.get('start_date')    or '').strip()
    end_date      = (data.get('end_date')      or '').strip()
    remark        = (data.get('remark')        or '').strip()
    start_remark  = (data.get('start_remark')  or '').strip()
    end_remark    = (data.get('end_remark')    or '').strip()
    no_days       = data.get('no_days')
    status        = (data.get('status')        or '').strip()
    reject_reason = (data.get('reject_reason') or '').strip()

    # Required field check
    if not all([leave_type, start_date, end_date, status]) or no_days is None:
        return jsonify({'success': False, 'message': 'Required fields missing'}), 400

    allowed_statuses = ('Pending', 'Approved', 'Rejected', 'Cancel')
    if status not in allowed_statuses:
        return jsonify({'success': False, 'message': 'Invalid status value'}), 400

    if status == 'Rejected' and not reject_reason:
        return jsonify({'success': False, 'message': 'Reject reason is required when status is Rejected'}), 400

    try:
        no_days = float(no_days)
        if no_days <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'No. of days must be a positive number'}), 400

    # Derive recommender / approver status from the overall status
    STATUS_MAP = {
        'Approved': ('Acknowledged', 'Approved'),
        'Rejected': ('Rejected',     'Rejected'),
        'Cancel':   ('Pending',      'Pending'),
        'Pending':  ('Pending',      'Pending'),
    }
    recommender_status, approver_status = STATUS_MAP[status]

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        # If cancelling, delete uploaded files
        if status == 'Cancel':
            cursor.execute(
                "SELECT filename FROM leave_documents WHERE leave_id = %s",
                (leave_id,)
            )
            docs = cursor.fetchall()
            for doc in docs:
                fp = os.path.join(app.config['UPLOAD_FOLDER'], doc['filename'])
                if os.path.exists(fp):
                    os.remove(fp)

        # Update the leave record 
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE leave_request
            SET leave_type = %s,
                leave_reason = %s,
                start_date = %s,
                remark = %s,
                start_remark = %s,
                end_date = %s,
                end_remark = %s,
                no_days = %s,
                status = %s,
                recommender_status = %s,
                approver_status = %s,
                reject_reason = %s,
                status_update = NOW()
            WHERE leave_id = %s
        """, (
            leave_type,
            leave_reason or None,
            start_date,
            remark or None,
            start_remark or None,
            end_date,
            end_remark or None,
            no_days,
            status,
            recommender_status,
            approver_status,
            reject_reason if status == 'Rejected' else None,
            leave_id
        ))

        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Leave not found or no changes made'}), 404

        conn.commit()
        return jsonify({'success': True, 'message': 'Leave updated successfully'})

    except Exception as e:
        conn.rollback()
        app.logger.error(f"hr_leave_edit error for leave_id={leave_id}: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

    finally:
        cursor.close()
        conn.close()


@app.route('/hr/leaves/export')
@login_required
@hr_required
def hr_leaves_export():
    status_filter = request.args.getlist('status')
    department_filter = request.args.get('department', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    exported_filter = request.args.get('exported', 'all')

    query = """
        SELECT lr.leave_id, lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.leave_reason, lr.start_date, lr.end_date,
               lr.no_days, lr.status, lr.recommender_status, lr.approver_status,
               lr.reject_reason, lr.created_at,
               lr.original_doc_received, lr.original_doc_received_by,
               lr.original_doc_received_at, lr.is_exported, lr.exported_at, lr.exported_by
        FROM leave_request lr WHERE 1=1
    """
    params = []
    if status_filter:
        query += f" AND lr.status IN ({','.join(['%s']*len(status_filter))})"
        params.extend(status_filter)
    if department_filter:
        query += " AND lr.department = %s"; params.append(department_filter)
    if search:
        query += " AND (lr.requester LIKE %s OR lr.requester_name LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
    if date_from:
        query += " AND lr.start_date >= %s"; params.append(date_from)
    if date_to:
        query += " AND lr.end_date <= %s"; params.append(date_to)
    if exported_filter == '0':
        query += " AND lr.is_exported = 0"
    query += " ORDER BY lr.requester ASC, lr.start_date ASC"  # ← order by emp no

    leaves = query_db(query, params)
    if not leaves:
        flash('No records found to export.', 'warning')
        return redirect(request.referrer or '/hr/leaves')

    ids_with_docs = set()
    if leaves:
        doc_rows = query_db(
            f"SELECT DISTINCT leave_id FROM leave_documents WHERE leave_id IN ({','.join(['%s']*len(leaves))})",
            [l['leave_id'] for l in leaves]
        )
        ids_with_docs = {r['leave_id'] for r in doc_rows}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Leave ID', 'Emp No', 'Employee Name', 'Department',
        'Leave Type', 'Leave Reason', 'Start Date', 'End Date',
        'No. Days', 'Status', 'Verifier Status', 'Approver Status',
        'Reject Reason', 'Applied Date', 'Has Document',
        'Original Doc Received', 'Doc Received By', 'Doc Received At'
    ])
    for leave in leaves:
        writer.writerow([
            leave['leave_id'], leave['requester'], leave['requester_name'],
            leave['department'] or '', leave['leave_type'] or '', leave['leave_reason'] or '',
            leave['start_date'].strftime('%Y-%m-%d') if leave['start_date'] else '',
            leave['end_date'].strftime('%Y-%m-%d') if leave['end_date'] else '',
            leave['no_days'], leave['status'],
            leave['recommender_status'] or '', leave['approver_status'] or '',
            leave['reject_reason'] or '',
            leave['created_at'].strftime('%Y-%m-%d %H:%M') if leave['created_at'] else '',
            'Yes' if leave['leave_id'] in ids_with_docs else 'No',
            'Yes' if leave['original_doc_received'] else 'No',
            leave['original_doc_received_by'] or '',
            leave['original_doc_received_at'].strftime('%Y-%m-%d %H:%M') if leave['original_doc_received_at'] else ''
        ])

    # Mark exported leaves
    unexported_ids = [l['leave_id'] for l in leaves if not l['is_exported']]
    if unexported_ids:
        now = dt.now()
        exported_by = session.get('employee_no')
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE leave_request
                       SET is_exported = 1,
                           exported_at = %s,
                           exported_by = %s
                     WHERE leave_id IN ({','.join(['%s'] * len(unexported_ids))})""",
                [now, exported_by] + unexported_ids
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
        finally:
            cursor.close()
            conn.close()

    output.seek(0)
    label = 'unexported' if exported_filter == '0' else 'all'
    filename = f"leaves_{label}_{dt.now().strftime('%Y%m%d')}.csv"
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})

@app.route('/hr/leaves/exported_ids')
@login_required
@hr_required
def hr_leaves_exported_ids():
    status_filter = request.args.getlist('status')
    department_filter = request.args.get('department', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = "SELECT leave_id, exported_by, exported_at FROM leave_request WHERE is_exported = 1"
    params = []
    if status_filter:
        query += f" AND status IN ({','.join(['%s']*len(status_filter))})"
        params.extend(status_filter)
    if department_filter:
        query += " AND department = %s"; params.append(department_filter)
    if search:
        query += " AND (requester LIKE %s OR requester_name LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
    if date_from:
        query += " AND start_date >= %s"; params.append(date_from)
    if date_to:
        query += " AND end_date <= %s"; params.append(date_to)

    rows = query_db(query, params)
    if not rows:
        return jsonify({'ids': [], 'exported_by': '', 'exported_at': ''})

    latest = max(rows, key=lambda r: r['exported_at'] or dt.min)
    return jsonify({
        'ids': [r['leave_id'] for r in rows],
        'exported_by': latest['exported_by'] or '',
        'exported_at': latest['exported_at'].strftime('%Y-%m-%d %H:%M') if latest['exported_at'] else ''
    })


@app.route('/update_original_doc_status/<int:leave_id>', methods=['POST'])
@login_required
def update_original_doc_status(leave_id):
    roles = session.get('roles', [])
    employee_no = session.get('employee_no')
    if 'HR' not in roles and 'Approver' not in roles and 'Admin' not in roles:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.get_json()
    received = data.get('received', False)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM leave_documents WHERE leave_id = %s", (leave_id,)
        )
        doc_count = cursor.fetchone()[0]
        if doc_count == 0:
            return jsonify({'success': False, 'message': 'No document uploaded for this leave'}), 400

        if received:
            cursor.execute("""
                UPDATE leave_request
                SET original_doc_received=1, original_doc_received_by=%s, original_doc_received_at=NOW()
                WHERE leave_id=%s
            """, (employee_no, leave_id))
        else:
            cursor.execute("""
                UPDATE leave_request
                SET original_doc_received=0, original_doc_received_by=NULL, original_doc_received_at=NULL
                WHERE leave_id=%s
            """, (leave_id,))

        conn.commit()
        return jsonify({'success': True, 'message': 'Status updated successfully'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/hr/holidays')
@login_required
@hr_required
def hr_holidays():
    search = request.args.get('search', '')
    year = request.args.get('year', datetime.now().year)
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT ph.*, COUNT(rl.id) as recommended_count
        FROM public_holidays ph
        LEFT JOIN recommended_leave rl ON ph.holiday_id = rl.holiday_id
        WHERE ph.status = 'Active' AND YEAR(ph.start_date) = %s
    """
    params = [year]
    if search:
        query += " AND ph.holiday_name LIKE %s"; params.append(f'%{search}%')
    query += " GROUP BY ph.holiday_id ORDER BY ph.start_date ASC"
    cursor.execute(query, params)
    holidays = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template(
        'hr_holidays.html',
        employee_name=session.get('employee_name', 'HR'),
        holidays=holidays, search=search,
        selected_year=year, current_year=datetime.now().year
    )


@app.route('/hr/holiday/<int:holiday_id>', methods=['GET', 'POST'])
@app.route('/hr/holiday/add', defaults={'holiday_id': None}, methods=['GET', 'POST'])
@login_required
@hr_required
def hr_holiday_form(holiday_id):
    holiday = None
    recommended_dates = []
    if holiday_id:
        holiday = query_db("SELECT * FROM public_holidays WHERE holiday_id = %s", (holiday_id,), one=True)
        if not holiday:
            flash("Holiday not found!", "danger")
            return redirect(url_for("hr_holidays"))
        recommended_dates = query_db("""
            SELECT * FROM recommended_leave WHERE holiday_id = %s ORDER BY leave_date ASC
        """, (holiday_id,))

    if request.method == 'POST':
        holiday_name = request.form['holiday_name'].strip()
        start_date = request.form['start_date']
        end_date = request.form['end_date']
        description = request.form.get('description', '').strip()
        is_mandatory = request.form.get('is_mandatory') == 'on'
        auto_deduct = request.form.get('auto_deduct') == 'on'
        recommended_dates_input = request.form.getlist('recommended_dates[]')
        recommended_priorities_input = request.form.getlist('recommended_priorities[]')

        if not holiday_name or not start_date or not end_date:
            flash("Please fill in all required fields", "danger")
            return redirect(request.url)

        conn = get_db()
        cursor = conn.cursor()
        try:
            if holiday_id:
                cursor.execute("""
                    UPDATE public_holidays
                    SET holiday_name=%s, start_date=%s, end_date=%s,
                        description=%s, is_mandatory=%s, auto_deduct=%s, updated_at=NOW()
                    WHERE holiday_id=%s
                """, (holiday_name, start_date, end_date, description, is_mandatory, auto_deduct, holiday_id))
                cursor.execute("DELETE FROM recommended_leave WHERE holiday_id = %s", (holiday_id,))
                new_holiday_id = holiday_id
                flash("Holiday updated successfully", "success")
            else:
                cursor.execute("""
                    INSERT INTO public_holidays
                    (holiday_name, start_date, end_date, description, is_mandatory, auto_deduct, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (holiday_name, start_date, end_date, description, is_mandatory, auto_deduct, session.get('employee_no')))
                new_holiday_id = cursor.lastrowid
                flash("Holiday added successfully", "success")

            for i, rec_date in enumerate(recommended_dates_input):
                if rec_date:
                    rec_priority = recommended_priorities_input[i] if i < len(recommended_priorities_input) else 'AL_FIRST'
                    cursor.execute("""
                        INSERT INTO recommended_leave (holiday_id, leave_date, leave_priority)
                        VALUES (%s, %s, %s)
                    """, (new_holiday_id, rec_date, rec_priority))

            conn.commit()

            if auto_deduct and recommended_dates_input:
                result = auto_create_leave_requests(new_holiday_id)
                if result['success']:
                    flash(f"Holiday saved! Created {result['created_requests']} leave requests. "
                          f"Skipped {result['skipped']} (already have leave).", "success")
                    if result['errors']:
                        flash(f"Errors: {', '.join(result['errors'][:5])}", "warning")
                else:
                    flash(f"Holiday saved, but auto-creation failed: {result['message']}", "warning")

            return redirect(url_for("hr_holidays"))
        except Exception as e:
            conn.rollback()
            flash(f"Error saving holiday: {str(e)}", "danger")
            return redirect(request.url)
        finally:
            cursor.close()
            conn.close()

    return render_template(
        "hr_holiday_form.html",
        employee_name=session.get("employee_name", "HR"),
        holiday=holiday,
        recommended_dates=recommended_dates
    )


@app.route('/hr/holiday/delete/<int:holiday_id>', methods=['POST'])
@login_required
@hr_required
def hr_holiday_delete(holiday_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE public_holidays SET status='Cancel', updated_at=NOW() WHERE holiday_id=%s", (holiday_id,))
        conn.commit()
        flash('Holiday deleted successfully!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error deleting holiday: {str(e)}', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('hr_holidays'))


@app.route('/logout')
def logout():
    if 'employee_no' in session:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("UPDATE auth_user SET last_logout=NOW() WHERE employee_no=%s", (session['employee_no'],))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error updating logout time: {e}")
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect('/calendar')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=916, debug=True)