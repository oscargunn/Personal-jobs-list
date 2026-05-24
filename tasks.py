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

DEFAULT_TABS = ["Personal", "University", "Home"]
STORAGE_FILE = "tasks_data.json"
SCOPES        = ["https://www.googleapis.com/auth/calendar.events"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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


def _calendar_map() -> dict:
    """Returns {calendar_id: tab_name} for all configured calendars.
    Reads from [calendar_map] section in secrets; falls back to single calendar_id."""
    try:
        cm = dict(st.secrets.get("calendar_map", {}))
        if cm:
            return cm
    except Exception:
        pass
    # Fallback: primary calendar → first tab
    cid      = _calendar_id()
    first_tab = (st.session_state.tabs_list[0]
                 if "tabs_list" in st.session_state and st.session_state.tabs_list
                 else "Personal")
    return {cid: first_tab}


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


def _calendar_id_for_tab(tab_name: str) -> str:
    """Return the calendar ID to use when writing events for a given tab.
    Looks up the reverse of calendar_map; falls back to the primary calendar_id."""
    for cal_id, name in _calendar_map().items():
        if name == tab_name:
            return cal_id
    return _calendar_id()


def create_gcal_event(job: dict) -> str | None:
    svc = _get_service()
    if not svc or not job.get("dueDate"):
        return None
    cal_id = _calendar_id_for_tab(job.get("location", ""))
    try:
        result = svc.events().insert(calendarId=cal_id, body=_event_body(job)).execute()
        return result.get("id")
    except Exception as e:
        st.session_state["gcal_error"] = f"Calendar error: {e}"
        return None


def update_gcal_event(event_id: str, job: dict):
    svc = _get_service()
    if not svc or not event_id:
        return
    cal_id = _calendar_id_for_tab(job.get("location", ""))
    try:
        svc.events().update(calendarId=cal_id, eventId=event_id, body=_event_body(job)).execute()
    except Exception as e:
        st.session_state["gcal_error"] = f"Calendar update failed: {e}"


def delete_gcal_event(event_id: str, tab_name: str = ""):
    svc = _get_service()
    if not svc or not event_id:
        return
    cal_id = _calendar_id_for_tab(tab_name) if tab_name else _calendar_id()
    try:
        svc.events().delete(calendarId=cal_id, eventId=event_id).execute()
    except Exception:
        pass


def sync_from_calendar() -> int:
    """Pull events from all configured calendars, routing each to its mapped tab."""
    svc = _get_service()
    if not svc:
        return 0

    cal_map = _calendar_map()   # {calendar_id: tab_name}

    # Auto-create any tabs that don't exist yet
    for tab_name in cal_map.values():
        if tab_name not in st.session_state.tabs_list:
            st.session_state.tabs_list.append(tab_name)

    past   = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
    future = (datetime.utcnow() + timedelta(days=180)).isoformat() + "Z"

    existing_ids = {j.get("calEventId") for j in st.session_state.jobs if j.get("calEventId")}
    added = 0

    for cal_id, tab_name in cal_map.items():
        # Paginate through all results — don't stop at 200
        page_token = None
        events = []
        try:
            while True:
                kwargs = dict(
                    calendarId=cal_id,
                    timeMin=past, timeMax=future,
                    maxResults=500, singleEvents=True, orderBy="startTime",
                )
                if page_token:
                    kwargs["pageToken"] = page_token
                result     = svc.events().list(**kwargs).execute()
                events    += result.get("items", [])
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            st.session_state["gcal_error"] = f"Could not read calendar '{tab_name}': {e}"
            continue

        for ev in events:
            if ev["id"] in existing_ids:
                continue
            if ev.get("recurringEventId"):          # skip auto-rolling events
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

            st.session_state.jobs.append({
                "id":          gen_id(),
                "title":       ev.get("summary", "Untitled"),
                "description": ev.get("description", ""),
                "notes":       "",
                "priority":    "Medium",
                "status":      "Pending",
                "location":    tab_name,
                "dueDate":     due_date,
                "dueTime":     due_time,
                "calEventId":  ev["id"],
                "createdAt":   now_ms(),
                "completedAt": None,
            })
            existing_ids.add(ev["id"])
            added += 1

    if added:
        save_data()
    return added

# ── Google Sheets persistence ─────────────────────────────────────────────────
# Service accounts can read/write Sheets without storage-quota issues.
# Setup: create a Google Sheet, share it with the service account (Editor),
# then add  sheets_id = "YOUR_SHEET_ID"  to [gcp_service_account] in secrets.

def _sheets_id() -> str:
    try:
        return st.secrets["gcp_service_account"].get("sheets_id", "")
    except Exception:
        return ""


def _get_sheets_service():
    if not _GCAL_PKGS or not has_gcal_secrets() or not _sheets_id():
        return None
    try:
        info  = dict(st.secrets["gcp_service_account"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SHEETS_SCOPES
        )
        return build("sheets", "v4", credentials=creds)
    except Exception:
        return None


def _load_from_sheets() -> dict | None:
    svc = _get_sheets_service()
    sid = _sheets_id()
    if not svc or not sid:
        return None
    try:
        result = (
            svc.spreadsheets().values()
            .get(spreadsheetId=sid, range="A1")
            .execute()
        )
        values = result.get("values", [])
        if values and values[0] and values[0][0].strip():
            return json.loads(values[0][0])
        return {}   # empty sheet = fresh start (not an error)
    except Exception as e:
        st.session_state["storage_error"] = f"Sheets load failed: {e}"
        return None


def _save_to_sheets(data: dict):
    svc = _get_sheets_service()
    sid = _sheets_id()
    if not svc or not sid:
        return
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sid,
            range="A1",
            valueInputOption="RAW",
            body={"values": [[json.dumps(data)]]},
        ).execute()
        st.session_state.pop("storage_error", None)
    except Exception as e:
        st.session_state["storage_error"] = f"Sheets save failed: {e}"


# ── Data layer ────────────────────────────────────────────────────────────────

def now_ms() -> float:
    return datetime.now().timestamp() * 1000

def gen_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))

def _migrate(jobs: list, tabs: list, archived_tabs: list) -> tuple:
    """One-time migrations applied after loading data regardless of source."""
    # Rename legacy 'Work' tab → 'University'
    tabs          = ["University" if t == "Work" else t for t in tabs]
    archived_tabs = ["University" if t == "Work" else t for t in archived_tabs]
    for j in jobs:
        if j.get("location") == "Work":
            j["location"] = "University"
    return jobs, tabs, archived_tabs


def load_data() -> tuple:
    def _parse(d: dict) -> tuple:
        jobs = d.get("jobs", [])
        for j in jobs:
            j.setdefault("calEventId", None)
            j.setdefault("dueTime", "")
        tabs          = d.get("tabs", list(DEFAULT_TABS))
        archived_tabs = d.get("archived_tabs", [])
        return _migrate(jobs, tabs, archived_tabs)

    # Try Sheets first — survives redeploys
    if _sheets_id():
        d = _load_from_sheets()
        if d is not None:
            return _parse(d)

    # Local file fallback (local dev / no Sheets configured)
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE) as f:
                return _parse(json.load(f))
        except Exception:
            pass

    return [], list(DEFAULT_TABS), []

def save_data():
    data = {
        "jobs":          st.session_state.jobs,
        "tabs":          st.session_state.tabs_list,
        "archived_tabs": st.session_state.archived_tabs,
    }
    _save_to_sheets(data)                          # persistent across redeploys
    with open(STORAGE_FILE, "w") as f:             # local cache / dev fallback
        json.dump(data, f, indent=2)

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
        delete_gcal_event(job["calEventId"], job.get("location", ""))
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
    slug = text.lower().replace(" ", "-")
    return (
        f'<span class="task-badge" data-badge="{slug}" '
        f'style="background:{style["bg"]};color:{style["color"]};'
        f'padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;'
        f'letter-spacing:0.03em;display:inline-block;margin:2px;">{text.upper()}</span>'
    )


def render_active_card(job: dict, lk: str):
    today_s = date.today().isoformat()
    is_over = (
        bool(job.get("dueDate"))
        and job["dueDate"] < today_s
        and job["status"] not in ("Completed", "Archived")
    )

    with st.container(border=True):
        # ── Title (click to edit) ───────────────────────────────────────────
        if st.button(job["title"], key=f"edit_{lk}_{job['id']}",
                     use_container_width=True, type="primary"):
            st.session_state.dlg_job_id = job["id"]
            st.session_state.dlg_open   = True
            st.rerun()

        # ── Badges + trash ──────────────────────────────────────────────────
        bc, dc = st.columns([6, 1])
        with bc:
            st.markdown(
                badge(job["priority"], PRIORITY_STYLES[job["priority"]]) + " " +
                badge(job["status"],   STATUS_STYLES[job["status"]]),
                unsafe_allow_html=True,
            )
        with dc:
            if st.button("🗑", key=f"del_{lk}_{job['id']}", use_container_width=True):
                remove_job(job["id"])
                st.rerun()

        # ── Due date line ───────────────────────────────────────────────────
        meta = []
        if job.get("dueDate"):
            cal_icon   = " 📅" if job.get("calEventId") else ""
            time_label = f" {job['dueTime']}" if job.get("dueTime") else ""
            overdue_tag = "  · OVERDUE" if is_over else ""
            meta.append(f"Due {job['dueDate']}{time_label}{cal_icon}{overdue_tag}")
        if job["status"] == "Completed" and job.get("completedAt"):
            ms_left = job["completedAt"] + 48 * 3_600_000 - now_ms()
            if ms_left > 0:
                h = int(ms_left / 3_600_000)
                m = int((ms_left % 3_600_000) / 60_000)
                meta.append(f"Archives in {h}h {m}m")
        if meta:
            col = "#ef4444" if is_over else "#6b7280"
            st.markdown(
                f"<p style='font-size:11px;color:{col};margin:4px 0 0;'>{'  ·  '.join(meta)}</p>",
                unsafe_allow_html=True,
            )

        # ── Notes (always clipped, no toggle) ──────────────────────────────
        notes = job.get("notes", "") or job.get("description", "")
        if notes:
            clipped = (notes[:100].rstrip() + "…") if len(notes) > 100 else notes
            st.markdown(
                f"<p style='font-size:11px;color:#6b7280;margin:2px 0 4px;"
                f"overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;"
                f"-webkit-box-orient:vertical;'>{clipped}</p>",
                unsafe_allow_html=True,
            )


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


def _local_today() -> date:
    """Return today's date in the user's configured timezone.
    Streamlit Cloud runs on UTC — without this, Auckland users see yesterday."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(_timezone())).date()
    except Exception:
        return date.today()


def render_today_tab():
    today_d      = _local_today()
    today_s      = today_d.isoformat()
    tomorrow_s   = (today_d + timedelta(days=1)).isoformat()
    month_end_s  = (today_d + timedelta(days=30)).isoformat()

    active = [j for j in st.session_state.jobs if j["status"] not in ("Completed", "Archived")]

    overdue      = sorted([j for j in active if j.get("dueDate") and j["dueDate"] < today_s],
                          key=lambda x: x["dueDate"])
    due_today    = [j for j in active if j.get("dueDate") and j["dueDate"] == today_s]
    due_tomorrow = [j for j in active if j.get("dueDate") and j["dueDate"] == tomorrow_s]
    coming_up    = sorted(
        [j for j in active if j.get("dueDate") and tomorrow_s < j["dueDate"] <= month_end_s],
        key=lambda x: x["dueDate"],
    )

    if not overdue and not due_today and not due_tomorrow and not coming_up:
        st.markdown("""
        <div style='text-align:center;padding:80px 0;'>
            <div style='font-size:38px;margin-bottom:10px;'>✓</div>
            <div style='font-size:16px;font-weight:600;margin-bottom:6px;'>You're all caught up</div>
            <div style='font-size:13px;color:#94a3b8;'>Nothing due in the next 30 days</div>
        </div>""", unsafe_allow_html=True)
        return

    def _cards(jobs, lk):
        cols = st.columns(3)
        for idx, job in enumerate(jobs):
            with cols[idx % 3]:
                render_active_card(job, lk)

    if overdue:
        st.markdown(f"#### ⚠ Overdue &nbsp; `{len(overdue)}`")
        _cards(overdue, "ov")
        if due_today or due_tomorrow or coming_up:
            st.markdown("---")

    if due_today:
        st.markdown(f"#### Today &nbsp; `{len(due_today)}`")
        _cards(due_today, "dt")
        if due_tomorrow or coming_up:
            st.markdown("---")

    if due_tomorrow:
        st.markdown(f"#### Tomorrow &nbsp; `{len(due_tomorrow)}`")
        _cards(due_tomorrow, "tm")
        if coming_up:
            st.markdown("---")

    if coming_up:
        st.markdown(f"#### Coming Up &nbsp; `{len(coming_up)}`")
        _cards(coming_up, "cu")


def render_kanban(location: str, view: str, filter_priority: str, filter_status: str):
    lk = location.replace(" ", "_").lower()

    def matches(j):
        return (
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
if "storage_error" in st.session_state:
    st.warning(f"💾 {st.session_state['storage_error']}")

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap');

/* ── Base ───────────────────────────────────────────────────────────────── */
html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }
.block-container { padding-top: 1.5rem !important; padding-bottom: 2rem !important; max-width: 1440px !important; }
#MainMenu, footer, header { visibility: hidden; }

/* ── App header ─────────────────────────────────────────────────────────── */
.app-title    { font-size: 15px; font-weight: 600; color: #111827; letter-spacing: -0.02em; margin: 0; }
.app-subtitle { font-size: 11px; color: #9ca3af; font-weight: 400; margin-top: 1px; }
.gcal-status  { font-size: 11px; font-weight: 500; color: #16a34a; margin: 6px 0 0; }
.gcal-warn    { font-size: 11px; color: #9ca3af; margin: 6px 0 0; }

/* ── Dividers ────────────────────────────────────────────────────────────── */
hr { border: none !important; border-top: 1px solid #f3f4f6 !important; margin: 0.5rem 0 !important; }

/* ── Tabs ────────────────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
    gap: 0; border-bottom: 1px solid #e5e7eb; background: transparent;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'Inter', sans-serif !important; font-size: 12px !important;
    font-weight: 500 !important; color: #9ca3af !important;
    padding: 8px 16px !important; border-radius: 0 !important;
    border-bottom: 2px solid transparent !important; letter-spacing: 0 !important;
}
.stTabs [aria-selected="true"] {
    color: #111827 !important; border-bottom: 2px solid #111827 !important;
    background: transparent !important;
}
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"]    { display: none !important; }

/* ── Cards ───────────────────────────────────────────────────────────────── */
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 8px !important; border: 1px solid #e5e7eb !important;
    background: #ffffff !important; padding: 0 !important; margin-bottom: 6px !important;
}
div[data-testid="stVerticalBlockBorderWrapper"] > div { padding: 10px 12px !important; }
div[data-testid="stVerticalBlockBorderWrapper"] p { margin: 0 !important; line-height: 1.4 !important; }

/* ── Card title button (primary inside a card) ───────────────────────────── */
div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"] {
    background: #f3f4f6 !important; color: #111827 !important;
    border: none !important; border-radius: 6px !important;
    text-align: center !important; font-size: 13px !important; font-weight: 600 !important;
    min-height: 44px !important; height: auto !important;
    padding: 10px 14px !important; white-space: normal !important;
    word-break: break-word !important; line-height: 1.4 !important;
    letter-spacing: -0.01em !important; width: 100% !important;
    box-shadow: none !important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"]:hover {
    background: #e5e7eb !important; color: #111827 !important;
}

/* ── Card delete button (secondary inside a card) ────────────────────────── */
div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="secondary"] {
    background: transparent !important; border: none !important;
    color: #d1d5db !important; font-size: 14px !important;
    padding: 2px 4px !important; height: 26px !important; min-height: 26px !important;
    box-shadow: none !important; filter: none !important;
}
div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="secondary"]:hover {
    color: #ef4444 !important; background: transparent !important;
}

/* ── Global buttons ──────────────────────────────────────────────────────── */
.stButton button {
    font-family: 'Inter', sans-serif !important; font-size: 12px !important;
    font-weight: 500 !important; border-radius: 6px !important;
    letter-spacing: 0 !important; height: 30px !important; min-height: 30px !important;
    padding: 0 12px !important;
}
.stButton button[kind="primary"]         { background: #111827 !important; border: none !important; color: #ffffff !important; }
.stButton button[kind="primary"]:hover   { background: #1f2937 !important; }
.stButton button[kind="secondary"]       { background: #ffffff !important; border: 1px solid #e5e7eb !important; color: #374151 !important; filter: none !important; }
.stButton button[kind="secondary"]:hover { background: #f9fafb !important; }

/* ── Inputs ──────────────────────────────────────────────────────────────── */
.stTextInput input, .stTextArea textarea {
    font-family: 'Inter', sans-serif !important; font-size: 13px !important;
    border-radius: 6px !important; border-color: #e5e7eb !important; color: #111827 !important;
}
.stSelectbox [data-baseweb="select"] { border-radius: 6px !important; }
.stSelectbox [data-baseweb="select"] > div { font-size: 12px !important; }

/* ── Typography ──────────────────────────────────────────────────────────── */
h4 { font-size: 11px !important; font-weight: 600 !important; color: #6b7280 !important;
     text-transform: uppercase !important; letter-spacing: 0.08em !important; }
.stCaptionContainer, [data-testid="stCaptionContainer"] { color: #9ca3af !important; font-size: 11px !important; }
[data-testid="stToast"] { font-family: 'Inter', sans-serif !important; font-size: 13px !important; }
.stRadio label { font-family: 'Inter', sans-serif !important; font-size: 12px !important; font-weight: 500 !important; }
code { font-family: 'DM Mono', monospace !important; font-size: 11px !important;
       background: #f3f4f6 !important; color: #6b7280 !important;
       border-radius: 4px !important; padding: 1px 6px !important; }

/* ── Kanban column headers ────────────────────────────────────────────────── */
.col-header { font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase;
              letter-spacing: 0.08em; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }

/* ── Dark mode ───────────────────────────────────────────────────────────── */
@media (prefers-color-scheme: dark) {

    /* Backgrounds */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
    [data-testid="stHeader"], .block-container { background-color: #0d0d0d !important; }

    /* Typography */
    html, body, p, span, div, label, [class*="css"], .stMarkdown { color: #e5e7eb !important; }
    .app-title    { color: #f9fafb !important; }
    .app-subtitle { color: #6b7280 !important; }
    h4            { color: #6b7280 !important; }
    .gcal-status  { color: #34d399 !important; }
    .gcal-warn    { color: #6b7280 !important; }

    /* Dividers */
    hr { border-top: 1px solid #1f1f1f !important; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #1f1f1f !important; }
    .stTabs [data-baseweb="tab"]      { color: #6b7280 !important; }
    .stTabs [aria-selected="true"]    { color: #f9fafb !important; border-bottom: 2px solid #f9fafb !important; }

    /* Cards */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #111111 !important; border-color: #1f1f1f !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] p { color: #d1d5db !important; }

    /* Card title button in dark mode */
    div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"] {
        background: #1a1a1a !important; color: #f9fafb !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"]:hover {
        background: #262626 !important; color: #f9fafb !important;
    }

    /* Card delete button in dark mode */
    div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="secondary"] {
        color: #374151 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="secondary"]:hover {
        color: #ef4444 !important;
    }

    /* Global buttons */
    .stButton button[kind="primary"]         { background: #f9fafb !important; color: #0d0d0d !important; }
    .stButton button[kind="primary"]:hover   { background: #e5e7eb !important; }
    .stButton button[kind="secondary"]       { background: #111111 !important; border-color: #1f1f1f !important; color: #9ca3af !important; }
    .stButton button[kind="secondary"]:hover { background: #1a1a1a !important; color: #f9fafb !important; }

    /* Inputs */
    .stTextInput input, .stTextArea textarea {
        background-color: #111111 !important; color: #f9fafb !important; border-color: #1f1f1f !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder { color: #4b5563 !important; }

    /* Selectboxes */
    .stSelectbox [data-baseweb="select"] > div, div[data-baseweb="select"] > div {
        background-color: #111111 !important; color: #f9fafb !important; border-color: #1f1f1f !important;
    }
    [data-baseweb="menu"], [data-baseweb="popover"] { background-color: #111111 !important; }
    [data-baseweb="option"]       { background-color: #111111 !important; color: #e5e7eb !important; }
    [data-baseweb="option"]:hover { background-color: #1a1a1a !important; }

    /* Misc */
    .stCheckbox label, .stRadio label, [data-testid="stRadio"] { color: #e5e7eb !important; }
    .stCaptionContainer, [data-testid="stCaptionContainer"] { color: #6b7280 !important; }
    code { background: #1a1a1a !important; color: #6b7280 !important; }

    /* Expander */
    [data-testid="stExpander"]         { border-color: #1f1f1f !important; background-color: #0d0d0d !important; }
    [data-testid="stExpander"] summary { color: #e5e7eb !important; }

    /* Dialog */
    [data-testid="stDialog"] > div, [data-baseweb="modal"] > div {
        background-color: #111111 !important; border: 1px solid #1f1f1f !important;
    }

    /* Toast */
    [data-testid="stToast"] { background-color: #111111 !important; color: #e5e7eb !important; }

    /* Badges */
    span.task-badge[data-badge="low"]         { background: #1e3a5f !important; color: #93c5fd !important; }
    span.task-badge[data-badge="medium"]      { background: #431407 !important; color: #fdba74 !important; }
    span.task-badge[data-badge="high"]        { background: #2e1065 !important; color: #d8b4fe !important; }
    span.task-badge[data-badge="urgent"]      { background: #450a0a !important; color: #fca5a5 !important; }
    span.task-badge[data-badge="pending"]     { background: #4a0d2e !important; color: #f9a8d4 !important; }
    span.task-badge[data-badge="in-progress"] { background: #431407 !important; color: #fdba74 !important; }
    span.task-badge[data-badge="completed"]   { background: #052e16 !important; color: #86efac !important; }
    span.task-badge[data-badge="archived"]    { background: #1a1a1a !important; color: #6b7280 !important; }
}
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────

hc1, hc2, hc3 = st.columns([4, 1.2, 1.5])
with hc1:
    st.markdown("""
    <div style="padding: 4px 0 6px;">
        <div class="app-title">Personal Tasks</div>
        <div class="app-subtitle">Task Manager</div>
    </div>
    """, unsafe_allow_html=True)
with hc2:
    if not _GCAL_PKGS:
        st.markdown("<p class='gcal-warn'>⚠ Reboot app</p>", unsafe_allow_html=True)
    elif not has_gcal_secrets():
        st.markdown("<p class='gcal-warn'>⚠ Add secrets</p>", unsafe_allow_html=True)
    elif gcal_connected():
        sheets_ok = _sheets_id() and "storage_error" not in st.session_state
        status    = "📅💾 Connected" if sheets_ok else "📅 Connected"
        st.markdown(f"<p class='gcal-status'>{status}</p>", unsafe_allow_html=True)
    else:
        st.markdown("<p class='gcal-warn'>⚠ Calendar error</p>", unsafe_allow_html=True)
with hc3:
    if gcal_connected():
        if st.button("⬇ Sync Calendar", use_container_width=True):
            n = sync_from_calendar()
            st.session_state["gcal_toast"] = (
                f"Imported {n} new task{'s' if n != 1 else ''} from Calendar" if n
                else "All calendars up to date"
            )
            st.rerun()

st.markdown("---")

# ── Location tabs ─────────────────────────────────────────────────────────────

_today_d      = _local_today()
_today_s      = _today_d.isoformat()
_month_end_s  = (_today_d + timedelta(days=30)).isoformat()
_today_count  = sum(
    1 for j in st.session_state.jobs
    if j.get("dueDate") and j["dueDate"] <= _month_end_s
    and j["status"] not in ("Completed", "Archived")
)
tab_labels = [f"Today  ({_today_count})" if _today_count else "Today"]
for loc in st.session_state.tabs_list:
    n = sum(1 for j in st.session_state.jobs if j["location"] == loc and j["status"] != "Archived")
    tab_labels.append(f"{loc}  ({n})")
tab_labels.append("+ New List")
archived_count = len(st.session_state.get("archived_tabs", []))
tab_labels.append(f"Archived Lists  ({archived_count})" if archived_count else "Archived Lists")

loc_tabs = st.tabs(tab_labels)

# ── Today tab (index 0) ───────────────────────────────────────────────────────
with loc_tabs[0]:
    render_today_tab()

# ── List tabs (index 1 … len(tabs)+1) ────────────────────────────────────────
for i, tab_ctx in enumerate(loc_tabs[1:-2]):
    with tab_ctx:
        loc = st.session_state.tabs_list[i]

        fc1, fc2, fc3, fc4 = st.columns([1.3, 1.3, 1.2, 1])
        with fc1:
            filter_priority = st.selectbox(
                "Priority", ["All", "Low", "Medium", "High", "Urgent"],
                label_visibility="collapsed", key=f"fp_{loc}",
            )
        with fc2:
            filter_status = st.selectbox(
                "Status", ["All", "Pending", "In Progress", "Completed"],
                label_visibility="collapsed", key=f"fs_{loc}",
            )
        with fc3:
            if st.button("+ Add Task", key=f"add_{loc}", type="primary", use_container_width=True):
                st.session_state.dlg_job_id   = None
                st.session_state.dlg_location = loc
                st.session_state.dlg_open     = True
                st.rerun()
        with fc4:
            view = st.radio(
                "View", ["Active", "Archive"],
                horizontal=True, key=f"view_{loc}", label_visibility="collapsed",
            )

        with st.expander("List Settings"):
            tabs_list = st.session_state.tabs_list
            idx       = tabs_list.index(loc)

            st.caption("Rename")
            rc1, rc2 = st.columns([3, 1])
            with rc1:
                new_name_val = st.text_input("New name", value=loc, key=f"rename_{loc}",
                                             label_visibility="collapsed", placeholder="New list name")
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

            st.caption("Reorder")
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
                st.caption(f"Position {idx + 1} of {len(tabs_list)}")

            st.caption("Danger zone")
            dz1, dz2 = st.columns(2)
            with dz1:
                if st.button("Archive List", key=f"arc_tab_{loc}", use_container_width=True):
                    for j in st.session_state.jobs:
                        if j["location"] == loc:
                            j["status"] = "Archived"
                    st.session_state.tabs_list = [t for t in st.session_state.tabs_list if t != loc]
                    st.session_state.archived_tabs.append(loc)
                    save_data()
                    st.rerun()
            with dz2:
                if st.button("Delete List", key=f"del_tab_{loc}", use_container_width=True):
                    for j in st.session_state.jobs:
                        if j["location"] == loc and j.get("calEventId"):
                            delete_gcal_event(j["calEventId"], loc)
                    st.session_state.tabs_list = [t for t in st.session_state.tabs_list if t != loc]
                    st.session_state.jobs      = [j for j in st.session_state.jobs if j["location"] != loc]
                    save_data()
                    st.rerun()

        st.divider()
        render_kanban(loc, view, filter_priority, filter_status)

# ── New List & Archived Lists ─────────────────────────────────────────────────

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
