"""Workflow actions through MCP: scheduling (calendar), then notifications (Gmail, Slack)."""
import asyncio
import re
import secrets
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage
from sqlalchemy import false, or_, select, text

from ..config import settings
from ..db import SessionLocal
from ..mcp_client import McpResult, call_tool
from ..models import Appointment
from .helpers import llm, once, pretty, slack_channel, step, text_of


def _action(service: str, kind: str, res: McpResult | None, ok_detail: str, skip_reason: str = "") -> dict:
    if res is None:
        return {"service": service, "kind": kind, "status": "skipped", "detail": skip_reason}
    if res.ok:
        return {"service": service, "kind": kind, "status": "ok", "detail": ok_detail}
    status = "skipped" if res.not_connected else "failed"
    return {"service": service, "kind": kind, "status": status, "detail": res.error}


# ---------------------------------------------------------------- scheduling


async def _reserve(uid: str, rm_email: str | None, start: datetime, end: datetime, ref: str) -> bool:
    """Atomically claim [start, end) for this customer and RM. The advisory lock serialises
    concurrent bookings for the same RM across ALL customers, preventing double-booking even
    when the RM's own calendar is not readable by this customer's Google account."""
    async with SessionLocal() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": rm_email or uid})
        clash = await s.execute(
            select(Appointment.id)
            .where(
                Appointment.status.in_(["pending", "confirmed"]),
                Appointment.start_at < end,
                Appointment.end_at > start,
                or_(Appointment.rm_email == rm_email if rm_email else false(), Appointment.user_id == uuid.UUID(uid)),
            )
            .limit(1)
        )
        if clash.first():
            return False
        s.add(Appointment(user_id=uuid.UUID(uid), reference=ref, rm_email=rm_email, start_at=start, end_at=end))
        await s.commit()
        return True


async def _finish_reservation(ref: str, status: str, event_id: str | None = None) -> None:
    """Mark a reserved appointment confirmed (with its calendar event id) or failed."""
    async with SessionLocal() as s:
        row = (await s.execute(select(Appointment).where(Appointment.reference == ref))).scalar_one_or_none()
        if row:
            row.status, row.gcal_event_id = status, event_id
            await s.commit()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%A %d %b %Y, %H:%M %Z")


async def scheduler(state) -> dict:
    """Book an appointment through the Calendar MCP server. If the customer named a time, that slot is checked
    against the customer's and the relationship manager's calendars; otherwise the first free slot of the day is
    taken. The slot is reserved in the appointments table before the event is created, so two requests cannot
    double-book, and alternatives are offered on a conflict. Skips with a clear reason when Calendar is not
    connected or no day was given.
    """
    if "schedule_appointment" not in state.get("intents", []):
        return {}
    uid, e, profile = state["user_id"], state["entities"], state.get("profile") or {}
    if not state.get("connections", {}).get("calendar"):
        return {"appointment": {"status": "skipped", "reason": "Google Calendar is not connected"},
                "actions": [_action("calendar", "schedule", None, "", "Google Calendar not connected")],
                "trace": [step("scheduler", "Calendar not connected", "skipped")]}
    day = e.get("appointment_date")
    if not day:
        return {"appointment": {"status": "needs_info", "reason": "No day was specified"},
                "trace": [step("scheduler", "No day specified", "skipped")]}

    tz = ZoneInfo(state.get("timezone") or settings.default_timezone)
    dur = int(e.get("appointment_duration_minutes") or 30)
    rm_email, rm_name = profile.get("rm_email"), profile.get("relationship_manager_name")
    others = [rm_email] if rm_email else []

    if e.get("appointment_time"):
        start = datetime.fromisoformat(f"{day}T{e['appointment_time']}").replace(tzinfo=tz)
        if start <= datetime.now(tz):
            return {"appointment": {"status": "conflict", "reason": "That time is in the past", "alternatives": []},
                    "trace": [step("scheduler", "Requested time is in the past", "failed")]}
        wanted = [(start, start + timedelta(minutes=dur))]
        avail = await call_tool("calendar", uid, "check_availability",
                                {"start": wanted[0][0].isoformat(), "end": wanted[0][1].isoformat(), "attendees": others})
        if not avail.ok and not avail.not_connected:
            return {"appointment": {"status": "failed", "reason": avail.error}, "actions": [_action("calendar", "availability", avail, "")],
                    "trace": [step("scheduler", f"Availability check failed: {avail.error}", "failed")]}
        free_now = avail.ok and avail.data.get("free", False)
    else:
        wanted, free_now = [], False

    slots_res = await call_tool("calendar", uid, "find_free_slots",
                                {"day": day, "timezone": str(tz), "duration_minutes": dur, "attendees": others, "max_slots": 8})
    candidates = [(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])) for s in slots_res.data.get("slots", [])] if slots_res.ok else []
    unreadable = slots_res.data.get("unreadable", []) if slots_res.ok else []

    ref = f"APT-{(wanted[0][0] if wanted else datetime.now(tz)):%Y%m%d}-{secrets.token_hex(2).upper()}"
    chosen = None
    if wanted and free_now and await _reserve(uid, rm_email, *wanted[0], ref):
        chosen = wanted[0]
    elif not wanted:
        for s_, e_ in candidates:
            ref = f"APT-{s_:%Y%m%d}-{secrets.token_hex(2).upper()}"
            if await _reserve(uid, rm_email, s_, e_, ref):
                chosen = (s_, e_)
                break
    if chosen is None:
        alts = [_fmt(s_) for s_, _ in candidates[:3] if not wanted or s_ != wanted[0][0]]
        return {"appointment": {"status": "conflict", "reason": "That slot is not available" if wanted else "No free slot found that day", "alternatives": alts},
                "trace": [step("scheduler", "Slot unavailable - alternatives offered", "done")]}

    start, end = chosen
    topic = e.get("appointment_topic") or "Banking consultation"
    description = (
        f"Reference: {ref}\nCustomer: {state.get('user_name')}\nRelationship manager: {rm_name or 'To be assigned'}\n"
        f"Branch: {settings.branch_address}\n\nAgenda:\n1. {topic}\n2. Review applicable rates and documents\n3. Next steps\n\n"
        f"Please bring photo ID. Quote reference {ref} at the branch."
    )
    attendees = [a for a in (state.get("user_email"), rm_email) if a]
    ev = await call_tool("calendar", uid, "create_event", {
        "summary": f"{settings.bank_name}: {topic} ({ref})", "start": start.isoformat(), "end": end.isoformat(),
        "timezone": str(tz), "description": description, "location": settings.branch_address,
        "attendees": attendees, "reference": ref,
    })
    if not ev.ok:
        await _finish_reservation(ref, "failed")
        return {"appointment": {"status": "failed", "reason": ev.error}, "actions": [_action("calendar", "schedule", ev, "")],
                "trace": [step("scheduler", f"Calendar error: {ev.error}", "failed")]}
    await _finish_reservation(ref, "confirmed", ev.data.get("event_id"))
    appt = {"status": "confirmed", "reference": ref, "when": _fmt(start), "duration_minutes": dur, "topic": topic,
            "location": settings.branch_address, "relationship_manager": rm_name, "rm_email": rm_email,
            "calendar_link": ev.data.get("html_link"),
            "rm_calendar_checked": bool(rm_email) and rm_email not in unreadable}
    return {"appointment": appt, "actions": [_action("calendar", "schedule", ev, f"Invite sent for {_fmt(start)} (ref {ref})")],
            "trace": [step("scheduler", f"Booked {ref} for {_fmt(start)}")]}


# ------------------------------------------------------------- notifications


async def _compose_email(state, purpose: str) -> str:
    """Draft a confirmation email body from the turn's facts only; the model may not add rates or dates."""
    facts = {"purpose": purpose, "customer": state.get("user_name"), "appointment": state.get("appointment"),
             "findings": state.get("evidence", {}), "bank": settings.bank_name}
    prompt = (
        "Write the body of a short, warm, professional plain-text email from the bank to the customer. Use ONLY these facts, "
        "no markdown, no [S#] citation tags, no invented numbers. Include the appointment reference/time/location if present. "
        f"Sign off as '{settings.bank_name}'.\n\n{pretty(facts)}"
    )
    body = text_of((await llm().ainvoke([HumanMessage(prompt)])).content)
    return re.sub(r"\s*\[S\d+\]", "", body).strip()


async def notify(state) -> dict:
    """Send the side-effect notifications for this turn in parallel: confirmation email (Gmail), relationship
    manager, loans team and compliance alerts (Slack). Each action is idempotent per turn and its outcome is
    returned for the reply and the audit trail; a failure never aborts the turn.
    """
    uid, intents, conns = state["user_id"], set(state.get("intents", [])), state.get("connections", {})
    appt, ev, risk, prefs = state.get("appointment") or {}, state.get("evidence", {}), state.get("risk", {}), state.get("prefs")
    turn = state.get("turn_id", uuid.uuid4().hex)
    approved = (state.get("approval") or {}).get("status") == "approved"
    booked = appt.get("status") == "confirmed"
    customer = state.get("user_name", "Customer")
    jobs: list = []

    async def run(service: str, kind: str, tool: str, args: dict, ok_detail: str) -> dict:
        """Call one MCP tool for one notification, at most once per turn, and normalise the result into an action record."""
        if not conns.get(service):
            return _action(service, kind, None, "", f"{service.title()} not connected")
        if not await once(f"{turn}:{kind}"):
            return {"service": service, "kind": kind, "status": "ok", "detail": "already performed"}
        return _action(service, kind, await call_tool(service, uid, tool, args), ok_detail)

    # 1. confirmation email to the customer
    purpose = None
    if booked:
        purpose = ("Appointment confirmed", "Your appointment is confirmed")
    elif "loan_application" in intents:
        purpose = ("Loan application received", "Your loan application has been received")
    elif "statement_request" in intents:
        purpose = ("Account statement summary", "Your account statement summary")
    elif approved:
        purpose = ("Request approved for processing", "Your request has been approved for processing")
    elif state["entities"].get("send_confirmation_email"):
        purpose = ("Summary of your enquiry", "Summary of your enquiry")
    if purpose and state.get("user_email") and conns.get("gmail"):
        body = await _compose_email(state, purpose[1])
        subject = f"{settings.bank_name}: {purpose[0]}" + (f" ({appt['reference']})" if booked else "")
        jobs.append(run("gmail", "customer_confirmation", "send_email", {"to": [state["user_email"]], "subject": subject, "body": body},
                        f"Confirmation emailed to {state['user_email']}"))
    elif purpose:
        jobs.append(asyncio.sleep(0, _action("gmail", "customer_confirmation", None, "", "Gmail not connected")))

    # 2. relationship manager: Slack DM (fallback: channel) and Gmail for high-priority requests
    rm_email = appt.get("rm_email") or (state.get("profile") or {}).get("rm_email")
    if booked and rm_email:
        msg = f"New customer appointment {appt['reference']}: {customer} on {appt['when']} - topic: {appt['topic']}."

        async def rm_slack() -> dict:
            """Tell the relationship manager on Slack: a direct message if their email is in the workspace,
            otherwise a post in the RM channel."""
            if not conns.get("slack"):
                return _action("slack", "rm_notification", None, "", "Slack not connected")
            if not await once(f"{turn}:rm_slack"):
                return {"service": "slack", "kind": "rm_notification", "status": "ok", "detail": "already performed"}
            res = await call_tool("slack", uid, "notify_user_by_email", {"email": rm_email, "text": msg})
            if not res.ok and not res.not_connected:
                res = await call_tool("slack", uid, "post_message", {"channel": slack_channel(prefs, "rm"), "text": f"@RM {rm_email}: {msg}"})
            return _action("slack", "rm_notification", res, "Relationship manager notified")

        jobs.append(rm_slack())
    if rm_email and ("loan_application" in intents or risk.get("level") == "high"):
        why = "loan application" if "loan_application" in intents else "high-risk request"
        jobs.append(run("gmail", "rm_email", "send_email",
                        {"to": [rm_email], "subject": f"[High priority] Customer {why}: {customer}",
                         "body": f"Customer: {customer} ({state.get('user_email')})\nRequest type: {', '.join(state.get('intents', []))}\n"
                                 f"Risk: {risk.get('level')} {'; '.join(risk.get('reasons', []))}\nAppointment: {appt.get('reference', 'none')}\n"},
                        f"RM {rm_email} emailed"))

    # 3. internal Slack alerts
    if "loan_application" in intents:
        elig = ev.get("eligibility_agent", {})
        jobs.append(run("slack", "loans_team_notice", "post_structured", {
            "channel": slack_channel(prefs, "loans"), "title": "Loan application pending approval", "severity": "info",
            "fields": {"Customer": customer, "Product": str(state["entities"].get("loan_type") or "n/a"),
                       "Eligibility": str(elig.get("eligible")), "Summary": str(elig.get("summary", ""))[:300]}}, "Loans team notified"))
    tx = ev.get("transaction_agent", {})
    if tx.get("high_severity") and not state.get("approval"):
        flags = "; ".join(f["detail"] for f in tx["flags"] if f["severity"] == "high")[:600]
        jobs.append(run("slack", "compliance_alert", "post_structured", {
            "channel": slack_channel(prefs, "compliance"), "title": "Unusual transaction pattern detected", "severity": "critical",
            "fields": {"Customer": customer, "Flags": flags, "Source": tx.get("analysis", {}).get("source", "")}}, "Compliance alerted"))

    results = list(await asyncio.gather(*jobs)) if jobs else []
    oks = sum(r["status"] == "ok" for r in results)
    return {"actions": results, "trace": [step("notify", f"{oks}/{len(results)} notifications completed")]} if results else {}
