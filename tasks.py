import streamlit as st
import json
import os
import random
import string
from datetime import datetime, date, time, timedelta

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    _GCAL_PKGS = True
except ImportError:
    _GCAL_PKGS = False

st.set_page_config(page_title="Personal Tasks", page_icon=None, layout="wide")

# ── Constants ─────────────────────────────────────────────────────────────────

PRIORITY_STYLES = {
    "Low":    {"bg": "#eff6ff", "color": "#2563eb", "dot": "#3b82f6"},
    "Medium": {"bg": "#fff7ed", "color": "#c2410c", "dot": "#f97316"},
    "High":   {"bg": "#fdf4ff", "color": "#7e22ce", "dot": "#a855f7"},
    "Urgent": {"bg": "#fef2f2", "color": "#b91c1c", "dot": "#ef4444"},
}
STATUS_STYLES = {
    "Pending":     {"bg": "#fdf2f8", "color": "#9d174d", "dot": "#ec4899"},
    "In Progress": {"bg": "#fff7ed", "color": "#c2410c", "dot": "#f97316"},
    "Completed":   {"bg": "#f0fdf4", "color": "#15803d", "dot": "#22c55e"},
    "Archived":    {"bg": "#f8fafc", "color": "#94a3b8", "dot": "#cbd5e1"},
}

DEFAULT_TABS = ["Personal", "Work", "Home"]
STORAGE_FILE = "tasks_data.json"
SCOPES       = ["https://www.googleapis.com/auth/calendar.events"]

# ── Google Calendar helpers ───────────────────────────────────────────────────

def has_gcal_secrets() -> bool:
    try:
        return bool(st.secrets.get("gcp_service_account", {}).get("client_email"))
    except Exception:
        return False


def _get_service():
    if not _GCAL_PKGS:
        return None
    try:
        info  = dict(st.secrets["gcp_service_account"])
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return build("calendar", "v3", credentials=creds)
    except Exception:
        return None


def _calendar_id() -> str:
    try:
        return st.secrets["gcp_service_account"].get("calendar_id", "primary")
    except Exception:
        return "primary"


def _timezone() -> str:
    try:
        return st.secrets["gcp_service_account"].get("timezone", "UTC")
    except Exception:
        return "UTC"


def gcal_connected() -> bool:
    return has_gcal_secrets() and _get_service() is not None


def _event_body(job: dict) -> dict:
    parts = [f"Priority: {job['priority']}", f"Status: {job['status']}", f"List: {job['location']}"]
    if job.get("notes"):
        parts.append(f"Notes: {job['notes']}")
    if job.get("description"):
        parts.append(job["description"])
    desc = "\n".join(parts)
    tz   = _timezone()

    if job.get("dueTime"):
        start_str = f"{job['dueDate']}T{job['dueTime']}:00"
        start_dt  = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S")
        end_str   = (start_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "summary":     job["title"],
            "description": desc,
            "start":       {"dateTime": start_str, "timeZone": tz},
            "end":         {"dateTime": end_str,   "timeZone": tz},
        }
    return {
        "summary":     job["title"],
        "description": desc,
        "start":       {"date": job["dueDate"]},
        "end":         {"date": job["dueDate"]},
    }


def create_gcal_event(job: dict) -> str | None:
    svc = _get_service()
    if not svc or not job.get("dueDate"):
        return None
    try:
        result = svc.events().insert(calendarId=_calendar_id(), body=_event_body(job)).execute()
        return result.get("id")
    except Exception as e:
        st.session_state["gcal_error"] = f"Calendar error: {e}"
        return None


def update_gcal_event(event_id: str, job: dict):
    svc = _get_service()
    if not svc or not event_id:
        return
    try:
        svc.events().update(calendarId=_calendar_id(), eventId=event_id, body=_event_body(job)).execute()
    except Exception as e:
        st.session_state["gcal_error"] = f"Calendar update failed: {e}"


def delete_gcal_event(event_id: str):
    svc = _get_service()
    if not svc or not event_id:
        return
    try:
        svc.events().delete(calendarId=_calendar_id(), eventId=event_id).execute()
    except Exception:
        pass


def sync_from_calendar(target_list: str) -> int:
    """Pull calendar events not already in the app and create tasks from them."""
    svc = _get_service()
    if not svc:
        return 0
    try:
        past   = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
        future = (datetime.utcnow() + timedelta(days=180)).isoformat() + "Z"
        result = svc.events().list(
            calendarId=_calendar_id(),
            timeMin=past, timeMax=future,
            maxResults=200, singleEvents=True, orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        existing_ids = {j.get("calEventId") for j in st.session_state.jobs if j.get("calEventId")}
        added = 0

        for ev in events:
            if ev["id"] in existing_ids:
                continue
            if ev.get("recurringEventId"):   # skip auto-rolling/recurring events
                continue
            start = ev.get("start", {})
            if "dateTime" in start:
                dt       = datetime.fromisoformat(start["dateTime"])
                due_date = dt.strftime("%Y-%m-%d")
                due_time = dt.strftime("%H:%M")
            elif "date" in start:
                due_date = start["date"]
                due_time = ""
            else:
                continue

            new_job = {
                "id":          gen_id(),
                "title":       ev.get("summary", "Untitled"),
                "description": ev.get("description", ""),
                "notes":       "",
                "priority":    "Medium",
                "status":      "Pending",
                "location":    target_list,
                "dueDate":     due_date,
                "dueTime":     due_time,
                "calEventId":  ev["id"],
                "createdAt":   now_ms(),
                "completedAt": None,
            }
            st.session_state.jobs.append(new_job)
            existing_ids.add(ev["id"])
            added += 1

        if added:
            save_data()
        return added
    except Exception as e:
        st.session_state["gcal_error"] = f"Calendar sync failed: {e}"
        return 0

# ── Data layer ────────────────────────────────────────────────────────────────

def now_ms() -> float:
    return datetime.now().timestamp() * 1000

def gen_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))

def load_data() -> tuple:
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE) as f:
                d = json.load(f)
            jobs = d.get("jobs", [])
            for j in jobs:
                j.setdefault("calEventId", None)
                j.setdefault("dueTime", "")
            return jobs, d.get("tabs", list(DEFAULT_TABS)), d.get("archived_tabs", [])
        except Exception:
            pass
    return [], list(DEFAULT_TABS), []

def save_data():
    with open(STORAGE_FILE, "w") as f:
        json.dump({
            "jobs":          st.session_state.jobs,
            "tabs":          st.session_state.tabs_list,
            "archived_tabs": st.session_state.archived_tabs,
        }, f, indent=2)

# ── Mutations ─────────────────────────────────────────────────────────────────

def auto_archive():
    ts    = now_ms()
    dirty = False
    for j in st.session_state.jobs:
        if j["status"] == "Completed" and j.get("completedAt") and ts - j["completedAt"] >= 48 * 3_600_000:
            j["status"] = "Archived"
            dirty = True
    if dirty:
        save_data()


def set_status(job_id: str, status: str):
    for j in st.session_state.jobs:
        if j["id"] == job_id:
            j["status"] = status
            if status == "Completed":
                j["completedAt"] = now_ms()
            break
    save_data()


def remove_job(job_id: str):
    job = next((j for j in st.session_state.jobs if j["id"] == job_id), None)
    if job and job.get("calEventId"):
        delete_gcal_event(job["calEventId"])
    st.session_state.jobs = [j for j in st.session_state.jobs if j["id"] != job_id]
    save_data()


def restore_job(job_id: str):
    for j in st.session_state.jobs:
        if j["id"] == job_id:
            j["status"]      = "Pending"
            j["completedAt"] = None
            break
    save_data()


def upsert_job(data: dict, job_id: str | None = None):
    is_new  = job_id is None
    old_job = None

    if job_id:
        for i, j in enumerate(st.session_state.jobs):
            if j["id"] == job_id:
                old_job = dict(j)
                st.session_state.jobs[i] = {**j, **data}
                saved = st.session_state.jobs[i]
                break
    else:
        new_job = {**data, "id": gen_id(), "createdAt": now_ms(), "completedAt": None, "calEventId": None, "dueTime": data.get("dueTime", "")}
        st.session_state.jobs.append(new_job)
        saved = new_job

    # ── Calendar sync ──────────────────────────────────────────────────────────
    new_due      = data.get("dueDate", "")
    old_due      = (old_job or {}).get("dueDate", "")
    old_event_id = (old_job or {}).get("calEventId")

    new_time = data.get("dueTime", "")
    old_time = (old_job or {}).get("dueTime", "")

    if is_new:
        if new_due:
            eid = create_gcal_event(saved)
            if eid:
                saved["calEventId"] = eid
                label = f"{new_due} {new_time}" if new_time else new_due
                st.session_state["gcal_toast"] = f"Added to Google Calendar ({label})"
    else:
        if new_due and old_event_id:
            changed = (new_due != old_due or new_time != old_time or
                       data.get("title") != old_job.get("title"))
            if changed:
                update_gcal_event(old_event_id, saved)
                st.session_state["gcal_toast"] = "Calendar event updated"
        elif new_due and not old_event_id:
            eid = create_gcal_event(saved)
            if eid:
                saved["calEventId"] = eid
                st.session_state["gcal_toast"] = f"Added to Google Calendar ({new_due})"
        elif not new_due and old_event_id:
            delete_gcal_event(old_event_id)
            saved["calEventId"] = None
            st.session_state["gcal_toast"] = "Calendar event removed"

    save_data()

# ── Dialog ────────────────────────────────────────────────────────────────────

@st.dialog("Task Details", width="large")
def task_dialog():
    job_id = st.session_state.get("dlg_job_id")
    job    = next((j for j in st.session_state.jobs if j["id"] == job_id), None) if job_id else None

    title       = st.text_input("Title *", value=job["title"] if job else "")
    description = st.text_area("Description", value=job.get("description", "") if job else "", height=70)

    PRIORITIES = ["Low", "Medium", "High", "Urgent"]
    STATUSES   = ["Pending", "In Progress", "Completed"]

    c1, c2 = st.columns(2)
    with c1:
        cur_p    = job["priority"] if job else "Medium"
        priority = st.selectbox("Priority", PRIORITIES, index=PRIORITIES.index(cur_p))
    with c2:
        cur_s  = job["status"] if job and job["status"] in STATUSES else "Pending"
        status = st.selectbox("Status", STATUSES, index=STATUSES.index(cur_s))

    all_tabs = st.session_state.tabs_list
    if not job:
        default_loc = st.session_state.get("dlg_location", all_tabs[0])
        loc_idx     = all_tabs.index(default_loc) if default_loc in all_tabs else 0
    else:
        cur_loc = job["location"] if job["location"] in all_tabs else all_tabs[0]
        loc_idx = all_tabs.index(cur_loc)
    location = st.selectbox("List", all_tabs, index=loc_idx)

    dc1, dc2 = st.columns([3, 2])
    with dc1:
        try:
            due_val = date.fromisoformat(job["dueDate"]) if job and job.get("dueDate") else None
        except ValueError:
            due_val = None
        due_date = st.date_input("Due Date", value=due_val)
    with dc2:
        existing_time = job.get("dueTime", "") if job else ""
        use_time = st.checkbox("Set time", value=bool(existing_time), key="dlg_use_time")
        if use_time:
            try:
                t_val = datetime.strptime(existing_time, "%H:%M").time() if existing_time else time(9, 0)
            except ValueError:
                t_val = time(9, 0)
            due_time_val = st.time_input("Time", value=t_val, label_visibility="collapsed")
            due_time = due_time_val.strftime("%H:%M")
        else:
            due_time = ""

    notes = st.text_area("Notes", value=job.get("notes", "") if job else "", height=70)

    # Calendar status hint
    if job and job.get("calEventId"):
        st.caption("📅 Synced to Google Calendar — changes update the event automatically")
    elif due_date and gcal_connected():
        st.caption("📅 Will be added to Google Calendar on save")
    elif due_date and not gcal_connected():
        st.caption("Connect Google Calendar in the header to sync this due date")

    sc, cc = st.columns(2)
    with sc:
        save   = st.button("Save", type="primary", use_container_width=True)
    with cc:
        cancel = st.button("Cancel", use_container_width=True)

    if save:
        if not title.strip():
            st.error("Title is required.")
            return
        upsert_job(
            {
                "title":       title.strip(),
                "description": description,
                "priority":    priority,
                "status":      status,
                "location":    location,
                "dueDate":     str(due_date) if due_date else "",
                "dueTime":     due_time,
                "notes":       notes,
            },
            job_id=job_id,
        )
        st.session_state.dlg_open   = False
        st.session_state.dlg_job_id = None
        st.rerun()

    if cancel:
        st.session_state.dlg_open   = False
        st.session_state.dlg_job_id = None
        st.rerun()

# ── Card helpers ──────────────────────────────────────────────────────────────

def badge(text: str, style: dict) -> str:
    return (
        f'<span style="background:{style["bg"]};color:{style["color"]};'
        f'padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;'
        f'letter-spacing:0.03em;display:inline-block;margin:2px;">{text.upper()}</span>'
    )


def render_active_card(job: dict, lk: str):
    with st.container(border=True):
        tc, ec, dc = st.columns([5, 1, 1])
        with tc:
            st.markdown(
                f"<p style='margin:0;font-size:13px;font-weight:600;line-height:1.3;color:#0f172a;'>{job['title']}</p>",
                unsafe_allow_html=True,
            )
        with ec:
            if st.button("✏", key=f"e_{lk}_{job['id']}", help="Edit", use_container_width=True):
                st.session_state.dlg_job_id = job["id"]
                st.session_state.dlg_open   = True
                st.rerun()
        with dc:
            if st.button("🗑", key=f"d_{lk}_{job['id']}", help="Delete", use_container_width=True):
                remove_job(job["id"])
                st.rerun()

        meta = []
        if job.get("dueDate"):
            cal_badge  = " 📅" if job.get("calEventId") else ""
            time_label = f" {job['dueTime']}" if job.get("dueTime") else ""
            meta.append(f"Due {job['dueDate']}{time_label}{cal_badge}")
        if job["status"] == "Completed" and job.get("completedAt"):
            ms_left = job["completedAt"] + 48 * 3_600_000 - now_ms()
            if ms_left > 0:
                h = int(ms_left / 3_600_000)
                m = int((ms_left % 3_600_000) / 60_000)
                meta.append(f"Archives in {h}h {m}m")

        meta_html  = f"<p style='font-size:11px;color:#94a3b8;margin:1px 0 0;'>{'  ·  '.join(meta)}</p>" if meta else ""
        notes_html = f"<p style='font-size:11px;color:#374151;margin:1px 0 0;'>{job['notes']}</p>" if job.get("notes") else ""
        desc_html  = f"<p style='font-size:11px;color:#374151;margin:1px 0 0;'>{job['description']}</p>" if job.get("description") else ""

        st.markdown(
            badge(job["priority"], PRIORITY_STYLES[job["priority"]]) + " " +
            badge(job["status"],   STATUS_STYLES[job["status"]]) +
            meta_html + notes_html + desc_html,
            unsafe_allow_html=True,
        )

        if job["status"] != "Completed":
            opts = ["Pending", "In Progress", "Completed"]
            cur  = opts.index(job["status"]) if job["status"] in opts else 0
            sel  = st.selectbox(
                "Status", opts, index=cur,
                key=f"s_{lk}_{job['id']}", label_visibility="collapsed",
            )
            if sel != job["status"]:
                set_status(job["id"], sel)
                st.rerun()


def render_archived_card(job: dict, lk: str):
    with st.container(border=True):
        tc, rc = st.columns([4, 1])
        with tc:
            st.markdown(f"**{job['title']}**")
        with rc:
            if st.button("Restore", key=f"r_{lk}_{job['id']}", help="Restore to Pending", use_container_width=True):
                restore_job(job["id"])
                st.rerun()
        st.markdown(
            badge(job["priority"], PRIORITY_STYLES[job["priority"]]) + " " +
            badge("Archived", STATUS_STYLES["Archived"]),
            unsafe_allow_html=True,
        )
        meta = []
        if job.get("dueDate"):
            meta.append(f"Due {job['dueDate']}")
        if job.get("notes"):
            meta.append(f"Note: {job['notes']}")
        if meta:
            st.caption("  ·  ".join(meta))


def render_kanban(location: str, view: str, search: str, filter_priority: str, filter_status: str):
    lk = location.replace(" ", "_").lower()
    q  = search.strip().lower()

    def matches(j):
        return (
            (not q or q in j["title"].lower() or q in (j.get("notes") or "").lower()) and
            (filter_priority == "All" or j["priority"] == filter_priority) and
            (filter_status   == "All" or j["status"]   == filter_status)
        )

    tab_jobs = [j for j in st.session_state.jobs if j["location"] == location]

    if view == "Active":
        pool = [j for j in tab_jobs if j["status"] != "Archived" and matches(j)]
        cols = st.columns(3)
        for i, col_name in enumerate(["Pending", "In Progress", "Completed"]):
            group = [j for j in pool if j["status"] == col_name]
            with cols[i]:
                st.markdown(f"#### {col_name} &nbsp; `{len(group)}`")
                if not group:
                    st.caption("No tasks")
                for job in group:
                    render_active_card(job, lk)
    else:
        pool = [j for j in tab_jobs if j["status"] == "Archived" and matches(j)]
        st.markdown(f"#### Archived &nbsp; `{len(pool)}`")
        if not pool:
            st.caption("No archived tasks")
        else:
            cols = st.columns(3)
            for idx, job in enumerate(pool):
                with cols[idx % 3]:
                    render_archived_card(job, lk)

# ── Session state ─────────────────────────────────────────────────────────────

if "initialized" not in st.session_state:
    jobs, tabs, archived_tabs = load_data()
    st.session_state.update({
        "jobs":          jobs,
        "tabs_list":     tabs,
        "archived_tabs": archived_tabs,
        "dlg_open":      False,
        "dlg_job_id":    None,
        "dlg_location":  tabs[0] if tabs else DEFAULT_TABS[0],
        "initialized":   True,
    })

auto_archive()

# ── Deferred toasts ───────────────────────────────────────────────────────────

if "gcal_toast" in st.session_state:
    st.toast(st.session_state.pop("gcal_toast"))
if "gcal_error" in st.session_state:
    st.error(st.session_state.pop("gcal_error"))

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif !important; }
.block-container { padding-top: 5rem !important; max-width: 1400px !important; }
#MainMenu, footer, header { visibility: hidden; }

.app-header { display: flex; align-items: center; gap: 12px; margin-bottom: 0; }
.app-title  { font-family: 'DM Sans', sans-serif; font-size: 20px; font-weight: 600; color: #0f172a; letter-spacing: -0.02em; margin: 0; }
.app-subtitle { font-size: 13px; color: #94a3b8; font-weight: 400; }

hr { border-color: #e2e8f0 !important; margin: 0.75rem 0 !important; }

.stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid #e2e8f0; background: transparent; }
.stTabs [data-baseweb="tab"] {
    font-family: 'DM Sans', sans-serif !important; font-size: 13px !important; font-weight: 500 !important;
    color: #64748b !important; padding: 10px 18px !important; border-radius: 0 !important;
    border-bottom: 2px solid transparent !important;
}
.stTabs [aria-selected="true"] { color: #0f172a !important; border-bottom: 2px solid #0f172a !important; background: transparent !important; }
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"]    { display: none !important; }

div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 2px !important; border-color: #e2e8f0 !important;
    padding: 0px !important; margin-bottom: 4px !important;
}
div[data-testid="stVerticalBlockBorderWrapper"] > div { padding: 4px 8px !important; }
div[data-testid="stVerticalBlockBorderWrapper"] p { margin: 0 !important; line-height: 1.3 !important; }
div[data-testid="stVerticalBlockBorderWrapper"] .stSelectbox > div { min-height: 28px !important; }
div[data-testid="stVerticalBlockBorderWrapper"] .stSelectbox [data-baseweb="select"] > div {
    padding-top: 2px !important; padding-bottom: 2px !important; min-height: 28px !important; font-size: 11px !important;
}

.stButton button {
    font-family: 'DM Sans', sans-serif !important; font-size: 12px !important; font-weight: 500 !important;
    border-radius: 3px !important; letter-spacing: 0.01em !important;
    padding: 2px 6px !important; height: 28px !important; min-height: 28px !important;
}
.stButton button[kind="primary"]          { background: #0f172a !important; border: none !important; color: white !important; }
.stButton button[kind="primary"]:hover    { background: #1e293b !important; }
.stButton button[kind="secondary"]        { background: #f8fafc !important; border: 1px solid #e2e8f0 !important; color: #64748b !important; filter: grayscale(100%) !important; }
.stButton button[kind="secondary"]:hover  { background: #f1f5f9 !important; color: #374151 !important; }

.stTextInput input, .stTextArea textarea, .stSelectbox select {
    font-family: 'DM Sans', sans-serif !important; font-size: 13px !important;
    border-radius: 5px !important; border-color: #e2e8f0 !important;
}
.stSelectbox [data-baseweb="select"] { border-radius: 5px !important; }

h4 { font-size: 13px !important; font-weight: 600 !important; color: #475569 !important; text-transform: uppercase !important; letter-spacing: 0.06em !important; }
.stCaptionContainer, [data-testid="stCaptionContainer"] { color: #94a3b8 !important; font-size: 12px !important; }
[data-testid="stToast"] { font-family: 'DM Sans', sans-serif !important; font-size: 13px !important; }
.stRadio label { font-family: 'DM Sans', sans-serif !important; font-size: 13px !important; font-weight: 500 !important; }
code { font-family: 'DM Mono', monospace !important; font-size: 11px !important; background: #f1f5f9 !important; color: #475569 !important; border-radius: 4px !important; padding: 1px 6px !important; }

.gcal-status { font-size: 12px; font-weight: 500; color: #15803d; margin-top: 8px; }
.gcal-warn   { font-size: 12px; color: #94a3b8; margin-top: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────

hc1, hc2, hc3, hc4, hc5, hc6 = st.columns([2, 2.8, 1.2, 1.2, 1.1, 1.3])
with hc1:
    st.markdown("""
    <div class="app-header">
        <div>
            <div class="app-title">Personal Tasks</div>
            <div class="app-subtitle">Task Manager</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
with hc2:
    search = st.text_input("Search", placeholder="Search tasks or notes", label_visibility="collapsed")
with hc3:
    filter_priority = st.selectbox("Priority", ["All", "Low", "Medium", "High", "Urgent"], label_visibility="collapsed")
with hc4:
    filter_status = st.selectbox("Status", ["All", "Pending", "In Progress", "Completed"], label_visibility="collapsed")
with hc5:
    if not _GCAL_PKGS:
        st.markdown("<p class='gcal-warn'>⚠ Reboot app</p>", unsafe_allow_html=True)
    elif not has_gcal_secrets():
        st.markdown("<p class='gcal-warn'>⚠ Add secrets</p>", unsafe_allow_html=True)
    elif gcal_connected():
        st.markdown("<p class='gcal-status'>📅 Connected</p>", unsafe_allow_html=True)
    else:
        st.markdown("<p class='gcal-warn'>⚠ Invalid creds</p>", unsafe_allow_html=True)
with hc6:
    if gcal_connected():
        sync_list = st.session_state.tabs_list[0] if st.session_state.tabs_list else "Personal"
        if st.button("⬇ Sync Calendar", use_container_width=True, help="Import new calendar events as tasks"):
            n = sync_from_calendar(sync_list)
            if n:
                st.session_state["gcal_toast"] = f"Imported {n} new task{'s' if n != 1 else ''} from Calendar"
            else:
                st.session_state["gcal_toast"] = "Calendar is up to date"
            st.rerun()

st.markdown("---")

# ── Location tabs ─────────────────────────────────────────────────────────────

tab_labels = []
for loc in st.session_state.tabs_list:
    n = sum(1 for j in st.session_state.jobs if j["location"] == loc and j["status"] != "Archived")
    tab_labels.append(f"{loc}  ({n})")
tab_labels.append("+ New List")
archived_count = len(st.session_state.get("archived_tabs", []))
tab_labels.append(f"Archived Lists  ({archived_count})" if archived_count else "Archived Lists")

loc_tabs = st.tabs(tab_labels)

for i, tab_ctx in enumerate(loc_tabs[:-2]):
    with tab_ctx:
        loc = st.session_state.tabs_list[i]

        vc, _, ac, arc, dc = st.columns([2, 2.5, 1.5, 1.5, 1.5])
        with vc:
            view = st.radio(
                "View", ["Active", "Archive"],
                horizontal=True, key=f"view_{loc}", label_visibility="collapsed",
            )
        with ac:
            if st.button("+ Add Task", key=f"add_{loc}", type="primary", use_container_width=True):
                st.session_state.dlg_job_id   = None
                st.session_state.dlg_location = loc
                st.session_state.dlg_open     = True
                st.rerun()
        with arc:
            if st.button("Archive List", key=f"arc_tab_{loc}", use_container_width=True):
                for j in st.session_state.jobs:
                    if j["location"] == loc:
                        j["status"] = "Archived"
                st.session_state.tabs_list = [t for t in st.session_state.tabs_list if t != loc]
                st.session_state.archived_tabs.append(loc)
                save_data()
                st.rerun()
        with dc:
            if st.button("Delete List", key=f"del_tab_{loc}", use_container_width=True):
                # Delete calendar events for all jobs in this list
                for j in st.session_state.jobs:
                    if j["location"] == loc and j.get("calEventId"):
                        delete_gcal_event(j["calEventId"])
                st.session_state.tabs_list = [t for t in st.session_state.tabs_list if t != loc]
                st.session_state.jobs      = [j for j in st.session_state.jobs if j["location"] != loc]
                save_data()
                st.rerun()

        with st.expander("List Settings"):
            tabs_list = st.session_state.tabs_list
            idx       = tabs_list.index(loc)

            st.markdown("<p style='font-size:12px;font-weight:600;color:#475569;margin-bottom:2px;'>Rename List</p>", unsafe_allow_html=True)
            rc1, rc2 = st.columns([3, 1])
            with rc1:
                new_name_val = st.text_input("New name", value=loc, key=f"rename_{loc}", label_visibility="collapsed", placeholder="New list name")
            with rc2:
                if st.button("Save", key=f"rename_save_{loc}", type="primary", use_container_width=True):
                    n = new_name_val.strip()
                    if not n:
                        st.warning("Name cannot be empty.")
                    elif n != loc and n in tabs_list:
                        st.warning(f"'{n}' already exists.")
                    elif n != loc:
                        st.session_state.tabs_list[idx] = n
                        for j in st.session_state.jobs:
                            if j["location"] == loc:
                                j["location"] = n
                        save_data()
                        st.rerun()

            st.markdown("<p style='font-size:12px;font-weight:600;color:#475569;margin:8px 0 2px;'>Reorder List</p>", unsafe_allow_html=True)
            oc1, oc2, oc3 = st.columns([1, 1, 4])
            with oc1:
                if st.button("← Left", key=f"move_left_{loc}", use_container_width=True, disabled=(idx == 0)):
                    tabs_list[idx], tabs_list[idx - 1] = tabs_list[idx - 1], tabs_list[idx]
                    save_data()
                    st.rerun()
            with oc2:
                if st.button("Right →", key=f"move_right_{loc}", use_container_width=True, disabled=(idx == len(tabs_list) - 1)):
                    tabs_list[idx], tabs_list[idx + 1] = tabs_list[idx + 1], tabs_list[idx]
                    save_data()
                    st.rerun()
            with oc3:
                st.markdown(
                    f"<p style='font-size:11px;color:#94a3b8;margin:6px 0 0;'>Position {idx + 1} of {len(tabs_list)}</p>",
                    unsafe_allow_html=True,
                )

        st.divider()
        render_kanban(loc, view, search, filter_priority, filter_status)

# ── New List ──────────────────────────────────────────────────────────────────

with loc_tabs[-2]:
    st.markdown("### Add a new list")
    new_name = st.text_input("List name", placeholder="e.g. Errands", label_visibility="collapsed", key="new_tab_input")
    if st.button("Create List", type="primary"):
        n = new_name.strip()
        if not n:
            st.warning("Enter a list name.")
        elif n in st.session_state.tabs_list:
            st.warning(f"'{n}' already exists.")
        else:
            st.session_state.tabs_list.append(n)
            save_data()
            st.rerun()

# ── Archived Lists ────────────────────────────────────────────────────────────

with loc_tabs[-1]:
    st.markdown("### Archived Lists")
    archived = st.session_state.get("archived_tabs", [])
    if not archived:
        st.caption("No archived lists.")
    else:
        for atab in archived:
            job_count = sum(1 for j in st.session_state.jobs if j["location"] == atab)
            ac1, ac2, ac3 = st.columns([4, 1.5, 1.5])
            with ac1:
                st.markdown(f"**{atab}** &nbsp; <span style='font-size:12px;color:#94a3b8;'>{job_count} tasks</span>", unsafe_allow_html=True)
            with ac2:
                if st.button("Restore", key=f"restore_tab_{atab}", type="primary", use_container_width=True):
                    st.session_state.archived_tabs = [t for t in st.session_state.archived_tabs if t != atab]
                    st.session_state.tabs_list.append(atab)
                    save_data()
                    st.rerun()
            with ac3:
                if st.button("Delete", key=f"perm_del_tab_{atab}", use_container_width=True):
                    for j in st.session_state.jobs:
                        if j["location"] == atab and j.get("calEventId"):
                            delete_gcal_event(j["calEventId"])
                    st.session_state.archived_tabs = [t for t in st.session_state.archived_tabs if t != atab]
                    st.session_state.jobs = [j for j in st.session_state.jobs if j["location"] != atab]
                    save_data()
                    st.rerun()
            st.divider()

# ── Open dialog ───────────────────────────────────────────────────────────────

if st.session_state.get("dlg_open"):
    task_dialog()
