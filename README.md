# Leave Management System

A Flask-based web application for managing employee leave requests, built for internal HR use. The system supports a multi-tier approval workflow with Sub-verifiers, Verifiers, and an Approver, along with HR admin tools for user and holiday management.

---

## Features

- **Multi-role approval workflow** — Sub-verifier → Verifier → Approver
- **Leave application** — Apply for yourself or on behalf of a subordinate
- **Document upload** — Attach supporting documents (PDF/image) per leave request
- **Email notifications** — Automated alerts to the next approver in the chain
- **Scheduled digest** — Daily email reminders to the Approver for pending requests
- **Backup approver support** — Pre-approval flow when the primary approver is on leave
- **HR admin panel** — Manage employees, users, roles, verifier groups, and public holidays
- **Calendar view** — Department-wide leave and public holiday visibility
- **ICS export** — Subscribe to leave calendar via `.ics` feed
- **CSV export** — Export leave records for payroll or reporting
- **Auto leave creation** — Automatically generate leave entries for company-advised holiday dates

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| Database | MySQL (`mysql-connector-python`) |
| Frontend | HTML, Bootstrap, JavaScript |
| Email | SMTP via `smtplib` |
| Scheduler | APScheduler |
| Image processing | Pillow |
| File handling | Werkzeug |

---

### 3. Set up the database

Create a MySQL database named `hr_database` and import the required schema. The application expects these tables:

- `employee` — employee records
- `auth_user` — user accounts and roles
- `leave_request` — leave submissions
- `leave_documents` — uploaded attachments
- `verifier_group` — verifier/sub-verifier assignments
- `public_holidays` — company holidays
- `recommended_leave` — company-advised leave dates
- `section` — department sections

---

## User Roles

| Role | Permissions |
|---|---|
| Employee | Apply leave, view own leaves, apply on behalf of same-dept teammates |
| Sub-verifier | All Employee permissions + acknowledge/reject subordinate leaves |
| Verifier | All Sub-verifier permissions + second-level acknowledgement |
| Approver | Approve or reject acknowledged leave requests |
| HR / Admin | Full access to employee management, leave records, holidays, and exports |

---

## Approval Workflow

```
Employee applies
      ↓
Sub-verifier acknowledges (V1)   ← skipped if not assigned
      ↓
Verifier acknowledges (V2)       ← skipped if not assigned
      ↓
Approver approves / rejects
```

If the primary approver is on leave, backup approvers can pre-approve requests. The primary approver gives final approval upon return.

---

## File Upload Limits

| Setting | Value |
|---|---|
| Max files per request | 3 |
| Max PDF file size | 1 MB |
| Max image dimension | 1920 px (auto-resized) |
| Image quality | 85% JPEG (auto-compressed) |
| Max total upload size | 3 MB |

---

## Scheduled Jobs

| Job | Schedule | Description |
|---|---|---|
| Pending approval digest | Mon–Fri, 8:00 AM & 2:00 PM | Emails Approver a summary of all pending leave requests |

---

## Key Routes

| Route | Access | Description |
|---|---|---|
| `/` | Public | Redirects to calendar or dashboard |
| `/calendar` | Public | Leave calendar view |
| `/login` | Public | Login page |
| `/dashboard` | Employee+ | Personal leave history and upcoming leaves |
| `/apply_leave` | Employee+ | Submit a leave request |
| `/view_leave` | Verifier / Approver | Manage pending leave requests |
| `/employee_history` | Verifier / Approver | View subordinate leave history |
| `/profile` | Employee+ | Leave balance and personal info |
| `/hr/dashboard` | HR / Admin | HR overview and stats |
| `/hr/users` | HR / Admin | Employee and user management |
| `/hr/leaves` | HR / Admin | All leave records with export |
| `/hr/holidays` | HR / Admin | Public holiday management |
| `/hr/verifier_groups` | HR / Admin | Verifier group assignments |
| `/calendar.ics` | Public | ICS calendar feed |

---

## License

Nur Athirah
