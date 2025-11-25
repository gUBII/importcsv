import csv
import json
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional


FALLBACK_TEXT = "NotFoundInTurnpoint"
FALLBACK_NUMERIC = "0"
DEFAULT_PASSWORD = "Circle@2024"
DEFAULT_COUNTRY = "Australia"
DEFAULT_REGION = ", Sydney, NSW-SYD"


@dataclass
class WorkerRecord:
    path: Path
    worker_id: str
    full_name: str
    team: str
    data: Dict[str, str]


def _safe_text(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value else FALLBACK_TEXT


def _safe_numeric(value: Optional[str]) -> str:
    value = (value or "").strip()
    return value if value else FALLBACK_NUMERIC


def _parse_date(value: Optional[str]) -> str:
    """
    TurnPoint often provides formats like '24 Dec 1972' or '24/Dec/1972'.
    Nexis accepts DD/MMM/YYYY. If missing or unparsable, use today.
    """
    value = (value or "").strip()
    if not value:
        return datetime.now().strftime("%d/%b/%Y")
    candidates = [
        "%d %b %Y",
        "%d/%b/%Y",
        "%d-%b-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%d/%b/%Y")
        except Exception:
            continue
    return datetime.now().strftime("%d/%b/%Y")


def _gender(value: Optional[str]) -> str:
    token = (value or "").strip().lower()
    if token in ("female", "f", "2"):
        return "FEMALE"
    if token in ("male", "m", "1"):
        return "MALE"
    return "OTHER"


def _designation(is_admin: bool) -> str:
    return "Admin" if is_admin else "Employee"


def _account_type(is_admin: bool) -> str:
    return "ADMIN" if is_admin else "USER"


DEPARTMENT_MAP = {
    "admin": "Admin",
    "case managers": "Case Managers",
    "domestic workers": "Domestic Workers",
    "external / brokerage": "External/Brokerage",
    "external/brokerage": "External/Brokerage",
    "payroll/finance": "Payroll/Finance",
    "rn": "RN",
    "scheduling staff": "Scheduling Staff",
    "volunteers": "Volunteers",
    "support coordinator": "Support Coordinator",
    "personal care workers": "Personal Care Workers",
}


def _department(team: str) -> str:
    key = team.strip().lower()
    return DEPARTMENT_MAP.get(key, FALLBACK_TEXT)


def _schads_status(pay_group: str) -> str:
    token = (pay_group or "").lower()
    if "casual" in token:
        return "Casual"
    if "full" in token or "part" in token:
        return "Full-time/Part-time"
    return "Full-time/Part-time"


def _email(row: Dict[str, str], fallback_id: str) -> str:
    email = (row.get("Email") or "").strip()
    if email:
        return email
    phone = (row.get("Mobile") or row.get("Landline") or "").strip()
    if phone:
        return phone
    return f"noemail{fallback_id}@nomail.com"


def _job_experience(row: Dict[str, str]) -> str:
    raw = (row.get("Job Experience") or "").strip()
    return raw if raw.isdigit() else FALLBACK_NUMERIC


def load_worker_detail_csv(path: Path) -> Dict[str, str]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return next(reader)


def discover_workers(root: Path) -> List[WorkerRecord]:
    workers: List[WorkerRecord] = []
    for csv_path in root.glob("**/*WorkerDetail.csv"):
        try:
            data = load_worker_detail_csv(csv_path)
            worker_id = (data.get("Worker ID") or "").strip()
            full_name = (data.get("Full Name") or "").strip()
            team = (data.get("Team") or "").strip()
            workers.append(
                WorkerRecord(
                    path=csv_path,
                    worker_id=worker_id,
                    full_name=full_name,
                    team=team,
                    data=data,
                )
            )
        except Exception:
            continue
    workers.sort(key=lambda w: (w.team.lower(), w.full_name.lower()))
    return workers


def build_nexis_employee(record: WorkerRecord, is_admin: bool = False) -> Dict[str, str]:
    row = record.data
    worker_id = record.worker_id or record.path.stem.split()[0]
    today_str = datetime.now().strftime("%d/%b/%Y")

    posting_date = _parse_date(row.get("Employment Start Date")) if row.get("Employment Start Date") else today_str
    joining_date = posting_date

    email = _email(row, worker_id or "00000")
    phone = (row.get("Mobile") or row.get("Landline") or "").strip() or FALLBACK_TEXT
    dob = _parse_date(row.get("Date of Birth"))

    acct_ref = row.get("Accounting System Reference") or ""
    acct_ref = acct_ref if acct_ref and acct_ref.lower() not in ("n/a", "na") else ""

    team = row.get("Team") or ""

    payload = {
        "PostingDate": posting_date,
        "JoiningDate": joining_date,
        "Title": _safe_text(row.get("Title")),
        "FirstName": _safe_text(row.get("First Name")),
        "LastName": _safe_text(row.get("Surname")),
        "Phone": phone,
        "DOB": dob,
        "Email": email,
        "Password": DEFAULT_PASSWORD,
        "Gender": _gender(row.get("Gender")),
        "Address": _safe_text(row.get("Address")),
        "Address2": row.get("Address 2", "").strip(),
        "City": _safe_text(row.get("Suburb")),
        "State": _safe_text(row.get("State")),
        "Zip": _safe_text(row.get("Postcode")),
        "Country": DEFAULT_COUNTRY,
        # Official detail
        "EmployeeCardID": acct_ref if acct_ref else FALLBACK_NUMERIC,
        "Designation": _designation(is_admin),
        "Department": _department(team),
        "JobExperience": _job_experience(row),
        "AccountType": _account_type(is_admin),
        "Region": row.get("Geographic Region") or DEFAULT_REGION,
        # HR accounts
        "SCHADSStatus": _schads_status(row.get("Pay Group") or ""),
        "SCHADSLevel": row.get("Pay Level") or FALLBACK_TEXT,
        "PayPoint": "",  # optional
        "WageBasic": FALLBACK_TEXT,
        "ABN": _safe_numeric(row.get("ABN / Contractor Number")),
        "PaymentType": "Bank Transfer",
    }
    return payload


def preview_payload(record: WorkerRecord, is_admin: bool = False) -> str:
    return json.dumps(build_nexis_employee(record, is_admin=is_admin), indent=2)
