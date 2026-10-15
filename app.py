"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows risk factors + question form, then review confirm
"""

from __future__ import annotations

import asyncio
import os
import uuid

import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from common.db import db_conn, db_path
from common.github import PR_URL_RE
from exercises.exercise_4_audit import build_graph


load_dotenv()


# ─── Database Helpers ───────────────────────────────────────────────────────
async def get_recent_sessions() -> list[dict]:
    """Fetch recent review sessions with worst_risk ordered high > med > low."""
    async with db_conn() as conn:
        async with conn.execute(
            """
            SELECT thread_id,
                   pr_url,
                   MIN(timestamp) AS started,
                   MAX(timestamp) AS last_event,
                   CASE
                       WHEN SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END) > 0 THEN 'high'
                       WHEN SUM(CASE WHEN risk_level = 'med' THEN 1 ELSE 0 END) > 0 THEN 'med'
                       ELSE 'low'
                   END AS worst_risk,
                   COUNT(*) AS events
              FROM audit_events
             GROUP BY thread_id, pr_url
             ORDER BY MAX(timestamp) DESC
             LIMIT 25
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def load_thread_checkpoint(thread_id: str):
    """Retrieve checkpoint state for an existing thread."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp, confirm_after_synthesize=True)
        cfg = {"configurable": {"thread_id": thread_id}}
        return await app.aget_state(cfg)


async def run_graph(pr_url: str, thread_id: str, resume_value=None, continue_execution=False):
    """Invoke the graph once. Returns the final result or dict with '__interrupt__'."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp, confirm_after_synthesize=True)
        cfg = {"configurable": {"thread_id": thread_id}}

        if continue_execution:
            return await app.ainvoke(None, cfg)
        if resume_value is None:
            return await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)
        else:
            return await app.ainvoke(Command(resume=resume_value), cfg)


def run_async(coro):
    """Helper to run async coroutines safely."""
    return asyncio.run(coro)


# ─── Page Setup ────────────────────────────────────────────────────────────
st.set_page_config(page_title="HITL PR Review Agent", layout="wide", page_icon="🤖")
st.title("🤖 Human-in-the-Loop PR Review Agent")


# ─── Session State Initialization ──────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None
if "editing_comment" not in st.session_state:
    st.session_state.editing_comment = False


# ─── Query Parameter Handling (Thread Resumption on Refresh/Link) ──────────
query_thread = st.query_params.get("thread")
if query_thread and query_thread != st.session_state.thread_id:
    try:
        snapshot = run_async(load_thread_checkpoint(query_thread))
        if snapshot and snapshot.values:
            st.session_state.thread_id = query_thread
            st.session_state.pr_url = snapshot.values.get("pr_url", "")
            if snapshot.next:
                # Paused at interrupt
                interrupt_payload = None
                if snapshot.tasks:
                    for t in snapshot.tasks:
                        if t.interrupts:
                            interrupt_payload = t.interrupts[0].value
                            break
                st.session_state.interrupt_payload = interrupt_payload
                st.session_state.final = None
            else:
                # Finished session
                st.session_state.interrupt_payload = None
                st.session_state.final = snapshot.values
    except Exception as e:
        st.sidebar.error(f"Failed to load thread {query_thread}: {e}")


# ─── Sidebar — Recent Sessions ─────────────────────────────────────────────
with st.sidebar:
    st.header("📋 Recent Review Sessions")
    try:
        sessions = run_async(get_recent_sessions())
        if sessions:
            for s in sessions:
                tid = s["thread_id"]
                risk = s["worst_risk"]
                risk_icon = "🔴" if risk == "high" else ("🟡" if risk == "med" else "🟢")
                is_active = (tid == st.session_state.thread_id)
                prefix = "👉 " if is_active else ""

                label = f"{prefix}{risk_icon} {s['pr_url'].split('/')[-1]} (#{tid[:6]})"
                if st.button(label, key=f"session_{tid}", help=f"PR: {s['pr_url']}\nEvents: {s['events']}\nStarted: {s['started']}"):
                    st.session_state.thread_id = tid
                    st.query_params["thread"] = tid
                    st.session_state.editing_comment = False

                    snapshot = run_async(load_thread_checkpoint(tid))
                    if snapshot and snapshot.values:
                        st.session_state.pr_url = snapshot.values.get("pr_url", "")
                        if snapshot.next:
                            interrupt_payload = None
                            if snapshot.tasks:
                                for t in snapshot.tasks:
                                    if t.interrupts:
                                        interrupt_payload = t.interrupts[0].value
                                        break
                            st.session_state.interrupt_payload = interrupt_payload
                            st.session_state.final = None
                        else:
                            st.session_state.interrupt_payload = None
                            st.session_state.final = snapshot.values
                    st.rerun()
        else:
            st.caption("No review sessions recorded yet.")
    except Exception as e:
        st.error(f"Database error reading audit log: {e}")


# ─── Top Form — Start New Review ───────────────────────────────────────────
st.subheader("Start or Inspect Review")
with st.form("start_form"):
    input_url = st.text_input(
        "Pull Request URL",
        value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Run Review", type="primary")

if submitted and input_url:
    # 1. Validation checks
    if not PR_URL_RE.search(input_url.strip()):
        st.error("❌ Invalid PR URL format. Expected: https://github.com/owner/repo/pull/123")
    elif not os.environ.get("OPENROUTER_API_KEY"):
        st.error("❌ Configuration error: OPENROUTER_API_KEY is not set in environment or .env file.")
    elif not os.environ.get("GITHUB_TOKEN"):
        st.error("❌ Configuration error: GITHUB_TOKEN is not set in environment or .env file.")
    else:
        new_thread_id = str(uuid.uuid4())
        st.session_state.pr_url = input_url.strip()
        st.session_state.thread_id = new_thread_id
        st.session_state.interrupt_payload = None
        st.session_state.final = None
        st.session_state.editing_comment = False
        st.query_params["thread"] = new_thread_id

        with st.spinner("Fetching PR and running LLM analysis..."):
            try:
                result = run_async(run_graph(st.session_state.pr_url, new_thread_id))
                if "__interrupt__" in result:
                    st.session_state.interrupt_payload = result["__interrupt__"][0].value
                else:
                    st.session_state.final = result
                st.rerun()
            except Exception as e:
                st.error(f"❌ Execution failed: {e}")


# ─── Helper to build default comment body for editing ──────────────────────
def get_default_comment_body(payload: dict) -> str:
    """Build the markdown comment text from payload."""
    if payload.get("comment_body"):
        return payload["comment_body"]
    lines = [f"### Automated review (confidence {payload.get('confidence', 0.0):.0%})", "", payload.get("summary", ""), ""]
    for c in payload.get("comments", []):
        lines.append(f"- **[{c['severity']}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")
    if payload.get("escalation_answers"):
        lines.append("\n_Reviewer answered escalation questions:_")
        for q, ans in payload["escalation_answers"].items():
            lines.append(f"> **{q}** {ans}")
    return "\n".join(lines)


# ─── Renderers for Interrupt Cards ─────────────────────────────────────────
def render_approval_card(payload: dict) -> dict | None:
    """58–72% bucket: show LLM review + Approve / Reject / Edit."""
    conf = payload["confidence"]
    st.markdown(f"### 🟡 Human Approval Requested — Confidence {conf:.0%}")
    st.info(f"**Reasoning:** {payload.get('confidence_reasoning', '')}")
    st.markdown(f"**Summary:**\n{payload.get('summary', '')}")

    st.markdown("#### Proposed Comments")
    comments = payload.get("comments", [])
    if comments:
        for c in comments:
            st.markdown(f"- **[{c['severity'].upper()}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")
    else:
        st.caption("No specific line comments proposed.")

    if payload.get("diff_preview"):
        with st.expander("🔍 View Diff Preview"):
            st.code(payload["diff_preview"], language="diff")

    # If currently in Edit Mode:
    if st.session_state.editing_comment:
        st.markdown("---")
        st.markdown("#### ✏️ Direct Comment Editing")
        st.caption("Edit the complete markdown text below before confirming.")
        default_body = get_default_comment_body(payload)
        edited_text = st.text_area("Review Comment Markdown", value=default_body, height=220,
                                   key=f"edit_area_{st.session_state.thread_id}")

        with st.expander("👁️ Preview Edited Markdown", expanded=True):
            st.markdown(edited_text)

        col_conf, col_canc = st.columns([1, 1])
        if col_conf.button("✅ Confirm & Post Review", type="primary", key="btn_confirm_edit"):
            if not edited_text.strip():
                st.error("Review comment must not be empty.")
            else:
                st.session_state.editing_comment = False
                return {"choice": "edit", "feedback": "Edited directly by reviewer", "edited_body": edited_text}
        if col_canc.button("❌ Cancel Edit", key="btn_cancel_edit"):
            st.session_state.editing_comment = False
            st.rerun()
        return None

    # Normal mode with 3 buttons
    st.markdown("---")
    feedback = st.text_input("Reviewer Feedback (optional note to include with comment):",
                             key=f"approval_feedback_{st.session_state.thread_id}")

    col1, col2, col3 = st.columns(3)
    if col1.button("✅ Approve & Post", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("🚫 Reject Review", use_container_width=True):
        return {"choice": "reject", "feedback": feedback or "Rejected by human reviewer"}
    if col3.button("✏️ Edit Review", use_container_width=True):
        st.session_state.editing_comment = True
        st.rerun()

    return None


def render_escalation_card(payload: dict) -> dict | None:
    """< 58% bucket: show risk factors + questions form."""
    conf = payload["confidence"]
    st.markdown(f"### 🔴 Strong Escalation Required — Confidence {conf:.0%}")
    st.warning(f"**Reasoning:** {payload.get('confidence_reasoning', '')}")

    if payload.get("risk_factors"):
        st.error("**⚠️ Identified Risk Factors:**\n- " + "\n- ".join(payload["risk_factors"]))

    st.markdown(f"**Summary:**\n{payload.get('summary', '')}")

    if payload.get("diff_preview"):
        with st.expander("🔍 View Diff Preview"):
            st.code(payload["diff_preview"], language="diff")

    st.markdown("#### ❓ Reviewer Clarification Questions")
    st.caption("The agent needs your input on these questions to re-synthesize an accurate review.")

    questions = payload.get("questions", [])
    with st.form("escalation_form"):
        answers: dict[str, str] = {}
        for idx, q in enumerate(questions):
            ans = st.text_area(f"Q{idx + 1}: {q}", key=f"q_{st.session_state.thread_id}_{idx}", placeholder="Your answer or explanation...")
            answers[q] = ans
        submit = st.form_submit_button("Submit Answers & Synthesize Review", type="primary")

    if submit:
        # Validate non-empty answers
        empty = [q for q, a in answers.items() if not a.strip()]
        if empty:
            st.warning("Please provide answers to all questions to help the agent refine the review.")
        else:
            return answers

    return None


def render_synthesize_approval_card(payload: dict) -> dict | None:
    """Post-synthesis confirmation card: Review the synthesized review before posting."""
    conf = payload.get("confidence", 0.0)
    st.markdown(f"### 🔄 Review Synthesized Analysis — Confidence {conf:.0%}")
    st.info(f"**Updated Reasoning:** {payload.get('confidence_reasoning', '')}")
    st.markdown(f"**Updated Summary:**\n{payload.get('summary', '')}")

    if payload.get("risk_factors"):
        st.warning("**⚠️ Persistent Risk Factors:**\n- " + "\n- ".join(payload["risk_factors"]))

    st.markdown("#### Synthesized Comments")
    comments = payload.get("comments", [])
    if comments:
        for c in comments:
            st.markdown(f"- **[{c['severity'].upper()}]** `{c['file']}:{c.get('line') or '?'}` — {c['body']}")

    if payload.get("escalation_answers"):
        with st.expander("📋 Reviewer Q&A Given"):
            for q, a in payload["escalation_answers"].items():
                st.markdown(f"**Q:** {q}\n\n**A:** {a}\n")

    # Direct edit mode
    if st.session_state.editing_comment:
        st.markdown("---")
        st.markdown("#### ✏️ Direct Comment Editing")
        default_body = get_default_comment_body(payload)
        edited_text = st.text_area("Review Comment Markdown", value=default_body, height=220,
                                   key=f"synth_edit_area_{st.session_state.thread_id}")
        with st.expander("👁️ Preview Edited Markdown", expanded=True):
            st.markdown(edited_text)

        col_conf, col_canc = st.columns([1, 1])
        if col_conf.button("✅ Confirm & Post Review", type="primary", key="btn_confirm_synth_edit"):
            if not edited_text.strip():
                st.error("Review comment must not be empty.")
            else:
                st.session_state.editing_comment = False
                return {"choice": "edit", "feedback": "Edited directly by reviewer", "edited_body": edited_text}
        if col_canc.button("❌ Cancel Edit", key="btn_cancel_synth_edit"):
            st.session_state.editing_comment = False
            st.rerun()
        return None

    st.markdown("---")
    feedback = st.text_input("Reviewer Note (optional):", key=f"synth_feedback_{st.session_state.thread_id}")

    col1, col2, col3 = st.columns(3)
    if col1.button("✅ Approve & Post Refined Review", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("🚫 Reject Review", use_container_width=True):
        return {"choice": "reject", "feedback": feedback or "Rejected by human reviewer"}
    if col3.button("✏️ Edit Review", use_container_width=True):
        st.session_state.editing_comment = True
        st.rerun()

    return None


# ─── Interrupt Handling Flow ───────────────────────────────────────────────
# A failed LLM/API node can have a checkpoint with pending work but no interrupt.
# Continue that checkpoint rather than fetching and analyzing the PR again.
if (st.session_state.thread_id and st.session_state.interrupt_payload is None
        and st.session_state.final is None):
    snapshot = run_async(load_thread_checkpoint(st.session_state.thread_id))
    if snapshot.next:
        st.warning("This session has unfinished work. Retry the saved step after resolving the API error.")
        if st.button("Retry Saved Step", type="primary"):
            try:
                with st.spinner("Retrying saved workflow step..."):
                    res = run_async(run_graph(st.session_state.pr_url, st.session_state.thread_id,
                                              continue_execution=True))
                if "__interrupt__" in res:
                    st.session_state.interrupt_payload = res["__interrupt__"][0].value
                else:
                    st.session_state.final = res
                st.rerun()
            except Exception as e:
                st.error(f"Retry failed: {e}")

payload = st.session_state.interrupt_payload
if payload is not None:
    kind = payload.get("kind")
    if kind == "approval_request":
        user_answer = render_approval_card(payload)
    elif kind == "escalation":
        user_answer = render_escalation_card(payload)
    elif kind == "synthesize_approval":
        user_answer = render_synthesize_approval_card(payload)
    else:
        st.error(f"Unknown interrupt kind: {kind}")
        user_answer = None

    if user_answer is not None:
        with st.spinner("Resuming review workflow..."):
            try:
                res = run_async(run_graph(
                    st.session_state.pr_url,
                    st.session_state.thread_id,
                    resume_value=user_answer,
                ))
                if "__interrupt__" in res:
                    st.session_state.interrupt_payload = res["__interrupt__"][0].value
                else:
                    st.session_state.interrupt_payload = None
                    st.session_state.final = res
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error during resume: {e}")


# ─── Final State Rendering ─────────────────────────────────────────────────
if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "?")
    analysis = final.get("analysis")

    # Important rule: Check for failed states before checking auto/committed prefixes
    if "failed" in action or action == "commit_failed" or action == "auto_commit_failed":
        st.error(f"❌ Comment posting failed: {action}. Check GitHub permissions or token scopes.")
    elif action.startswith("auto") or action.startswith("committed"):
        st.success(f"✅ Review Successfully Posted! (Action: `{action}`)")
        st.markdown(f"🔗 [**View Pull Request on GitHub**]({st.session_state.pr_url})")
    elif action == "rejected":
        st.warning(f"⚠️ Review was rejected. No comments posted to GitHub.")
    else:
        st.info(f"Final Action: {action}")

    if analysis:
        with st.expander("📄 Review Details & Summary", expanded=True):
            conf = getattr(analysis, "confidence", None)
            if conf is not None:
                st.metric("Final Confidence", f"{conf:.0%}")
            summary = getattr(analysis, "summary", "")
            st.markdown(f"**Summary:**\n{summary}")
            comments = getattr(analysis, "comments", [])
            if comments:
                st.markdown("**Review Comments:**")
                for c in comments:
                    if hasattr(c, "severity"):
                        st.markdown(f"- **[{c.severity.upper()}]** `{c.file}:{c.line or '?'}` — {c.body}")
                    elif isinstance(c, dict):
                        st.markdown(f"- **[{c.get('severity', '').upper()}]** `{c.get('file')}:{c.get('line') or '?'}` — {c.get('body')}")

    st.caption(f"Thread ID: `{st.session_state.thread_id}` · Audit Replay: `uv run python -m audit.replay --thread {st.session_state.thread_id}`")
