# projects/operations_agent/streamlit_app.py
# Operations Agent — Streamlit Demo UI  (fixed)
#
# Fixes vs previous version:
#   1. Uses st.chat_message() — reliable rendering, no unsafe_allow_html needed
#   2. API calls match actual router endpoints exactly:
#        POST /sessions                     — start new session
#        POST /sessions/{id}/messages       — follow-up (clarification)
#        POST /sessions/{id}/approve        — approve/reject
#   3. Shows spinner + error detail so silent failures are visible
#   4. timeout=180s (LLM can take 30-60s on cold start)
#
# Run (two terminals):
#   Terminal 1: uvicorn projects.operations_agent.api.main:app --reload
#   Terminal 2: streamlit run projects/operations_agent/streamlit_app.py

import streamlit as st
import requests
import uuid
from datetime import datetime

API_URL = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="Operations Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state ─────────────────────────────────────────────────────────────
for key, default in {
    "thread_id":     None,
    "messages":      [],
    "audit_trail":   [],
    "last_response": None,
    "user_role":     "customer",
    "waiting":       False,
    "api_error": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ── API helpers ───────────────────────────────────────────────────────────────
def api_post(path: str, body: dict) -> dict | None:
    st.session_state.api_error = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            r = session.post(f"{API_URL}{path}", json=body, timeout=180)

        if r.status_code in (200, 201):
            return r.json()

        st.session_state.api_error = f"API error {r.status_code}: {r.text[:300]}"
        return None
    except requests.exceptions.Timeout:
        st.session_state.api_error = "Request timed out (>180s). Check Uvicorn logs."
        return None
    except requests.exceptions.ConnectionError as exc:
        st.session_state.api_error = f"Cannot reach API: {exc}"
        return None
    except Exception as exc:
        st.session_state.api_error = f"Unexpected error: {exc}"
        return None

def start_session(message: str, role: str) -> dict | None:
    thread_id = str(uuid.uuid4())
    data = api_post("/sessions", {
        "message": message,
        "user_role": role,
        "thread_id": thread_id,
    })
    if data:
        st.session_state.thread_id = data["thread_id"]
    return data

def continue_session(message: str) -> dict | None:
    tid = st.session_state.thread_id
    return api_post(f"/sessions/{tid}/messages", {"message": message})

def send_approval(approved: bool) -> dict | None:
    tid = st.session_state.thread_id
    return api_post(f"/sessions/{tid}/approve", {"approved": approved})

def handle_response(response: dict, user_msg: str):
    if not response:
        return
    st.session_state.last_response = response
    status = response.get("status", "")

    if status == "awaiting_approval":
        agent_text = "I've reviewed this request and propose the action below. Please approve or reject."
    else:
        agent_text = response.get("response") or "(no response text)"

    st.session_state.messages.append({
        "role": "agent", "content": agent_text, "status": status,
    })
    st.session_state.audit_trail.append({
        "time":    datetime.now().strftime("%H:%M:%S"),
        "role":    st.session_state.user_role,
        "msg":     user_msg[:45] + ("…" if len(user_msg) > 45 else ""),
        "intent":  response.get("intent") or "—",
        "action":  response.get("proposed_action") or "—",
        "outcome": response.get("execution_outcome") or status or "—",
    })

def reset():
    st.session_state.thread_id     = None
    st.session_state.messages      = []
    st.session_state.last_response = None

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔐 Session")

    role = st.selectbox(
        "User Role",
        ["customer", "support", "supervisor"],
        index=["customer", "support", "supervisor"].index(st.session_state.user_role),
        help="Controls which tools and actions are permitted",
    )
    st.session_state.user_role = role

    role_color = {"customer": "blue", "support": "green", "supervisor": "red"}[role]
    role_perms = {
        "customer":   "View own orders & shipments",
        "support":    "View all data · Create tickets",
        "supervisor": "Full access · Issue refunds",
    }
    st.markdown(
        f"<div style='background:#f0f2f6;padding:10px;border-radius:8px;"
        f"border-left:4px solid {'#1f77b4' if role=='customer' else '#2ca02c' if role=='support' else '#d62728'};'>"
        f"<b>{role.upper()}</b><br><small>{role_perms[role]}</small></div>",
        unsafe_allow_html=True,
    )

    if st.session_state.thread_id:
        st.caption(f"Session: `{st.session_state.thread_id[:8]}...`")

    if st.button("🔄 New Session", use_container_width=True):
        reset()
        st.rerun()

    st.divider()
    st.subheader("⚡ Quick Demo Scenarios")
    st.caption("Click → message loads → press Send")

    scenarios = {
        "👤 Order Status":        ("Where is my order O001?",                                                    "customer"),
        "👤 Refund Request":      ("I want a refund for order O001, the item was defective",                    "customer"),
        "🎧 Create Ticket":       ("Create a support ticket for customer C002 — order O002 is stuck",           "support"),
        "🔑 Issue Refund":        ("Issue a full refund of $149.99 for order O001, customer C001 — defective",  "supervisor"),
        "🔒 Role Block Demo":     ("Issue a refund for order O001 immediately",                                  "customer"),
        "❓ Clarification Demo":  ("I need help with my recent order",                                           "customer"),
    }

    for label, (msg, forced_role) in scenarios.items():
        if st.button(label, use_container_width=True):
            st.session_state.user_role = forced_role
            reset()
            st.session_state["_prefill"] = msg
            st.rerun()

    st.divider()
    st.subheader("📋 Audit Trail")
    if not st.session_state.audit_trail:
        st.caption("Actions appear here after each request.")
    else:
        for entry in reversed(st.session_state.audit_trail[-5:]):
            outcome = entry["outcome"]
            color = ("#2ca02c" if outcome == "success"
                     else "#d62728" if outcome in ("rejected", "role_denied")
                     else "#ff7f0e")
            st.markdown(
                f"<div style='font-size:0.78rem;padding:6px 8px;margin-bottom:4px;"
                f"border-left:3px solid {color};background:#f8f8f8;border-radius:4px;'>"
                f"<b>{entry['time']}</b> · <code>{entry['role']}</code><br>"
                f"\"{entry['msg']}\"<br>"
                f"Intent: <b>{entry['intent']}</b> · Action: <b>{entry['action']}</b><br>"
                f"Outcome: <b style='color:{color}'>{outcome}</b>"
                f"</div>",
                unsafe_allow_html=True,
            )

# ── Main ──────────────────────────────────────────────────────────────────────
st.title("🤖 Operations Agent")
st.caption("AI-powered customer operations · Role-based access · Human-in-the-loop approval · Full audit trail")

# Architecture badges
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("🧠 LangGraph",  "12-node workflow")
c2.metric("🔐 RBAC",       "3-tier roles")
c3.metric("✋ HITL",       "Human approval gate")
c4.metric("📋 Audit",      "Immutable log")
c5.metric("☁️ Azure",      "ACA + Key Vault")

st.divider()

role_colors = {"customer": "#1f77b4", "support": "#2ca02c", "supervisor": "#d62728"}
role_perms  = {
    "customer":   "Can view own orders and shipments only",
    "support":    "Can view all data and create support tickets", 
    "supervisor": "Full access — can issue refunds with approval",
}
st.markdown(
    f"<div style='padding:8px 12px;border-radius:6px;border-left:4px solid "
    f"{role_colors[st.session_state.user_role]};background:#f8f8f8;margin-bottom:1rem;"
    f"font-size:0.85rem;'>"
    f"<b>{st.session_state.user_role.upper()}</b> — "
    f"{role_perms[st.session_state.user_role]}</div>",
    unsafe_allow_html=True,
)

# ── Chat history ──────────────────────────────────────────────────────────────
if not st.session_state.messages:
    st.info("👈 Pick a scenario from the sidebar, or type a message below. Switch roles to see access control in action.")
else:
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.write(msg["content"])
        else:
            with st.chat_message("assistant"):
                st.write(msg["content"])
                status = msg.get("status", "")
                if status == "complete":
                    st.success("✅ Complete")
                elif status == "awaiting_approval":
                    st.warning("⏳ Awaiting your approval below")
                elif status == "awaiting_clarification":
                    st.info("💬 Agent needs more information")
if st.session_state.api_error:
    st.error(st.session_state.api_error)                    

# ── Approval card ─────────────────────────────────────────────────────────────
last = st.session_state.last_response
if last and last.get("status") == "awaiting_approval":
    proposed = last.get("proposed_action", "")
    args     = last.get("proposed_args") or {}

    action_labels = {
        "issue_refund":          "💰 Issue Refund",
        "create_support_ticket": "🎫 Create Support Ticket",
    }
    st.warning(f"### ⚠️ Human Approval Required — {action_labels.get(proposed, proposed)}")

    col_details, col_buttons = st.columns([2, 1])
    with col_details:
        rows = {"Action": action_labels.get(proposed, proposed)}
        for k, v in args.items():
            label = k.replace("_", " ").title()
            if k == "amount":
                v = f"${float(v):.2f}"
            rows[label] = str(v)
        for label, value in rows.items():
            st.markdown(f"**{label}:** {value}")

    with col_buttons:
        st.write("")
        if st.button("✅ Approve", use_container_width=True, type="primary"):
            with st.spinner("Executing action..."):
                resp = send_approval(True)
            if resp:
                st.session_state.messages.append({"role": "user", "content": "✅ Approved"})
                handle_response(resp, "approved")
            st.rerun()

        if st.button("❌ Reject", use_container_width=True):
            with st.spinner("Rejecting..."):
                resp = send_approval(False)
            if resp:
                st.session_state.messages.append({"role": "user", "content": "❌ Rejected"})
                handle_response(resp, "rejected")
            st.rerun()

# ── Status strip ──────────────────────────────────────────────────────────────
if last and last.get("status") and last.get("status") != "awaiting_approval":
    outcome = last.get("execution_outcome") or last.get("status") or ""
    intent  = last.get("intent") or "—"
    action  = last.get("proposed_action") or "—"
    #st.caption(f"Status: `{outcome}` · Intent: `{intent}` · Action: `{action}`")

# ── Input ─────────────────────────────────────────────────────────────────────
st.divider()
prefill = st.session_state.pop("_prefill", "")

with st.form("chat_form", clear_on_submit=True):
    col_in, col_btn = st.columns([5, 1])
    with col_in:
        user_input = st.text_input(
        "Message",
        key="chat_message",
        placeholder="e.g. 'Where is my order O001?'",
        label_visibility="collapsed",
        )
    with col_btn:
        submitted = st.form_submit_button("Send →", use_container_width=True, type="primary")

if submitted and user_input.strip():
    st.session_state.messages.append({"role": "user", "content": user_input})

    # decide: new session or follow-up clarification
    last_status = (st.session_state.last_response or {}).get("status")
    is_clarification = (
        st.session_state.thread_id is not None
        and last_status == "awaiting_clarification"
    )

    with st.spinner("Agent thinking..."):
        if is_clarification:
            response = continue_session(user_input)
        else:
            response = start_session(user_input, st.session_state.user_role)

    handle_response(response, user_input)
    st.rerun()

# ── Demo data reference ───────────────────────────────────────────────────────
with st.expander("📦 Demo Data Reference — customers, orders, IDs"):
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Customers**")
        st.table({"ID": ["C001","C002","C003"],
                  "Name": ["Alice Smith","Bob Jones","Sara Khan"],
                  "Tier": ["Premium","Standard","Premium"],
                  "Status": ["Active","Active","Suspended"]})
        st.markdown("**Orders**")
        st.table({"ID": ["O001","O002","O003"],
                  "Customer": ["C001","C002","C001"],
                  "Status": ["Shipped","Processing","Delivered"],
                  "Total": ["$149.99","$49.99","$299.99"]})
    with col2:
        st.markdown("**Role Permissions**")
        st.table({"Role": ["customer","support","supervisor"],
                  "Read Orders": ["Own only","All","All"],
                  "View Customers": ["No","Yes","Yes"],
                  "Create Ticket": ["No","Yes","Yes"],
                  "Issue Refund": ["No","No","Yes"]})
        st.markdown("**Tracking**")
        st.table({"Order": ["O001","O003"],
                  "Carrier": ["FedEx","UPS"],
                  "Tracking": ["FX123456789","UP987654321"],
                  "Status": ["In Transit","Delivered"]})