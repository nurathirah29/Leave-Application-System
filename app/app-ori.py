import os
import math
import time
import smtplib
import logging
import traceback
import functools
import atexit
import mysql.connector
from decimal import Decimal
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
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['PERMANENT_SESSION_LIFETIME'] = 1800 # 30 minutes

# Database connection
def get_db():
    return mysql.connector.connect(
        host='7localhost',
        user='root',
        password='ERMsql999$',
        database='hr_db'
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

def allowed_file_type(file: FileStorage):
    allowed_mime_types = {'application/pdf', 'image/png', 'image/jpeg'}
    return file.mimetype in allowed_mime_types

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

# Role-based access control decorator
def role_required(*required_roles):
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            user_roles = session.get('roles', [])
            if not any (role in user_roles for role in required_roles):
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

ROLE_PERMISSIONS = {
    'Employee': ['leave.apply', 'leave.view'],
    'Verifier': ['leave.approve', 'leave.view'],
    'Approver': ['leave.approve', 'leave.view'],
    'HR': ['user.manage', 'leave.view', 'reports.view']
}

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

    # Verifier
    if 'Verifier' in roles:
        verifier = query_db("""
            SELECT 1
            FROM leave_request lr
            JOIN employee e ON lr.requester = e.Employee_no
            JOIN section s ON e.Section_id = s.section_id
            WHERE (s.verifier_id = %s OR s.verifier_id_2 = %s)
              AND lr.recommender_status = 'Pending'
              AND lr.requester != %s
            LIMIT 1
        """, (employee_no, employee_no, employee_no), one=True)

        if verifier:
            has_pending = True

    # Approver
    if not has_pending and 'Approver' in roles:
        approver = query_db("""
            SELECT 1
            FROM leave_request
            WHERE recommender_status = 'Acknowledged'
              AND approver_status = 'Pending'
            LIMIT 1
        """, one=True)

        if approver:
            has_pending = True

    return dict(has_pending_manage_leave=has_pending)

def calculate_leave_days(start_date, end_date, remark, start_remark, end_remark):
    s = datetime.strptime(start_date, "%Y-%m-%d").date()
    e = datetime.strptime(end_date, "%Y-%m-%d").date()

    def is_half_day(val):
        if not val:
            return False
        val_lower = str(val).lower()
        return 'half day' in val_lower or '0.5' in val_lower

    # Count working days
    count = 0
    current = s
    while current <= e:
        if current.weekday() < 5:  # 0=Monday, 4=Friday
            count += 1
        current += timedelta(days=1)

    # Single day leave
    if s == e:
        return 0.5 if is_half_day(remark) else 1.0

    # Multi-day leave
    start_val = 0.5 if is_half_day(start_remark) else 1.0
    end_val = 0.5 if is_half_day(end_remark) else 1.0
    middle_days = max(count - 2, 0)

    return float(middle_days + start_val + end_val)

def auto_create_leave_requests(holiday_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT * FROM public_holidays
            WHERE holiday_id = %s AND auto_deduct = TRUE
        """, (holiday_id,))
        holiday = cursor.fetchone()

        if not holiday:
            return {'success': False, 'message': 'Holiday not found or auto-deduct disabled.'}
        
        # Get recommended leave dates for this holiday
        cursor.execute("""
            SELECT * FROM recommended_leave
            WHERE holiday_id = %s
            ORDER BY leave_date ASC
        """, (holiday_id,))
        recommended_dates = cursor.fetchall()
        
        if not recommended_dates:
            return {'success': False, 'message': 'No recommended dates found'}
        
        # Get all active employees
        cursor.execute("""
            SELECT Employee_no, Employee_name, Register_date
            FROM employee
            WHERE status = 'Active'
        """)
        employees = cursor.fetchall()
        
        results = {
            'success': True,
            'total_employees': len(employees),
            'total_dates': len(recommended_dates),
            'created_requests': 0,
            'skipped': 0,
            'errors': []
        }
        
        current_year = date.today().year
        
        # Process each employee
        for emp in employees:
            employee_no = emp['Employee_no']
            employee_name = emp['Employee_name']
            register_date = emp['Register_date']
            
            # Calculate employee's current AL balance
            al_balance = calculate_employee_al_balance(
                employee_no, 
                register_date, 
                current_year,
                cursor
            )
            
            # Process each recommended date for this employee
            for rec_date in recommended_dates:
                leave_date = rec_date['leave_date']
                leave_priority = rec_date['leave_priority']
                
                # Check if leave request already exists for this date
                cursor.execute("""
                    SELECT request_id FROM leave_request
                    WHERE requester = %s
                      AND start_date <= %s
                      AND end_date >= %s
                """, (employee_no, leave_date, leave_date))
                
                existing = cursor.fetchone()
                if existing:
                    results['skipped'] += 1
                    continue  # Skip if already has leave on this date
                
                # Determine leave type based on priority and balance
                leave_type, days_to_deduct = determine_leave_type(
                    leave_priority,
                    al_balance,
                    1.0  # Each recommended date is 1 day
                )
                
                # Create the leave request
                try:
                    cursor.execute("""
                        INSERT INTO leave_request
                        (requester, requester_name, leave_type, start_date, end_date,
                         no_days, leave_reason, status, applied_date, 
                         auto_created, related_holiday_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), TRUE, %s)
                    """, (
                        employee_no,
                        employee_name,
                        leave_type,
                        leave_date,
                        leave_date,
                        1.0,  # 1 day
                        f"Auto-created for {holiday['holiday_name']} - Company advised leave",
                        'Approved',  # Auto-approve or keep as 'Pending'?
                        holiday_id
                    ))
                    
                    results['created_requests'] += 1
                    
                    # Update AL balance for next iteration
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
    # Calculate years of service
    today = date.today()
    years_of_service = (today - register_date).days / 365.25
    
    # Determine annual entitlement
    if years_of_service < 2:
        annual_leave_per_year = 10
    elif years_of_service < 5:
        annual_leave_per_year = 14
    else:
        annual_leave_per_year = 18
    
    # Calculate monthly accrual
    monthly_accrual_rate = annual_leave_per_year / 12
    
    # Calculate months worked in current year
    if register_date.year == current_year:
        months_worked = (today.month - register_date.month) + 1
    else:
        months_worked = today.month
    
    # Accrued AL this year
    accrued_al = round(monthly_accrual_rate * months_worked, 1)
    
    # Get carry-forward balance from previous year
    cursor.execute("""
        SELECT carried_days FROM leave_carryforward
        WHERE employee_no = %s
          AND year = %s
          AND status = 'Active'
    """, (employee_no, current_year - 1))
    
    carryforward = cursor.fetchone()
    carryforward_balance = float(carryforward['carried_days']) if carryforward else 0
    
    # Total entitlement
    total_entitlement = accrued_al + carryforward_balance
    
    # Get used AL this year
    cursor.execute("""
        SELECT COALESCE(SUM(no_days), 0) as used_days
        FROM leave_request
        WHERE requester = %s
          AND leave_type = 'Annual Leave'
          AND status = 'Approved'
          AND YEAR(start_date) = %s
    """, (employee_no, current_year))
    
    used = cursor.fetchone()
    used_days = float(used['used_days']) if used else 0
    
    # Calculate balance
    balance = total_entitlement - used_days
    
    return max(0, balance)  # Ensure non-negative

def determine_leave_type(leave_priority, al_balance, days_needed):
    if leave_priority == 'AL_ONLY':
        # Must use AL regardless of balance
        return ('Annual Leave', days_needed)
    
    elif leave_priority == 'UPL_ONLY':
        # Always use Unpaid Leave
        return ('Unpaid Leave', days_needed)
    
    elif leave_priority == 'AL_FIRST':
        # Try AL first, then UPL
        if al_balance >= days_needed:
            return ('Annual Leave', days_needed)
        else:
            # Not enough AL, use Unpaid Leave
            return ('Unpaid Leave', days_needed)
    
    # Default fallback
    return ('Unpaid Leave', days_needed)

# HR-only decorator
def hr_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if 'HR' not in session.get('roles', []):
            flash('HR access required!', 'danger')
            return redirect('/dashboard')
        return f(*args, **kwargs)
    return wrapped

EMAIL_ADDRESS = 'esystem@excel-rim.com'
EMAIL_PASSWORD = 'Ersystem@2024$$'
SMTP_SERVER = 'mail.excel-rim.com'
SMTP_PORT = 587

SYSTEM_LINK = "http://192.168.110.99:916/calendar"

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
        
        logging.info(f"Email sent successfully to {', '.join(recipients)}")
    except Exception as e:
        logging.error(f"Failed to send email: {str(e)}")
        traceback.print_exc()
        return False

def build_leave_email(requester, requester_name, leave_type, leave_reason):
    return f"""
    <html>
        <body style="font-family: Times New Roman;">
            <p>Dear Verifier,</p>
            <p>A leave request has been submitted and requires your attention.</p>
            <table style="border-collapse: collapse; width: 50%; font-size: 13px;">
                <tr>
                    <td style="padding: 6px; font-weight: bold;">Requester:</td>
                    <td style="padding: 6px;">{requester} - {requester_name}</td>
                </tr>
                <tr>
                    <td style="padding: 6px; font-weight: bold;">Leave Type:</td>
                    <td style="padding: 6px;">{leave_type}</td>
                </tr>
                <tr>
                    <td style="padding: 6px; font-weight: bold;">Leave Reason:</td>
                    <td style="padding: 6px;">{leave_reason}</td>
                </tr>
            </table>
            <br>
            <p>
                Please click <a href="{SYSTEM_LINK}" style="color: #007bff; font-weight: bold;">here</a> to acknowledge the request.
            </p>
            <br><br>
            <p>
                Thanks & Regards<br>
                Excel Rim
            </p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="color: #999; font-size: 12px;">
                This email is auto-generated by system.<br>
                Please do not reply to this email.
            </p>
        </body>
    </html>
    """

def build_pending_approval_email(leaves):
    rows_html = ""
    for i, leave in enumerate(leaves, 1):
        bg = '#f9f9f9' if i % 2 == 0 else '#ffffff'
        rows_html += f"""
            <tr style="background-color: {bg};">
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{i}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['requester']} - {leave['requester_name']}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['department'] or '-'}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['leave_type']}</td>
                <td style="padding: 8px; border: 1px solid #ddd;">{leave['leave_reason']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['start_date']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['end_date']}</td>
                <td style="padding: 8px; border: 1px solid #ddd; text-align:center;">{leave['no_days']} day(s)</td>
            </tr>
        """

    return f"""
    <html>
        <body style="font-family: Times New Roman, sans-serif; color: #333;">

            <p>Dear Approver,</p>

            <p>
                The following leaves have been acknowledged by the verifier and are currently awaiting your approval.
            </p>

            <table style="border-collapse: collapse; width: 100%; font-size: 13px;">
                <thead>
                    <tr>
                        <th style="padding: 10px; border: 1px solid #ddd;">No.</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Employee</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Department</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Leave Type</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Leave Reason</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Start Date</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">End Date</th>
                        <th style="padding: 10px; border: 1px solid #ddd;">Days</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>

            <br>
            <p>
                Please click 
                <a href="{SYSTEM_LINK}" style="color: #007bff; font-weight: bold;">here</a> 
                to review and process the requests.
            </p>

            <br>
            <p>
                Thanks & Regards,<br>
                <strong>Excel Rim HR System</strong>
            </p>
            
            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="color: #999; font-size: 12px;">
                This email is auto-generated by system.<br>
                Please do not reply to this email.
            </p>
        </body>
    </html>
    """
def send_pending_approval_email():
    pending_leaves = query_db("""
        SELECT lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.start_date, lr.end_date,
               lr.no_days, lr.approver
        FROM leave_request lr
        WHERE lr.recommender_status IN ('Acknowledged', 'N/A')
          AND (lr.recommender_2_status IN ('Acknowledged', 'N/A') OR lr.recommender_2 IS NULL)
          AND lr.approver_status = 'Pending'
          AND lr.status = 'Pending'
        ORDER BY lr.created_at ASC
    """)

    if not pending_leaves:
        logging.info("No pending approval leaves found. Skipping email.")
        return

    # Get the single approver email
    approver_row = query_db(
        "SELECT e.Email FROM employee e JOIN auth_user au ON e.Employee_no = au.employee_no WHERE au.roles LIKE '%Approver%' AND au.status = 'Active' LIMIT 1",
        one=True
    )

    if not approver_row or not approver_row['Email']:
        logging.warning("No approver email found. Skipping.")
        return

    body = build_pending_approval_email(pending_leaves)

    send_email(
        EMAIL_ADDRESS,
        approver_row['Email'],
        f"[Daily Reminder] {len(pending_leaves)} Leave Request(s) Pending Your Approval",
        body
    )

    logging.info(f"Pending approval digest sent to {approver_row['Email']} ({len(pending_leaves)} leave(s)).")

# -------------------------
# Initialize Scheduler
# -------------------------
scheduler = BackgroundScheduler(timezone='Asia/Kuala_Lumpur')
scheduler.add_job(
    send_pending_approval_email,
    trigger='cron',
    day_of_week='mon-fri',  # Weekdays only
    hour='8,14',            # 8:00 AM and 2:00 PM
    minute=0,
    id='pending_approval_digest',
    replace_existing=True
)
scheduler.start()
logging.info("Scheduler started. Pending approval digest will run Mon-Fri at 8AM and 2PM.")

# Shutdown scheduler gracefully when app exits
atexit.register(lambda: scheduler.shutdown())

def is_on_leave(employee_no):
    row = query_db("""
        SELECT 1
        FROM leave_request
        WHERE requester = %s
          AND status = 'Approved'
          AND CURDATE() BETWEEN start_date AND end_date
        LIMIT 1
    """, (employee_no,), one=True)
    return row is not None

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
                   e.Employee_name, s.section_id AS section_control
            FROM auth_user au
            LEFT JOIN employee e ON e.Employee_no = au.employee_no
            LEFT JOIN section s ON s.verifier_id = au.employee_no
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
            session['section_control'] = user.get('section_control', None)
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

    # Pagination
    per_page = 7  
    current_page = request.args.get('page', 1, type=int)

    # Filter
    filter_status = request.args.get('status', '').strip()
    filter_leave_type = request.args.get('leave_type', '').strip()
    filter_month = request.args.get('month', '', type=int)
    filter_year = request.args.get('year', '', type=int)
    filter_date_from = request.args.get('date_from', '').strip()
    filter_date_to = request.args.get('date_to', '').strip()

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *, 
            COALESCE(no_days, 0) AS no_days
        FROM leave_request
        WHERE requester=%s AND status != 'Cancel'
        ORDER BY created_at DESC
    """, (employee_no,))
    leave_requests = cursor.fetchall()

    for leave in leave_requests:
        is_verifier_for_this = False
        is_approver_for_this = False

        if 'Verifier' in roles:
            cursor.execute("""
                SELECT 1
                FROM employee e
                JOIN section s ON e.Section_id = s.section_id
                WHERE e.Employee_no = %s 
                    AND (s.verifier_id = %s OR s.verifier_id_2 = %s)
            """, (leave['requester'], employee_no, employee_no))
            is_verifier_for_this = cursor.fetchone() is not None

        if 'Approver' in roles:
            is_approver_for_this = True

        # Leave permissions
        can_edit = False
        if is_approver_for_this and not is_verifier_for_this:
            # Approver - can always edit
            can_edit = True
        elif is_verifier_for_this and not is_approver_for_this:
            # Verifier - can edit before approval
            can_edit = (leave.get('recommender_status') == 'Acknowledged' and leave.get('approver_status') == 'Pending')
        elif is_verifier_for_this and is_approver_for_this:
            # Verifier and Approver - can edit until final approval
            can_edit = leave.get('approver_status') == 'Pending'
        elif leave['requester'] == employee_no:
            # Employee edit their own leave
            can_edit = leave.get('recommender_status') == 'Pending'
        
        leave['can_edit'] = can_edit
        
        if leave['status'] == 'Pending':
            v1_done = leave.get('recommender_status') in ('Acknowledged', 'N/A')
            v2_done = leave.get('recommender_2_status') in ('Acknowledged', 'N/A')
            
            if leave.get('recommender_status') == 'Rejected' or leave.get('recommender_2_status') == 'Rejected':
                leave['display_status'] = 'Rejected'
            elif v1_done and v2_done:
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

    default_leaves = filtered_leaves

    total_leaves = len(default_leaves)
    total_pages = (total_leaves + per_page - 1) // per_page if total_leaves > 0 else 1

    if current_page < 1:
        current_page = 1
    elif current_page > total_pages and total_pages > 0:
        current_page = total_pages
    
    start_idx = (current_page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_leaves = default_leaves[start_idx:end_idx]

    today = date.today()
    upcoming_leaves = [l for l in leave_requests if l['status'] == 'Approved' and l['start_date'] >= today]

    cursor.close()
    conn.close()

    current_year = datetime.now().year

    return render_template(
        'dashboard.html',
        employee_name=employee_name,
        username=username,
        roles=roles,
        leave_requests=leave_requests,
        default_leaves=default_leaves,
        paginated_leaves=paginated_leaves,
        upcoming_leaves=upcoming_leaves,
        selected_status=[filter_status] if filter_status else [],
        current_page=current_page,
        total_pages=total_pages,
        total_leaves=total_leaves,
        per_page=per_page,
        current_year=current_year,
        filter_status=filter_status,
        filter_leave_type=filter_leave_type,
        filter_month=filter_month,
        filter_year=filter_year,
        filter_date_from=filter_date_from,
        filter_date_to=filter_date_to
    )

@app.route('/view_leave')
@login_required
@role_required('Verifier', 'Approver')
def view_leave():
    roles = session.get('roles', [])
    employee_no = session.get('employee_no')

    verifier_leaves = []
    approver_leaves = []

    # Verifier Section
    if 'Verifier' in roles:
        verifier_leaves = query_db("""
            SELECT lr.*, e.Employee_name AS requester_name
            FROM leave_request lr
            JOIN employee e ON lr.requester = e.Employee_no
            LEFT JOIN leave_request v1_leave
                ON v1_leave.requester = lr.recommender
                AND v1_leave.status = 'Approved'
                AND CURDATE() BETWEEN v1_leave.start_date AND v1_leave.end_date                  
            WHERE (
                -- V1: normal case
                (lr.recommender = %s AND lr.recommender_status = 'Pending')
                OR
                -- V2: V1 already acknowledged (normal flow)
                (lr.recommender_2 = %s AND lr.recommender_2_status = 'Pending'
                AND lr.recommender_status = 'Acknowledged')
                OR
                -- V2: V1 is on leave today, V2 can act alone
                (lr.recommender_2 = %s AND lr.recommender_2_status = 'Pending'
                AND lr.recommender_status = 'Pending'
                AND v1_leave.leave_id IS NOT NULL)
            )
            AND lr.requester != %s
            AND lr.status != 'Cancel'
            ORDER BY lr.created_at DESC
        """, (employee_no, employee_no, employee_no, employee_no))

    # Approver Section
    if 'Approver' in roles:
        all_approver_leaves = query_db("""
            SELECT lr.*, e.Employee_name AS requester_name
            FROM leave_request lr
            JOIN employee e ON lr.requester = e.Employee_no
            WHERE lr.approver_status = 'Pending'
              AND lr.status NOT IN ('Rejected', 'Cancel')
              AND (
                  -- Both verifiers acknowledged (or N/A)
                  (lr.recommender_status IN ('Acknowledged', 'N/A') OR lr.recommender IS NULL)
                  AND
                  (lr.recommender_2_status IN ('Acknowledged', 'N/A') OR lr.recommender_2 IS NULL)
              )
            ORDER BY lr.created_at DESC
        """)

        if employee_no == 'ER0001':
            approver_leaves = all_approver_leaves

        # Filter leaves based on role rules
        elif 'Verifier' in roles:
            # Verifier + Approver cannot approve:
            # 1. Own leave requests
            # 2. Leave from same department
            approver_dept = session.get('department')
            approver_leaves = [
                leave for leave in all_approver_leaves 
                if leave['requester'] != employee_no 
                and leave.get('department') != approver_dept
            ]
        else:
            # Only Approver (no Verifier role) — cannot approve own leave or same dept
            approver_dept = session.get('department')
            approver_leaves = [
                leave for leave in all_approver_leaves
                if leave['requester'] != employee_no
                and leave.get('department') != approver_dept
            ]

    return render_template(
        'manage_request.html',
        verifier_leaves=verifier_leaves,
        approver_leaves=approver_leaves,
        roles=roles,
        employee_no=employee_no
    )

@app.route('/apply_leave', methods=['GET', 'POST'])
@login_required
def apply_leave():
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])
    department = session.get('department')
    employee_name = session.get('employee_name')

    # Fetch Verifier department members
    subordinates = []
    if 'Verifier' in roles:
        subordinates = query_db("""
            SELECT e.Employee_no, e.Employee_name
            FROM employee e
            JOIN section s ON s.section_id = e.Section_id
            WHERE (s.verifier_id = %s OR s.verifier_id_2 = %s)
            AND e.Employee_no != %s
            AND e.status = 'Active'
        """, (employee_no, employee_no, employee_no))

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
            department=department,
            roles=roles,
            subordinates=subordinates
        )

    # -------------------------
    # POST: process leave form
    # -------------------------
    requester = request.form.get('requester_emp_no') if 'Verifier' in roles else employee_no
    if not requester:
        requester = employee_no
    is_self_apply = (requester == employee_no)

    # Get requester name
    emp_row = query_db(
        "SELECT Employee_name FROM employee WHERE Employee_no = %s AND status = 'Active'",
        (requester,), one=True
    )
    if not emp_row:
        flash('Employee information not found.', 'danger')
        return redirect('/apply_leave')
    requester_name = emp_row['Employee_name']

    # Leave reason
    reason = request.form.get('reason', '').strip()
    if reason == 'Other':
        reason = request.form.get('other_reason', '').strip()
    if not reason:
        flash('Please provide a leave reason.', 'danger')
        return redirect('/apply_leave')

    start_date_str = request.form.get('start_date', '').strip()
    end_date_str   = request.form.get('end_date', '').strip()

    if not start_date_str or not end_date_str:
        flash('Please provide both start and end dates.', 'danger')
        return redirect('/apply_leave')

    start_remark = request.form.get('start_remark') or None
    end_remark = request.form.get('end_remark') or None

    # Upload document
    upload_document = None
    file = request.files.get('upload_document')
    if file and file.filename:
        if not allowed_file(file.filename):
            flash('Invalid file extension. Allowed: PDF, PNG, JPG.', 'danger')
            return redirect('/apply_leave')
        if not allowed_file_type(file):
            flash('Invalid file type.', 'danger')
            return redirect('/apply_leave')
        
        try: 
            ext = file.filename.rsplit('.', 1)[1].lower()

            # Use the REQUESTER's department
            req_dept_row = query_db(
                "SELECT Department FROM employee WHERE Employee_no = %s",
                (requester,), one=True
            )
            req_dept = (req_dept_row['Department'] if req_dept_row and req_dept_row['Department'] else department or 'General')
            section_safe = req_dept.replace(' ', '_').replace('/', '-').replace('(', '').replace(')', '')

            yymmdd       = datetime.strptime(start_date_str, "%Y-%m-%d").strftime("%y%m%d")
            raw_filename = f"{yymmdd}_{section_safe}_{requester}.{ext}"
            filename     = secure_filename(raw_filename)

            if not filename or filename == f".{ext}":
                ts = datetime.now().strftime("%y%m%d%H%M%S")
                filename = f"{ts}_{requester}.{ext}"

            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

            file.seek(0)  
            file.save(file_path)

            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:  
                upload_document = filename
            else:
                flash('File failed to save. Please try again.', 'danger')
                return redirect('/apply_leave')
                
        except Exception as e:
            flash(f'Error uploading file: {str(e)}', 'danger')
            return redirect('/apply_leave')

    # Calculate number of days
    no_days = calculate_leave_days(
        start_date_str,
        end_date_str,
        request.form.get('remark'),
        start_remark,
        end_remark
    )

    # -------------------------
    # Determine verifiers & approver
    # -------------------------
    verifier_row = query_db("""
        SELECT s.verifier_id, s.verifier_id_2, s.section_id
        FROM employee e
        JOIN section s ON s.section_id = e.Section_id
        WHERE e.Employee_no = %s
    """, (requester,), one=True)

    recommender = verifier_row['verifier_id'] if verifier_row else None
    recommender_2 = verifier_row['verifier_id_2'] if verifier_row else None
    section_id = verifier_row['section_id'] if verifier_row else None

    AUTO_ACK_SECTIONS = ['SL', 'PC']
    is_auto_ack_section = section_id in AUTO_ACK_SECTIONS

    # Initial status
    recommender_status = 'Pending'
    recommender_2_status = 'Pending' if recommender_2 else 'N/A'
    approver_status = 'Pending'
    status = 'Pending'

    # Handle self apply or auto ack section
    if 'Approver' in roles and is_self_apply:
        recommender_status = 'Acknowledged'
        recommender_2_status = 'N/A'
        approver_status = 'Approved'
        status = 'Approved'
    elif is_auto_ack_section:
        recommender_status = 'Acknowledged'
        recommender_2_status = 'N/A'
        recommender_2 = None
    elif 'Verifier' in roles:
        recommender_status = 'Acknowledged'
        recommender_2_status = 'N/A'
        approver_status = 'Pending'
        status = 'Pending'
    elif is_self_apply and recommender == employee_no:
        recommender_status = 'Acknowledged'
        recommender_2_status = 'N/A'
        approver_status = 'Pending'
        status = 'Pending'

    # -------------------------
    # Insert leave request
    # -------------------------
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO leave_request
            (requester, requester_name, applied_by, department, created_at, leave_type, leave_reason, 
            start_date, end_date, no_days, remark, start_remark, end_remark, upload_document,
            recommender, recommender_status, recommender_2, recommender_2_status, 
            approver, approver_status, status, status_update)
            VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            requester, requester_name, employee_no, department,
            request.form['leave_type'], reason,
            request.form['start_date'], request.form['end_date'],
            no_days, request.form.get('remark'),
            start_remark, end_remark, upload_document,
            recommender, recommender_status, recommender_2, recommender_2_status,
            approver, approver_status, status
        ))
        conn.commit()
        flash('Leave request submitted successfully!', 'success')

        # Email Notification
        if not is_auto_ack_section:
            def is_on_leave_today(emp_no):
                row = query_db("""
                    SELECT 1 FROM leave_request
                    WHERE requester=%s AND status='Approved'
                    AND CURDATE() BETWEEN start_date AND end_date
                    LIMIT 1
                """, (emp_no,), one=True)
                return row is not None

            recipients = []
            # Verifier 1
            if recommender and not is_on_leave_today(recommender):
                r = query_db("SELECT Email FROM employee WHERE Employee_no=%s", (recommender,), one=True)
                if r and r['Email']:
                    recipients.append(r['Email'])
            # Verifier 2 — always notify if exists and not on leave
            if recommender_2 and recommender_2_status != 'N/A' and not is_on_leave_today(recommender_2):
                r = query_db("SELECT Email FROM employee WHERE Employee_no=%s", (recommender_2,), one=True)
                if r and r['Email']:
                    recipients.append(r['Email'])

            if recipients:
                email_body = build_leave_email(requester, requester_name, request.form['leave_type'], reason)
                send_email(EMAIL_ADDRESS, recipients, "Leave Request Submitted", email_body)

    except Exception as e:
        conn.rollback()
        flash(f'Error submitting leave request: {str(e)}', 'danger')
        return redirect('/apply_leave')
    finally:
        cursor.close()
        conn.close()

    return redirect('/dashboard')

@app.route('/update_leave/<int:leave_id>', methods=['POST'])
@login_required
def update_leave(leave_id):
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])

    leave_type = request.form.get('leave_type')
    leave_reason = request.form.get('leave_reason', '').strip()
    if leave_reason == 'Other':
        leave_reason = request.form.get('other_reason', '').strip()
        if not leave_reason:
            flash('Please specify the leave reason.', 'danger')
            return redirect('/dashboard')

    start_date = request.form.get('start_date')
    end_date = request.form.get('end_date')
    remark = request.form.get('remark', '')
    start_remark = request.form.get('start_remark', '')
    end_remark = request.form.get('end_remark', '')

    no_days = calculate_leave_days(
        start_date,
        end_date,
        remark,
        start_remark,
        end_remark
    )

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT upload_document, recommender_status, approver_status, requester
            FROM leave_request
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))
        row = cursor.fetchone()

        if not row:
            flash('Only Pending leave requests can be edited.', 'warning')
            return redirect('/dashboard')

        # Role based access control
        is_verifier_for_this = False
        is_approver_for_this = False

        if 'Verifier' in roles:
            # Check if current user is the verifier for this leave's requester
            cursor.execute("""
                SELECT 1
                FROM employee e
                JOIN section s ON e.Section_id = s.section_id
                WHERE e.Employee_no = %s AND (s.verifier_id = %s OR s.verifier_id_2 = %s)
            """, (row['requester'], employee_no, employee_no))
            is_verifier_for_this = cursor.fetchone() is not None

        if 'Approver' in roles:
            is_approver_for_this = True

        # Apply appropriate permission based on their role for THIS leave
        can_edit = False
        
        if is_approver_for_this and not is_verifier_for_this:
            # Approver - can always edit
            can_edit = True
        elif is_verifier_for_this and not is_approver_for_this:
            # Verifier - can edit before approval
            can_edit = (row['recommender_status'] == 'Acknowledged' and row['approver_status'] == 'Pending')
        elif is_verifier_for_this and is_approver_for_this:
            # Has both roles for this leave - can edit until final approval
            can_edit = row['approver_status'] == 'Pending'
        elif row['requester'] == employee_no:
            # Employee editing their own leave
            can_edit = row['recommender_status'] == 'Pending'
        
        if not can_edit:
            flash('You do not have permission to edit this leave request.', 'danger')
            return redirect('/dashboard')
        
        old_document = row['upload_document']
        old_recommender_status = row['recommender_status']
        new_document = old_document

        # Handle file upload
        file = request.files.get('upload_document')
        if file and file.filename:
            if not allowed_file(file.filename) or not allowed_file_type(file):
                flash('Invalid file format.', 'danger')
                return redirect('/dashboard')
            
            try:
                ext = file.filename.rsplit('.', 1)[1].lower()
                date_part = start_date.replace('-', '')[2:]

                # Use REQUESTER's department
                req_dept_row = query_db(
                    "SELECT Department FROM employee WHERE Employee_no = %s",
                    (row['requester'],), one=True
                )
                req_dept = (req_dept_row['Department'] if req_dept_row and req_dept_row['Department']
                            else session.get('department', 'General'))
                section_safe = req_dept.replace(' ', '_').replace('/', '-').replace('(', '').replace(')', '')

                raw_filename = f"{date_part}_{section_safe}_{row['requester']}.{ext}"
                filename = secure_filename(raw_filename)

                if not filename or filename == f".{ext}":
                    ts = datetime.now().strftime("%y%m%d%H%M%S")
                    filename = f"{ts}_{row['requester']}.{ext}"

                if old_document:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], old_document)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception as e:
                            print(f"Error deleting old file: {e}")

                os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.seek(0)         
                file.save(file_path)

                if os.path.exists(file_path) and os.path.getsize(file_path) > 0: 
                    new_document = filename
                else:
                    flash('File failed to save. Please try again.', 'danger')
                    return redirect('/dashboard')

            except Exception as e:
                flash(f'Error saving file: {str(e)}', 'danger')
                return redirect('/dashboard')

        # Update leave - reset recommender_status to Pending
        new_recommender_status = 'Pending' if old_recommender_status == 'Pending' else old_recommender_status
        new_recommender_2_status = None

        cursor.execute("""
            SELECT recommender_2, recommender_2_status FROM leave_request WHERE leave_id = %s
        """, (leave_id,))
        v2_row = cursor.fetchone()
        if v2_row and v2_row['recommender_2']:
            # If V2 exists, reset their status too when V1 is reset
            new_recommender_2_status = 'Pending' if old_recommender_status == 'Pending' else v2_row['recommender_2_status']
        else:
            new_recommender_2_status = v2_row['recommender_2_status'] if v2_row else 'N/A'

        cursor.execute("""
            UPDATE leave_request
            SET leave_type=%s, leave_reason=%s, start_date=%s, end_date=%s, no_days=%s,
                remark=%s, start_remark=%s, end_remark=%s, upload_document=%s,
                recommender_status=%s, recommender_2_status=%s, status_update=NOW()
            WHERE leave_id=%s AND status='Pending'
        """, (
            leave_type, leave_reason, start_date, end_date, no_days,
            remark, start_remark, end_remark, new_document,
            new_recommender_status, new_recommender_2_status, leave_id
        ))
        conn.commit()
        flash('Leave request updated successfully!', 'success')

    except Exception as e:
        conn.rollback()
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
            SELECT upload_document, recommender_status, approver_status, requester
            FROM leave_request
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))
        row = cursor.fetchone()

        if not row:
            return jsonify(success=False, message='Only Pending leave requests can be cancelled.')
        
        # Determine which role applies to this specific leave
        is_verifier_for_this = False
        is_approver_for_this = False

        if 'Verifier' in roles:
            # Check if current user is the verifier for this leave's requester
            cursor.execute("""
                SELECT 1
                FROM employee e
                JOIN section s ON e.Section_id = s.section_id
                WHERE e.Employee_no = %s AND (s.verifier_id = %s OR s.verifier_id_2 = %s)
            """, (row['requester'], employee_no, employee_no))
            is_verifier_for_this = cursor.fetchone() is not None

        if 'Approver' in roles:
            is_approver_for_this = True

        # Apply appropriate permission based on their role for THIS leave
        can_cancel = False
        
        if is_approver_for_this and not is_verifier_for_this:
            # Approver - can always cancel
            can_cancel = True
        elif is_verifier_for_this and not is_approver_for_this:
            # Verifier - can cancel before approval
            can_cancel = (row['recommender_status'] == 'Acknowledged' and row['approver_status'] == 'Pending')
        elif is_verifier_for_this and is_approver_for_this:
            # Has both roles for this leave - can cancel until final approval
            can_cancel = row['approver_status'] == 'Pending'
        elif row['requester'] == employee_no:
            # Employee cancelling their own leave
            can_cancel = row['recommender_status'] == 'Pending'
        
        if not can_cancel:
            return jsonify(success=False, message='You do not have permission to cancel this leave request.')

        document = row['upload_document']

        # Update database first
        cursor.execute("""
            UPDATE leave_request
            SET status = 'Cancel',
                status_update = NOW()
            WHERE leave_id = %s AND status = 'Pending'
        """, (leave_id,))

        conn.commit()

        # Delete file after successful commit
        if document:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], document)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
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

@app.route('/acknowledge_leave/<int:leave_id>/<action>', methods=['POST'])
@login_required
@role_required('Verifier')
def acknowledge_leave(leave_id, action):
    employee_no = session.get('employee_no')
    reject_reason = request.form.get('reject_reason')

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT requester, requester_name, leave_type, leave_reason,
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

        if action == 'acknowledge':
            if is_v1:
                cursor.execute("""
                    UPDATE leave_request
                    SET recommender_status='Acknowledged', status_update=NOW()
                    WHERE leave_id=%s
                """, (leave_id,))
            elif is_v2:
                cursor.execute("""
                    UPDATE leave_request
                    SET recommender_2_status='Acknowledged', status_update=NOW()
                    WHERE leave_id=%s
                """, (leave_id,))
            conn.commit()
            flash('Leave request acknowledged successfully.', 'success')

        elif action == 'reject':
            if not reject_reason or not reject_reason.strip():
                flash('Please provide a reason for rejection.', 'warning')
                return redirect(url_for('view_leave'))
            # Reject the leave request
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
    employee_no = session.get('employee_no')
    reject_reason = request.form.get('reject_reason')
    user_roles = session.get('roles', [])
    
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT requester, approver, approver_status, recommender, recommender_status, recommender_2, recommender_2_status
            FROM leave_request
            WHERE leave_id = %s
        """, (leave_id,))
        leave = cursor.fetchone()

        if not leave:
            flash('Leave request not found.', 'danger')
            return redirect(url_for('view_leave'))
        
        # ********************
        # Authorization Check
        # ********************

        # Cannot approve own leave
        if leave['requester'] == employee_no:
            flash('You cannot approve your own leave request.', 'danger')
            return redirect(url_for('view_leave'))
        
        # If user is both verifier and approver
        if 'Verifier' in user_roles and 'Approver' in user_roles:
            if employee_no in [leave['recommender'], leave['recommender_2']]:
                flash('You cannot approve a leave you verified.', 'danger')
                return redirect(url_for('view_leave'))
            
        # Check approver
        approver_dept = session.get('department')
        cursor.execute("SELECT department FROM leave_request WHERE leave_id = %s", (leave_id,))
        leave_dept_row = cursor.fetchone()
        if leave_dept_row and leave_dept_row.get('department') == approver_dept:
            flash('You are not authorized to approve this leave request.', 'danger')
            return redirect(url_for('view_leave'))

        # ********************
        # Status Checks
        # ********************

        if leave['approver_status'] != 'Pending':
            flash('This leave request has already been processed.', 'warning')
            return redirect(url_for('view_leave'))

        # Verifier completion checks
        v1_done = (
            leave['recommender'] is None or
            leave['recommender_status'] in ('Acknowledged', 'N/A')
        )

        v2_done = (
            leave['recommender_2'] is None or
            leave['recommender_2_status'] in ('Acknowledged', 'N/A')
        )

        # Rejection checks
        if leave['recommender_status'] == 'Rejected' or leave['recommender_2_status'] == 'Rejected':
            flash('This leave request has been rejected by a verifier.', 'warning')
            return redirect(url_for('view_leave'))

        if not (v1_done and v2_done):
            flash('Leave must be acknowledged by all verifiers before approval.', 'warning')
            return redirect(url_for('view_leave'))
        
        # ********************
        # Process Action
        # ********************

        if action == 'approve':
            cursor.execute("""
                UPDATE leave_request 
                SET status = 'Approved', 
                    approver_status = 'Approved',
                    status_update = NOW()
                WHERE leave_id = %s
            """, (leave_id,))
            flash('Leave request approved successfully.', 'success')

        elif action == 'reject':
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
    user_roles = session.get('roles', [])

    try:
        approved_count = 0
        skipped_count = 0

        for leave_id in leave_ids:
            cursor.execute("""
                SELECT recommender_status, recommender_2_status, approver_status, requester, approver, recommender, recommender_2
                FROM leave_request
                WHERE leave_id = %s
            """, (leave_id,))
            leave = cursor.fetchone()

            if not leave:
                skipped_count += 1
                continue

            # Skip own leave
            if leave['requester'] == employee_no:
                skipped_count += 1
                continue

            # Skip if this user was a verifier for this leave
            if 'Verifier' in user_roles:
                if employee_no in [leave['recommender'], leave['recommender_2']]:
                    skipped_count += 1
                    continue

            # If Verifier+Approver, skip same-department leaves
            if 'Verifier' in user_roles and 'Approver' in user_roles:
                approver_dept = session.get('department')
                if leave.get('department') == approver_dept:
                    skipped_count += 1
                    continue

            # Skip if not the assigned approver (unless ER0001)
            if employee_no != 'ER0001' and leave['approver'] != employee_no:
                skipped_count += 1
                continue

            # Already processed
            if leave['approver_status'] != 'Pending':
                skipped_count += 1
                continue

            # FIX: treat NULL recommender_2_status as done
            v1_done = (
                leave['recommender'] is None or
                leave['recommender_status'] in ('Acknowledged', 'N/A')
            )
            v2_done = (
                leave['recommender_2'] is None or
                leave['recommender_2_status'] is None or
                leave['recommender_2_status'] in ('Acknowledged', 'N/A')
            )

            if not v1_done or not v2_done:
                skipped_count += 1
                continue

            cursor.execute("""
                UPDATE leave_request
                SET status = 'Approved',
                    approver_status = 'Approved',
                    status_update = NOW()
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
@role_required('Verifier', 'Approver')
def employee_history():
    roles = session.get('roles', [])
    employee_no = session.get('employee_no')

    # Fetch all subordinates where this user is recommender_1 or recommender_2
    subordinates = query_db("""
        SELECT DISTINCT e.Employee_no, e.Employee_name
        FROM employee e
        JOIN leave_request lr ON lr.requester = e.Employee_no
        WHERE (lr.recommender = %s OR lr.recommender_2 = %s)
          AND e.Employee_no != %s
    """, (employee_no, employee_no, employee_no))

    # Filters
    filter_employee   = request.args.get('employee', '')
    filter_status     = request.args.get('status', '')
    filter_leave_type = request.args.get('leave_type', '')
    filter_month      = request.args.get('month', '')
    filter_date_from  = request.args.get('date_from', '')
    filter_date_to    = request.args.get('date_to', '')
    current_page      = int(request.args.get('page', 1))
    per_page          = 10

    query = """
        SELECT lr.*,
               e.Employee_name    AS requester_name,
               e.department,
               v1.Employee_name   AS recommender_name,
               v2.Employee_name   AS recommender_2_name,
               ap.Employee_name   AS approver_name
        FROM leave_request lr
        JOIN employee e    ON lr.requester       = e.Employee_no
        LEFT JOIN employee v1 ON lr.recommender   = v1.Employee_no
        LEFT JOIN employee v2 ON lr.recommender_2 = v2.Employee_no
        LEFT JOIN employee ap ON lr.approver      = ap.Employee_no
        WHERE (lr.recommender = %s OR lr.recommender_2 = %s)
          AND lr.requester != %s
    """
    params = [employee_no, employee_no, employee_no]

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

    all_leaves   = query_db(query, params)
    total_leaves = len(all_leaves)
    total_pages  = max(1, math.ceil(total_leaves / per_page))
    current_page = max(1, min(current_page, total_pages))
    paginated_leaves = all_leaves[(current_page - 1) * per_page : current_page * per_page]

    leave_types = query_db("""
        SELECT DISTINCT leave_type FROM leave_request
        WHERE recommender = %s OR recommender_2 = %s
    """, (employee_no, employee_no))

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

@app.route('/profile')
@login_required
def profile():
    employee_no = session['employee_no']
    employee_name = session.get('employee_name', session.get('username'))

    # Employee Info
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

    # --- Years of Service ---
    years_of_service = 0
    if employee['Register_date']:
        years_of_service = (today - employee['Register_date']).days / 365.25

    # --- Annual Leave Policy (FULL YEAR ENTITLEMENT) ---
    if years_of_service < 2:
        annual_leave_per_year = 10
    elif years_of_service < 5:
        annual_leave_per_year = 14
    else:
        annual_leave_per_year = 18

    # --- Proration Logic (KIV for now) ---
    # monthly_accrual_rate = annual_leave_per_year / 12
    # register_date = employee['Register_date']
    # if register_date.year == current_year:
    #     months_worked = (today.month - register_date.month) + 1
    # else:
    #     months_worked = today.month
    # prorated_entitled = round(monthly_accrual_rate * months_worked, 1)

    # --- Approved Leave Usage (Current Year) ---
    leave_breakdown = query_db("""
        SELECT leave_type, COALESCE(SUM(no_days), 0) AS days_taken
        FROM leave_request
        WHERE requester = %s
          AND status = 'Approved'
          AND YEAR(start_date) = %s
        GROUP BY leave_type
    """, (employee_no, current_year))

    leave_breakdown_dict = {row['leave_type']: float(row['days_taken']) for row in leave_breakdown}

    annual_leave_used = leave_breakdown_dict.get('Annual Leave', 0)
    medical_leave_used = leave_breakdown_dict.get('Medical Leave', 0)
    emergency_leave_used = leave_breakdown_dict.get('Emergency Leave', 0)
    unpaid_leave_used = leave_breakdown_dict.get('Unpaid Leave', 0)

    other_leave_used = sum(
        v for k, v in leave_breakdown_dict.items()
        if k not in ['Annual Leave', 'Medical Leave', 'Emergency Leave', 'Unpaid Leave']
    )

    total_used_days = sum(leave_breakdown_dict.values())

    # --- Leave Summary ---
    total_entitled = annual_leave_per_year          # Full year
    # prorated_entitled = prorated_entitled          # KIV
    days_taken = annual_leave_used
    # total_balance = prorated_entitled - days_taken # KIV

    # --- Pending Leave ---
    pending_days_row = query_db("""
        SELECT COALESCE(SUM(no_days), 0) AS pending_days
        FROM leave_request
        WHERE requester = %s
          AND status = 'Pending'
          AND YEAR(start_date) = %s
    """, (employee_no, current_year), one=True)

    pending_days = float(pending_days_row['pending_days']) if pending_days_row else 0
    # available_balance = total_balance - pending_days  # KIV

    # --- Leave Status Counts ---
    status_counts_raw = query_db("""
        SELECT status, COUNT(*) AS count
        FROM leave_request
        WHERE requester = %s
          AND YEAR(start_date) = %s
        GROUP BY status
    """, (employee_no, current_year))

    status_counts = {row['status']: row['count'] for row in status_counts_raw}

    acknowledged_count = query_db("""
        SELECT COUNT(*) AS count
        FROM leave_request
        WHERE requester = %s
          AND status = 'Pending'
          AND recommender_status = 'Acknowledged'
          AND YEAR(start_date) = %s
    """, (employee_no, current_year), one=True)

    if acknowledged_count and acknowledged_count['count'] > 0:
        status_counts['Acknowledged'] = acknowledged_count['count']
        status_counts['Pending'] = status_counts.get('Pending', 0) - acknowledged_count['count']

    # --- Leave Dictionaries (UI Friendly) ---
    entitled_leaves = {
        'Annual Leave': total_entitled
    }

    leave_balance = {
        'Annual Leave': total_entitled - annual_leave_used  # Simple full-year balance
    }

    # --- Recent Leaves ---
    recent_leaves = query_db("""
        SELECT *
        FROM leave_request
        WHERE requester = %s
          AND YEAR(start_date) = %s
        ORDER BY start_date DESC
        LIMIT 10
    """, (employee_no, current_year))

    return render_template(
        'profile.html',
        employee=employee,
        employee_name=employee_name,

        # Leave Summary
        total_entitled=total_entitled,
        # prorated_entitled=prorated_entitled,  # KIV
        days_taken=days_taken,
        total_balance=leave_balance['Annual Leave'],
        # available_balance=available_balance,  # KIV

        # Leave Details
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

        # Meta
        pending_days=pending_days,
        status_counts=status_counts,
        current_year=current_year,
        years_of_service=round(years_of_service, 1),
        annual_leave_per_year=annual_leave_per_year,
        # monthly_accrual_rate=round(monthly_accrual_rate, 2),  # KIV
        # months_worked=months_worked,  # KIV
        recent_leaves=recent_leaves,
    )

@app.route('/get_leaves')
def get_leaves():
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        employee_no = session.get('employee_no')
        roles = session.get('roles', [])

        leaves = []

        if employee_no:
            # HR and pure Approver see all departments
            if 'HR' in roles or ('Approver' in roles and 'Verifier' not in roles):
                cursor.execute("""
                    SELECT requester_name, start_date, end_date, remark, start_remark, end_remark
                    FROM leave_request
                    WHERE status = 'Approved'
                """)
                raw_leaves = cursor.fetchall()

            else:
                # Verifier, Verifier+Approver, Employee — department only
                cursor.execute("""
                    SELECT Department
                    FROM employee
                    WHERE Employee_no = %s
                """, (employee_no,))
                user_info = cursor.fetchone()

                if user_info:
                    cursor.execute("""
                        SELECT requester_name, start_date, end_date, remark, start_remark, end_remark
                        FROM leave_request
                        WHERE status = 'Approved'
                          AND department = %s
                    """, (user_info['Department'],))
                    raw_leaves = cursor.fetchall()
                else:
                    raw_leaves = []

            for leave in raw_leaves:
                current_date = leave["start_date"]
                end_date = leave["end_date"]

                while current_date <= end_date:
                    if current_date.weekday() < 5:
                        # Determine remark for this specific day
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

        # Public holidays — visible to everyone
        cursor.execute("""
            SELECT holiday_name, start_date, end_date
            FROM public_holidays
            WHERE status = 'Active'
        """)
        public_holidays = cursor.fetchall()

        for holiday in public_holidays:
            current_date = holiday["start_date"]
            end_date = holiday["end_date"]

            while current_date <= end_date:
                leaves.append({
                    "title": holiday["holiday_name"],
                    "start": current_date.strftime('%Y-%m-%d'),
                    "allDay": True,
                    "className": "public-holiday"
                })
                current_date += timedelta(days=1)

        # Recommended leave dates — visible to everyone
        cursor.execute("""
            SELECT ph.holiday_name, rl.leave_date
            FROM recommended_leave rl
            JOIN public_holidays ph ON rl.holiday_id = ph.holiday_id
            WHERE ph.status = 'Active'
        """)
        recommended_leaves = cursor.fetchall()

        for rec in recommended_leaves:
            leaves.append({
                "title": f"{rec['holiday_name']}",
                "start": rec["leave_date"].strftime('%Y-%m-%d'),
                "allDay": True,
                "className": "recommended-leave"
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
    
    cursor.execute("""
        SELECT start_date, end_date, holiday_name
        FROM public_holidays
        WHERE status = 'Active'
    """)
    holidays = cursor.fetchall()
    
    cursor.execute("""
        SELECT rl.leave_date, ph.holiday_name
        FROM recommended_leave rl
        JOIN public_holidays ph ON rl.holiday_id = ph.holiday_id
        WHERE ph.status = 'Active'
    """)
    recommended = cursor.fetchall()

    cursor.close()
    conn.close()
    
    # Convert dates to string format
    holiday_dates = []

    for holiday in holidays:
        current = holiday['start_date']
        end = holiday['end_date']
        while current <= end:
            holiday_dates.append({
                'date': current.strftime('%Y-%m-%d'),
                'name': holiday['holiday_name']
            })
            current += timedelta(days=1)
    
    for rec in recommended:
        holiday_dates.append({
            'date': rec['leave_date'].strftime('%Y-%m-%d'),
            'name': rec['holiday_name']
        })

    return jsonify(holiday_dates)

def _escape_ics_text(s):
    if s is None:
        return ''
    s = str(s)
    s = s.replace('\\', '\\\\')
    s = s.replace('\n', '\\n')
    s = s.replace(',', '\\,')
    s = s.replace(';', '\\;')
    return s

@app.route('/calendar.ics')
def calendar_ics():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # Get approved leaves
    cursor.execute("""
        SELECT leave_id, requester_name, start_date, end_date, leave_type, leave_reason
        FROM leave_request
        WHERE status = 'Approved' AND end_date >= CURDATE()
        ORDER BY start_date
    """)
    leaves = cursor.fetchall()

    # Get public holidays
    cursor.execute("""
        SELECT holiday_id, holiday_name, start_date, end_date, description
        FROM public_holidays
        WHERE status = 'Active' AND end_date >= CURDATE()
        ORDER BY start_date
    """)
    holidays = cursor.fetchall()

    cursor.close()
    conn.close()

    now_utc = datetime.utcnow()
    cal_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//YourOrg//HR Leave Calendar//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:HR Leave Calendar"
    ]

    # Add employee leaves
    for r in leaves:
        start = r['start_date']
        end = r['end_date']
        if isinstance(start, str):
            start = datetime.strptime(start, "%Y-%m-%d").date()
        if isinstance(end, str):
            end = datetime.strptime(end, "%Y-%m-%d").date()

        dtstart = start.strftime("%Y%m%d")
        dtend = (end + timedelta(days=1)).strftime("%Y%m%d")

        uid = f"leave-{r['leave_id']}@{request.host.split(':')[0]}"
        dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
        summary = f"{r.get('requester_name','Employee')} - {r.get('leave_type','Leave')}"
        description = r.get('leave_reason', '')

        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_escape_ics_text(summary)}",
            f"DESCRIPTION:{_escape_ics_text(description)}",
            "TRANSP:OPAQUE",
            "END:VEVENT"
        ])

    # Add public holidays
    for h in holidays:
        start = h['start_date']
        end = h['end_date']
        if isinstance(start, str):
            start = datetime.strptime(start, "%Y-%m-%d").date()
        if isinstance(end, str):
            end = datetime.strptime(end, "%Y-%m-%d").date()

        dtstart = start.strftime("%Y%m%d")
        dtend = (end + timedelta(days=1)).strftime("%Y%m%d")

        uid = f"holiday-{h['holiday_id']}@{request.host.split(':')[0]}"
        dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
        summary = f"🎉 {h.get('holiday_name','Public Holiday')}"
        description = h.get('description', '')

        cal_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_escape_ics_text(summary)}",
            f"DESCRIPTION:{_escape_ics_text(description)}",
            "TRANSP:OPAQUE",
            "END:VEVENT"
        ])

    cal_lines.append("END:VCALENDAR")
    ics_text = "\r\n".join(cal_lines) + "\r\n"

    return Response(ics_text, mimetype='text/calendar; charset=utf-8')

# HR ROUTES
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

def save_employee(emp_no, emp_name, department, position='', section_id='', email='', phone='', status='Active'):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT Employee_no FROM employee WHERE Employee_no=%s", (emp_no,))
        exists = cursor.fetchone()

        if exists:
            cursor.execute("""
                UPDATE employee
                SET Employee_name=%s, Department=%s, Position=%s,
                    Email=%s, Phone=%s, Section_id=%s, status=%s
                WHERE Employee_no=%s
            """, (emp_name, department, position, email, phone, section_id, status, emp_no))
        else:
            cursor.execute("""
                INSERT INTO employee
                (Employee_no, Employee_name, Department, Position,
                 Email, Phone, Section_id, Register_date, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,CURDATE(),%s)
            """, (emp_no, emp_name, department, position, email, phone, section_id, status))

        conn.commit()
        return True, "Employee saved"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cursor.close()
        conn.close()

def save_user_account(emp_no, username, password, department, roles):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT employee_no FROM auth_user WHERE employee_no=%s", (emp_no,))
        exists = cursor.fetchone()

        if exists:
            cursor.execute("""
                UPDATE auth_user
                SET username=%s, password=%s, department=%s, roles=%s
                WHERE employee_no=%s
            """, (username, password, department, roles, emp_no))
        else:
            cursor.execute("""
                INSERT INTO auth_user
                (employee_no, username, password, department, roles, status)
                VALUES (%s,%s,%s,%s,%s,'Active')
            """, (emp_no, username, password, department, roles))

        conn.commit()
        return True, "User account created"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cursor.close()
        conn.close()

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
        SELECT COUNT(*) as total
        FROM leave_request
        WHERE status = 'Approved' AND MONTH(start_date) = MONTH(CURDATE()) AND YEAR(start_date) = YEAR(CURDATE())
    """)
    approved_this_month = cursor.fetchone().get('total', 0)

    # Fetch approved leave this month
    cursor.execute("""
        SELECT leave_id, requester, requester_name, department, leave_type, leave_reason, 
               start_date, end_date, no_days, status
        FROM leave_request
        WHERE status = 'Approved' 
        AND MONTH(start_date) = MONTH(CURDATE()) 
        AND YEAR(start_date) = YEAR(CURDATE())
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
        LEFT JOIN auth_user au 
        ON e.Employee_no = au.employee_no
        AND au.company = e.company
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
        
        # Get employee data
        cursor.execute("""
            SELECT Employee_no AS emp_no, Employee_name AS emp_name, Position AS position,
                   Department, Section_id AS section_id, Email, Phone, Status, Register_date
            FROM employee
            WHERE Employee_no = %s
        """, (emp_no,))
        employee = cursor.fetchone()
        
        # Get user account data if exists
        cursor.execute("""
            SELECT username, password, roles, status, company, department
            FROM auth_user
            WHERE employee_no = %s
        """, (emp_no,))
        users = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        if not employee:
            flash("Employee not found!", "danger")
            return redirect(url_for("hr_users"))

    if request.method == 'POST':
        # Employee data
        emp_no_form = request.form['emp_no'].strip()
        emp_name = request.form['emp_name'].strip()
        department = request.form['department'].strip()
        section_id = request.form.get('section_control', '').strip()
        position = request.form.get('position', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        register_date = request.form.get('register_date', '').strip()
        status = request.form.get('status', 'Active')
        
        # User account data
        create_user = request.form.get("create_user") == "yes"
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        roles = request.form.get('roles', 'Employee').strip()

        # Validation
        if not emp_no_form or not emp_name or not department or not register_date:
            flash("Please fill in all required fields (Employee No, Name, Department, Register Date)", "danger")
            return redirect(request.url)

        if create_user:
            if not username:
                flash("Username is required when creating a user account", "danger")
                return redirect(request.url)
            
            if not emp_no and not password:  # New employee needs password
                flash("Password is required when creating a new user account", "danger")
                return redirect(request.url)
            
            if roles == "Verifier" and not section_id:
                flash("Verifier role requires a Section assignment", "warning")
                return redirect(request.url)

        conn = get_db()
        cursor = conn.cursor()
        
        try:
            # Save/Update Employee
            if emp_no:  # Update existing
                cursor.execute("""
                    UPDATE employee
                    SET Employee_name=%s, Department=%s, Section_id=%s, Position=%s, 
                        Email=%s, Phone=%s, Status=%s, Register_date=%s,
                        Last_edit_date=%s, Last_edit_user=%s
                    WHERE Employee_no=%s
                """, (emp_name, department, section_id or None, position, email, phone, 
                      status, register_date, datetime.now(), session.get('employee_name'), emp_no_form))
            else:  # Create new
                cursor.execute("""
                    INSERT INTO employee
                    (Employee_no, Employee_name, Department, Section_id, Position, 
                     Email, Phone, Status, Register_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (emp_no_form, emp_name, department, section_id or None, position, 
                      email, phone, status, register_date))

            # Handle User Account
            cursor.execute("""
                SELECT ID FROM auth_user WHERE employee_no = %s
            """, (emp_no_form,))
            existing_user = cursor.fetchone()

            if create_user:
                # Check if username already exists (for different employee)
                cursor.execute("""
                    SELECT ID FROM auth_user 
                    WHERE username = %s AND employee_no != %s
                """, (username, emp_no_form))
                if cursor.fetchone():
                    conn.rollback()
                    flash(f"Username '{username}' is already taken by another user", "danger")
                    return redirect(request.url)

                if existing_user:
                    # Update existing user account
                    if password:  # Only update password if provided
                        cursor.execute("""
                            UPDATE auth_user
                            SET username=%s, password=%s, roles=%s, department=%s,
                                last_edit_date=%s, last_edit_user=%s
                            WHERE employee_no=%s
                        """, (username, password, roles, department, 
                              datetime.now(), session.get('employee_name'), emp_no_form))
                    else:
                        cursor.execute("""
                            UPDATE auth_user
                            SET username=%s, roles=%s, department=%s,
                                last_edit_date=%s, last_edit_user=%s
                            WHERE employee_no=%s
                        """, (username, roles, department, 
                              datetime.now(), session.get('employee_name'), emp_no_form))
                else:
                    # Create new user account
                    cursor.execute("""
                        INSERT INTO auth_user
                        (username, password, employee_no, department, roles, status)
                        VALUES (%s, %s, %s, %s, %s, 'Active')
                    """, (username, password, emp_no_form, department, roles))
            else:
                pass

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

    # Prepare data for template
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

@app.route('/hr/leaves')
@login_required
@hr_required
def hr_leaves():
    # Pagination setup
    per_page = 8  # Rows
    current_page = request.args.get('page', 1, type=int)
 
    # Get filter parameters
    status_filter = request.args.get('status', '')
    department_filter = request.args.get('department', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
 
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)
 
    query = """
        SELECT lr.leave_id, lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.leave_reason, lr.start_date, lr.end_date,
               lr.no_days, lr.status, lr.recommender_status, lr.approver_status,
               lr.reject_reason, lr.created_at, lr.upload_document,
               lr.original_doc_received, lr.original_doc_received_by,
               lr.original_doc_received_at,
               lr.is_exported, lr.exported_at, lr.exported_by
        FROM leave_request lr
        WHERE 1=1
    """
    params = []
 
    if status_filter:
        query += " AND lr.status = %s"
        params.append(status_filter)
 
    if department_filter:
        query += " AND lr.department = %s"
        params.append(department_filter)
 
    if search:
        query += " AND (lr.requester LIKE %s OR lr.requester_name LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
 
    if date_from:
        query += " AND lr.start_date >= %s"
        params.append(date_from)
 
    if date_to:
        query += " AND lr.end_date <= %s"
        params.append(date_to)
 
    query += " ORDER BY lr.created_at DESC"
 
    cursor.execute(query, params)
    all_leaves = cursor.fetchall()
 
    # Calculate pagination
    total_leaves = len(all_leaves)
    total_pages  = (total_leaves + per_page - 1) // per_page if total_leaves > 0 else 1
 
    if current_page < 1:
        current_page = 1
    elif current_page > total_pages and total_pages > 0:
        current_page = total_pages
 
    # Get paginated slice of leaves
    start_idx = (current_page - 1) * per_page
    end_idx = start_idx + per_page
    paginated_leaves = all_leaves[start_idx:end_idx]
 
    # Get departments for filter dropdown
    cursor.execute("""
        SELECT DISTINCT department FROM leave_request
        WHERE department IS NOT NULL ORDER BY department
    """)
    departments = [row['department'] for row in cursor.fetchall()]
 
    cursor.close()
    conn.close()
 
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
        per_page=per_page
    )

@app.route('/hr/leave/update/<int:leave_id>/<action>', methods=['POST'])
@login_required
@hr_required
def hr_leave_update(leave_id, action):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            "SELECT upload_document FROM leave_request WHERE leave_id = %s",
            (leave_id,)
        )
        leave = cursor.fetchone()

        if action == 'approve':
            cursor.execute("""
                UPDATE leave_request
                SET status = 'Approved',
                    recommender_status = 'Acknowledged',
                    approver_status = 'Approved',
                    status_update = NOW()
                WHERE leave_id = %s
                  AND status IN ('Pending', 'Acknowledged')
            """, (leave_id,))
            flash('Leave approved successfully!', 'success')

        elif action == 'reject':
            reason = request.form.get('reject_reason', '').strip()
            if not reason:
                flash('Rejection reason required', 'warning')
                return redirect(url_for('hr_leaves'))

            cursor.execute("""
                UPDATE leave_request
                SET status = 'Rejected',
                    approver_status = 'Rejected',
                    reject_reason = %s,
                    status_update = NOW()
                WHERE leave_id = %s
            """, (reason, leave_id))
            flash('Leave rejected successfully!', 'info')

        elif action == 'cancel':
            cursor.execute("""
                UPDATE leave_request
                SET status = 'Cancel',
                    status_update = NOW()
                WHERE leave_id = %s
            """, (leave_id,))

            # DELETE FILE ON CANCEL
            if leave and leave['upload_document']:
                file_path = os.path.join(
                    app.config['UPLOAD_FOLDER'],
                    leave['upload_document']
                )
                if os.path.exists(file_path):
                    os.remove(file_path)

            flash('Leave cancelled successfully!', 'warning')

        else:
            flash('Invalid action', 'danger')
            return redirect(url_for('hr_leaves'))

        conn.commit()

    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'danger')

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('hr_leaves'))

@app.route('/hr/leaves/export')
@login_required
@hr_required
def hr_leaves_export():
    import csv
    import io
    from datetime import datetime as dt
 
    status_filter = request.args.get('status', '')
    department_filter = request.args.get('department', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    exported_filter = request.args.get('exported', 'all')  # 'all' or '0'
 
    query = """
        SELECT lr.leave_id, lr.requester, lr.requester_name, lr.department,
               lr.leave_type, lr.leave_reason, lr.start_date, lr.end_date,
               lr.no_days, lr.status, lr.recommender_status, lr.approver_status,
               lr.reject_reason, lr.created_at, lr.upload_document,
               lr.original_doc_received, lr.original_doc_received_by,
               lr.original_doc_received_at,
               lr.is_exported, lr.exported_at, lr.exported_by
        FROM leave_request lr
        WHERE 1=1
    """
    params = []
 
    if status_filter:
        query += " AND lr.status = %s"
        params.append(status_filter)
 
    if department_filter:
        query += " AND lr.department = %s"
        params.append(department_filter)
 
    if search:
        query += " AND (lr.requester LIKE %s OR lr.requester_name LIKE %s)"
        params.extend([f'%{search}%', f'%{search}%'])
 
    if date_from:
        query += " AND lr.start_date >= %s"
        params.append(date_from)
 
    if date_to:
        query += " AND lr.end_date <= %s"
        params.append(date_to)
 
    # ── Only unexported rows when selected ──
    if exported_filter == '0':
        query += " AND lr.is_exported = 0"
    # ────────────────────────────────────────
 
    query += " ORDER BY lr.created_at DESC"
 
    leaves = query_db(query, params)
 
    # ── Abort early if nothing to export ──
    if not leaves:
        flash('No records found to export.', 'warning')
        return redirect(request.referrer or '/hr/leaves')
 
    # ── Stamp exported rows ──
    ids = [l['leave_id'] for l in leaves]
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            f"""
            UPDATE leave_request
            SET is_exported = 1,
                exported_at = NOW(),
                exported_by = %s
            WHERE leave_id IN ({','.join(['%s'] * len(ids))})
            """,
            [session.get('employee_name', 'HR')] + ids
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        app.logger.error(f"Export stamp failed: {e}")
 
    # ── Build CSV ──
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
            leave['leave_id'],
            leave['requester'],
            leave['requester_name'],
            leave['department'] or '',
            leave['leave_type'] or '',
            leave['leave_reason'] or '',
            leave['start_date'].strftime('%Y-%m-%d') if leave['start_date'] else '',
            leave['end_date'].strftime('%Y-%m-%d') if leave['end_date'] else '',
            leave['no_days'],
            leave['status'],
            leave['recommender_status'] or '',
            leave['approver_status'] or '',
            leave['reject_reason'] or '',
            leave['created_at'].strftime('%Y-%m-%d %H:%M') if leave['created_at'] else '',
            'Yes' if leave['upload_document'] else 'No',
            'Yes' if leave['original_doc_received'] else 'No',
            leave['original_doc_received_by'] or '',
            leave['original_doc_received_at'].strftime('%Y-%m-%d %H:%M') if leave['original_doc_received_at'] else ''
        ])
 
    output.seek(0)
 
    label    = 'unexported' if exported_filter == '0' else 'all'
    filename = f"leaves_{label}_{dt.now().strftime('%Y%m%d_%H%M%S')}.csv"
 
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/update_original_doc_status/<int:leave_id>', methods=['POST'])
@login_required
def update_original_doc_status(leave_id):
    employee_no = session.get('employee_no')
    roles = session.get('roles', [])
    
    # Only HR or Approver can update this
    if 'HR' not in roles and 'Approver' not in roles:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    data = request.get_json()
    received = data.get('received', False)
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # First check if the leave has an uploaded document
        cursor.execute(
            "SELECT upload_document FROM leave_request WHERE leave_id = %s",
            (leave_id,)
        )
        result = cursor.fetchone()
        
        if not result or not result[0]:
            return jsonify({'success': False, 'message': 'No document uploaded for this leave'}), 400
        
        if received:
            # Mark as received
            cursor.execute("""
                UPDATE leave_request 
                SET original_doc_received = 1,
                    original_doc_received_by = %s,
                    original_doc_received_at = NOW()
                WHERE leave_id = %s
            """, (employee_no, leave_id))
        else:
            # Unmark
            cursor.execute("""
                UPDATE leave_request 
                SET original_doc_received = 0,
                    original_doc_received_by = NULL,
                    original_doc_received_at = NULL
                WHERE leave_id = %s
            """, (leave_id,))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'Status updated successfully'})
    
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    
    finally:
        cursor.close()
        conn.close()

# HR route to manage public holidays
@app.route('/hr/holidays')
@login_required
@hr_required
def hr_holidays():
    search = request.args.get('search', '')
    year = request.args.get('year', datetime.now().year)
    
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    
    query = """
        SELECT ph.*, 
               COUNT(rl.id) as recommended_count
        FROM public_holidays ph
        LEFT JOIN recommended_leave rl ON ph.holiday_id = rl.holiday_id
        WHERE ph.status = 'Active'
        AND YEAR(ph.start_date) = %s
    """
    params = [year]
    
    if search:
        query += " AND ph.holiday_name LIKE %s"
        params.append(f'%{search}%')
    
    query += " GROUP BY ph.holiday_id ORDER BY ph.start_date ASC"
    
    cursor.execute(query, params)
    holidays = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return render_template(
        'hr_holidays.html',
        employee_name=session.get('employee_name', 'HR'),
        holidays=holidays,
        search=search,
        selected_year=year,
        current_year=datetime.now().year
    )

# Add/Edit public holiday
@app.route('/hr/holiday/<int:holiday_id>', methods=['GET', 'POST'])
@app.route('/hr/holiday/add', defaults={'holiday_id': None}, methods=['GET', 'POST'])
@login_required
@hr_required
def hr_holiday_form(holiday_id):
    holiday = None
    recommended_dates = []
    
    if holiday_id:
        holiday = query_db(
            "SELECT * FROM public_holidays WHERE holiday_id = %s",
            (holiday_id,), one=True
        )
        if not holiday:
            flash("Holiday not found!", "danger")
            return redirect(url_for("hr_holidays"))
        
        # Fetch existing recommended leave dates
        recommended_dates = query_db("""
            SELECT * FROM recommended_leave
            WHERE holiday_id = %s
            ORDER BY leave_date ASC
        """, (holiday_id,))
    
    if request.method == 'POST':
        holiday_name = request.form['holiday_name'].strip()
        start_date = request.form['start_date']
        end_date = request.form['end_date']
        description = request.form.get('description', '').strip()
        is_mandatory = request.form.get('is_mandatory') == 'on'
        auto_deduct = request.form.get('auto_deduct') == 'on'
        
        # Get recommended leave dates (arrays)
        recommended_dates_input = request.form.getlist('recommended_dates[]')
        recommended_priorities_input = request.form.getlist('recommended_priorities[]')
        
        if not holiday_name or not start_date or not end_date:
            flash("Please fill in all required fields", "danger")
            return redirect(request.url)
        
        conn = get_db()
        cursor = conn.cursor()
        
        try:
            if holiday_id:
                # Update holiday
                cursor.execute("""
                    UPDATE public_holidays
                    SET holiday_name=%s, start_date=%s, end_date=%s, 
                        description=%s, is_mandatory=%s, auto_deduct=%s,
                        updated_at=NOW()
                    WHERE holiday_id=%s
                """, (holiday_name, start_date, end_date, description, 
                      is_mandatory, auto_deduct, holiday_id))
                
                # Delete existing recommended dates
                cursor.execute("DELETE FROM recommended_leave WHERE holiday_id = %s", (holiday_id,))
                
                new_holiday_id = holiday_id
                flash("Holiday updated successfully", "success")
            else:
                # Insert new holiday
                cursor.execute("""
                    INSERT INTO public_holidays
                    (holiday_name, start_date, end_date, description, 
                     is_mandatory, auto_deduct, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (holiday_name, start_date, end_date, description, 
                      is_mandatory, auto_deduct, session.get('employee_no')))
                
                new_holiday_id = cursor.lastrowid
                flash("Holiday added successfully", "success")
            
            # Insert recommended leave dates
            for i, rec_date in enumerate(recommended_dates_input):
                if rec_date:  # Only insert if date is provided
                    rec_priority = recommended_priorities_input[i] if i < len(recommended_priorities_input) else 'AL_FIRST'
                    cursor.execute("""
                        INSERT INTO recommended_leave
                        (holiday_id, leave_date, leave_priority)
                        VALUES (%s, %s, %s)
                    """, (new_holiday_id, rec_date, rec_priority))
            
            conn.commit()

            if auto_deduct and recommended_dates_input:
                result = auto_create_leave_requests(new_holiday_id)

                if result['success']:
                    flash(
                        f"✓ Holiday saved! Created {result['created_requests']} leave requests "
                        f"for {result['total_employees']} employees across {result['total_dates']} dates. "
                        f"Skipped {result['skipped']} (already have leave).",
                        "success"
                    )
                    if result['errors']:
                        flash(f"⚠ Errors: {', '.join(result['errors'][:5])}", "warning")
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

# Delete public holiday
@app.route('/hr/holiday/delete/<int:holiday_id>', methods=['POST'])
@login_required
@hr_required
def hr_holiday_delete(holiday_id):
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE public_holidays 
            SET status = 'Cancel', updated_at = NOW()
            WHERE holiday_id = %s
        """, (holiday_id,))
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
        employee_no = session['employee_no']
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE auth_user 
                SET last_logout = NOW() 
                WHERE employee_no = %s
            """, (employee_no,))
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