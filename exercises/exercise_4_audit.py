"""Exercise 4 — Structured SQLite audit trail + durable checkpointer.

Zero setup — SQLite stores everything in a single file (`./hitl_audit.db`).
The audit_events schema is created automatically on first connection.

Goals:
1. Use AsyncSqliteSaver so the graph can resume after a crash.
2. Define and emit an `AuditEntry` (common/schemas.py) for every meaningful step,
   so the full session can be replayed.
3. Verify with `uv run python -m audit.replay --thread <id>`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from rich.console import Console
from rich.panel import Panel

from common.db import db_path, write_audit_event
from common.github import fetch_pr, post_review_comment
from common.llm import get_llm
from common.schemas import (
    AUTO_APPROVE_THRESHOLD,
    ESCALATE_THRESHOLD,
    AuditEntry,
    PRAnalysis,
    ReviewState,
    risk_level_for,
)


console = Console()
AGENT_ID = "pr-review-agent@v0.1"


def validate_approval_response(resp: dict) -> str:
    """Require an explicit decision before a review can be posted."""
    if not isinstance(resp, dict) or resp.get("choice") not in {"approve", "reject", "edit"}:
        raise ValueError("Expected an explicit approve, reject, or edit decision")
    if resp["choice"] == "edit" and not (resp.get("edited_body") or "").strip():
        raise ValueError("An edited review must contain non-empty comment text")
    return resp["choice"]


async def audit(state: ReviewState, entry: AuditEntry) -> None:
    """Write one structured AuditEntry row to the `audit_events` table.

    `thread_id` and `pr_url` are taken from `state` so callers only build
    the entry itself.
    """
    await write_audit_event(
        thread_id=state["thread_id"],
        pr_url=state["pr_url"],
        entry=entry,
    )


async def node_fetch_pr(state: ReviewState) -> dict:
    console.print("[cyan]→ fetch_pr[/cyan]")
    t0 = time.monotonic()
    with console.status("[dim]Fetching PR from GitHub...[/dim]"):
        pr = fetch_pr(state["pr_url"])
    console.print(f"  [green]✓[/green] {len(pr.files_changed)} files, head {pr.head_sha[:7]}")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="fetch_pr",
        confidence=0.0,
        risk_level="med",
        decision="pending",
        reason=f"Fetched {len(pr.files_changed)} files, head={pr.head_sha[:7]}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {
        "pr_title": pr.title,
        "pr_diff": pr.diff,
        "pr_files": pr.files_changed,
        "pr_head_sha": pr.head_sha,
    }


async def node_analyze(state: ReviewState) -> dict:
    console.print("[cyan]→ analyze[/cyan]")
    t0 = time.monotonic()
    llm = get_llm()
    llm.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "600"))
    structured_llm = llm.with_structured_output(PRAnalysis)
    with console.status("[dim]LLM reviewing the diff...[/dim]"):
        a: PRAnalysis = await structured_llm.ainvoke([
            {
                "role": "system",
                "content": (
                    "You are a senior code reviewer. Analyze the pull request diff thoroughly. "
                    "Be concise: summary and reasoning each under 50 words, at most 4 comments of 30 words each.\n"
                    "Provide:\n"
                    "- summary: One-paragraph description of what the PR does\n"
                    "- comments: List of review comments (file, line, severity: nit/suggestion/issue/blocker, body)\n"
                    "- risk_factors: List of identified risks\n"
                    "- confidence: Self-reported confidence score between 0.0 and 1.0:\n"
                    "  * >= 0.73: Completely trivial (typo fixes, docs, pure one-line refactor) with 0 issues.\n"
                    "  * 0.58 - 0.72: Features, schema additions, or moderate changes that need human eyes (~65%).\n"
                    "  * < 0.58: Risky changes (security flaws, plaintext secrets, SQL injection, no tests) (< 58%).\n"
                    "- confidence_reasoning: Detailed explanation of why this confidence score was chosen\n"
                    "- escalation_questions: If confidence < 0.58, you MUST populate escalation_questions "
                    "with 2–4 specific, context-rich questions (referencing specific files, lines, or design choices in the diff)."
                ),
            },
            {"role": "user", "content": f"Title: {state['pr_title']}\nDiff:\n{state['pr_diff']}"},
        ])
    console.print(f"  [green]✓[/green] confidence={a.confidence:.0%}, {len(a.comments)} comment(s)")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="analyze",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="pending",
        reason=a.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"analysis": a}


async def node_route(state: ReviewState) -> dict:
    console.print("[cyan]→ route[/cyan]")
    t0 = time.monotonic()
    c = state["analysis"].confidence
    if c >= AUTO_APPROVE_THRESHOLD:
        decision = "auto_approve"
        audit_decision = "auto"
    elif c < ESCALATE_THRESHOLD:
        decision = "escalate"
        audit_decision = "escalate"
    else:
        decision = "human_approval"
        audit_decision = "pending"
    console.print(f"  [green]✓[/green] decision=[bold]{decision}[/bold] (confidence={c:.0%})")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="route",
        confidence=c,
        risk_level=risk_level_for(c),
        reviewer_id=None,
        decision=audit_decision,
        reason=f"Confidence {c:.0%} routed to {decision}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"decision": decision}


async def node_pre_human_approval(state: ReviewState) -> dict:
    """Audit node before interrupt to record pending state without duplicate entries on resume."""
    t0 = time.monotonic()
    a = state["analysis"]
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="human_approval",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="pending",
        reason=a.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {}


async def node_human_approval(state: ReviewState) -> dict:
    t0 = time.monotonic()
    a = state["analysis"]

    resp = interrupt({
        "kind": "approval_request",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "comments": [c.model_dump() for c in a.comments],
        "diff_preview": state["pr_diff"][:2000],
        "comment_body": _render_comment_body(state),
    })

    choice = validate_approval_response(resp)
    feedback = resp.get("feedback")
    reviewer = os.environ.get("GITHUB_USER")

    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="human_approval",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer,
        decision=choice,
        reason=feedback or f"Reviewer decided: {choice}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))

    return {
        "human_choice": choice,
        "human_feedback": feedback,
        "posted_comment_body": resp.get("edited_body"),
    }


def _render_comment_body(state: ReviewState) -> str:
    if state.get("posted_comment_body"):
        return state["posted_comment_body"]
    a = state["analysis"]
    lines = [f"### Automated review (confidence {a.confidence:.0%})", "", a.summary, ""]
    for c in a.comments:
        lines.append(f"- **[{c.severity}]** `{c.file}:{c.line or '?'}` — {c.body}")
    if state.get("human_feedback"):
        lines.append(f"\n_Reviewer note: {state['human_feedback']}_")
    if state.get("escalation_answers"):
        lines.append("\n_Reviewer answered escalation questions:_")
        for q, ans in state["escalation_answers"].items():
            lines.append(f"> **{q}** {ans}")
    return "\n".join(lines)


def _post(state: ReviewState) -> str:
    try:
        post_review_comment(state["pr_url"], _render_comment_body(state))
        console.print(f"  [green]✓[/green] posted comment to {state['pr_url']}")
        return "committed"
    except Exception as e:
        console.print(f"  [red]✗[/red] post failed: {e}")
        return "commit_failed"


async def node_commit(state: ReviewState) -> dict:
    console.print("[cyan]→ commit[/cyan]")
    t0 = time.monotonic()
    a = state.get("analysis")
    conf = a.confidence if a else 0.0
    risk = risk_level_for(conf)
    reviewer = os.environ.get("GITHUB_USER")

    choice = state.get("human_choice")
    escalation_ready = bool(state.get("escalation_answers"))
    if choice == "reject" or (choice not in {"approve", "edit"} and (choice is not None or not escalation_ready)):
        console.print(f"  [yellow]·[/yellow] skipping comment (choice={state.get('human_choice')})")
        action = "rejected"
        await audit(state, AuditEntry(
            agent_id=AGENT_ID,
            action="commit",
            confidence=conf,
            risk_level=risk,
            reviewer_id=reviewer,
            decision="reject",
            reason=state.get("human_feedback") or "Review rejected by human reviewer",
            execution_time_ms=int((time.monotonic() - t0) * 1000),
        ))
        return {"final_action": action}

    post_res = _post(state)
    if post_res == "committed":
        action = "committed_after_escalation" if state.get("escalation_answers") else "committed"
        decision = state.get("human_choice") or "approve"
        reason = state.get("human_feedback") or "Review committed to GitHub"
    else:
        action = "commit_failed"
        decision = "failed"
        reason = "Failed to post comment to GitHub"

    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="commit",
        confidence=conf,
        risk_level=risk,
        reviewer_id=reviewer if choice or escalation_ready else None,
        decision=decision,
        reason=reason,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": action}


async def node_auto_approve(state: ReviewState) -> dict:
    console.print("[cyan]→ auto_approve[/cyan]  [dim]high confidence — posting directly[/dim]")
    t0 = time.monotonic()
    a = state["analysis"]
    post_res = _post(state)
    action = f"auto_{post_res}"
    is_success = (post_res == "committed")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="auto_approve",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="auto" if is_success else "failed",
        reason="Auto-approved and committed review" if is_success else "Auto-approval comment post failed",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"final_action": action}


async def node_pre_escalate(state: ReviewState) -> dict:
    """Audit node before escalation interrupt to record pending state."""
    t0 = time.monotonic()
    a = state["analysis"]
    questions = a.escalation_questions or ["What is the intent of this PR?"]
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="escalate",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="pending",
        reason=f"Awaiting answers to {len(questions)} question(s)",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {}


async def node_escalate(state: ReviewState) -> dict:
    t0 = time.monotonic()
    a = state["analysis"]
    questions = a.escalation_questions or ["What is the intent of this PR?"]

    answers = interrupt({
        "kind": "escalation",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "risk_factors": a.risk_factors,
        "questions": questions,
    })

    reviewer = os.environ.get("GITHUB_USER")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="escalate",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer,
        decision="escalate",
        reason="\n".join(f"Q: {q}\nA: {answer}" for q, answer in answers.items()),
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"escalation_answers": answers}


async def node_synthesize(state: ReviewState) -> dict:
    console.print("[cyan]→ synthesize[/cyan]")
    t0 = time.monotonic()
    qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in (state.get("escalation_answers") or {}).items())
    llm = get_llm()
    llm.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "600"))
    structured_llm = llm.with_structured_output(PRAnalysis)
    with console.status("[dim]LLM refining review with reviewer answers...[/dim]"):
        refined: PRAnalysis = await structured_llm.ainvoke([
            {
                "role": "system",
                "content": (
                    "You are a senior code reviewer. You previously reviewed a pull request and escalated specific questions to a human reviewer.\n"
                    "The human reviewer has now provided answers.\n"
                    "Synthesize an updated, refined PR review. Be concise: summary and reasoning each under "
                    "50 words, at most 4 comments of 30 words each, short risk factors, no new escalation questions.\n"
                    "- Update summary and review comments based on human's clarifications.\n"
                    "- Important: Keep any unaddressed or unresolved risks in risk_factors and comments. Do NOT artificially inflate confidence if serious security or design risks remain.\n"
                    "- Re-evaluate confidence and provide confidence_reasoning."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Title: {state.get('pr_title', '')}\n"
                    f"Diff:\n{state.get('pr_diff', '')}\n\n"
                    f"Initial Summary: {state['analysis'].summary}\n"
                    f"Initial Risks: {state['analysis'].risk_factors}\n"
                    f"Initial Confidence: {state['analysis'].confidence:.0%}\n\n"
                    f"Reviewer Q&A:\n{qa}"
                ),
            },
        ])
    console.print(f"  [green]✓[/green] refined confidence={refined.confidence:.0%}")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="synthesize",
        confidence=refined.confidence,
        risk_level=risk_level_for(refined.confidence),
        reviewer_id=None,
        decision="escalate",
        reason=refined.confidence_reasoning,
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {"analysis": refined}


async def node_pre_confirm_synthesize(state: ReviewState) -> dict:
    """Audit node before post-synthesis confirmation interrupt (used in UI mode)."""
    t0 = time.monotonic()
    a = state["analysis"]
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="confirm_synthesize",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=None,
        decision="pending",
        reason="Awaiting final review confirmation after synthesis",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {}


async def node_confirm_synthesize(state: ReviewState) -> dict:
    """Interrupt to confirm the synthesized review before posting (used in UI mode)."""
    t0 = time.monotonic()
    a = state["analysis"]
    resp = interrupt({
        "kind": "synthesize_approval",
        "pr_url": state["pr_url"],
        "confidence": a.confidence,
        "confidence_reasoning": a.confidence_reasoning,
        "summary": a.summary,
        "comments": [c.model_dump() for c in a.comments],
        "diff_preview": state["pr_diff"][:2000],
        "risk_factors": a.risk_factors,
        "escalation_answers": state.get("escalation_answers"),
        "comment_body": _render_comment_body(state),
    })
    choice = validate_approval_response(resp)
    feedback = resp.get("feedback")
    reviewer = os.environ.get("GITHUB_USER")
    await audit(state, AuditEntry(
        agent_id=AGENT_ID,
        action="confirm_synthesize",
        confidence=a.confidence,
        risk_level=risk_level_for(a.confidence),
        reviewer_id=reviewer,
        decision=choice,
        reason=feedback or f"Post-synthesis confirmation: {choice}",
        execution_time_ms=int((time.monotonic() - t0) * 1000),
    ))
    return {
        "human_choice": choice,
        "human_feedback": feedback,
        "posted_comment_body": resp.get("edited_body"),
    }


def build_graph(checkpointer, confirm_after_synthesize: bool = False):
    """Build the review state graph.

    Args:
        checkpointer: AsyncSqliteSaver instance
        confirm_after_synthesize: If True, adds an interrupt node after synthesize
                                 for manual confirmation (used in UI mode).
    """
    g = StateGraph(ReviewState)
    nodes = [
        ("fetch_pr", node_fetch_pr),
        ("analyze", node_analyze),
        ("route", node_route),
        ("auto_approve", node_auto_approve),
        ("pre_human_approval", node_pre_human_approval),
        ("human_approval", node_human_approval),
        ("pre_escalate", node_pre_escalate),
        ("escalate", node_escalate),
        ("synthesize", node_synthesize),
        ("commit", node_commit),
    ]
    if confirm_after_synthesize:
        nodes.extend([
            ("pre_confirm_synthesize", node_pre_confirm_synthesize),
            ("confirm_synthesize", node_confirm_synthesize),
        ])

    for name, fn in nodes:
        g.add_node(name, fn)

    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "analyze")
    g.add_edge("analyze", "route")
    g.add_conditional_edges(
        "route", lambda s: s["decision"],
        {
            "auto_approve": "auto_approve",
            "human_approval": "pre_human_approval",
            "escalate": "pre_escalate",
        },
    )
    g.add_edge("auto_approve", END)
    g.add_edge("pre_human_approval", "human_approval")
    g.add_edge("human_approval", "commit")
    g.add_edge("commit", END)

    g.add_edge("pre_escalate", "escalate")
    g.add_edge("escalate", "synthesize")
    if confirm_after_synthesize:
        g.add_edge("synthesize", "pre_confirm_synthesize")
        g.add_edge("pre_confirm_synthesize", "confirm_synthesize")
        g.add_edge("confirm_synthesize", "commit")
    else:
        g.add_edge("synthesize", "commit")

    return g.compile(checkpointer=checkpointer)


def handle_interrupt(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind in ("approval_request", "synthesize_approval"):
        title = (
            f"Approval Request (conf={payload['confidence']:.0%})"
            if kind == "approval_request"
            else f"Confirm Synthesized Review (conf={payload['confidence']:.0%})"
        )
        console.print(Panel.fit(
            payload["summary"],
            title=title,
            border_style="green",
        ))
        for c in payload.get("comments", []):
            console.print(f"  [{c['severity']}] {c['file']}:{c.get('line') or '?'} — {c['body']}")
        if payload.get("diff_preview"):
            console.print("\n[dim]--- diff preview ---[/dim]")
            console.print(payload["diff_preview"])

        choice = ""
        while choice not in {"approve", "reject", "edit"}:
            choice = console.input("\n[bold]Choice (approve/reject/edit)?[/bold] ").strip().lower()

        if choice == "edit":
            console.print("[yellow]Edit mode: You can provide edited feedback or custom comment text.[/yellow]")
            feedback = console.input("Feedback / Edit note: ").strip()
            custom_body = console.input("Custom comment body (press Enter to keep default with note): ").strip()
            confirm = console.input("Confirm posting this review? (y/n): ").strip().lower()
            if confirm in ("y", "yes"):
                return {
                    "choice": "edit",
                    "feedback": feedback,
                    "edited_body": custom_body or payload.get("comment_body") or (
                        payload["summary"] + (f"\n\nReviewer note: {feedback}" if feedback else "")
                    ),
                }
            else:
                console.print("[red]Edit cancelled — treated as rejected.[/red]")
                return {"choice": "reject", "feedback": "Cancelled by user during edit"}

        feedback = console.input("Feedback: ").strip() if choice != "approve" else ""
        return {"choice": choice, "feedback": feedback}

    if kind == "escalation":
        console.print(Panel.fit(
            payload["summary"],
            title=f"Escalation (conf={payload['confidence']:.0%})",
            border_style="yellow",
        ))
        if payload.get("risk_factors"):
            console.print(f"[red]Risks:[/red] {', '.join(payload['risk_factors'])}")
        return {q: console.input(f"Q: {q}\nA: ").strip() for q in payload["questions"]}

    raise ValueError(f"Unknown interrupt kind: {kind}")


async def run(pr_url: str | None, thread_id: str | None):
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)

        if thread_id:
            cfg = {"configurable": {"thread_id": thread_id}}
            snapshot = await app.aget_state(cfg)
            if snapshot and snapshot.values:
                # Existing thread found in checkpoint
                current_pr = snapshot.values.get("pr_url") or pr_url
                console.rule(f"[bold]Exercise 4 — Resuming Thread[/bold]")
                console.print(f"[dim]PR: {current_pr}[/dim]")
                console.print(f"[dim]thread_id = {thread_id}[/dim]\n")

                if snapshot.next:
                    console.print(f"[yellow]Thread is paused at interrupt node: {snapshot.next}[/yellow]")
                    interrupt_payload = None
                    if snapshot.tasks:
                        for t in snapshot.tasks:
                            if t.interrupts:
                                interrupt_payload = t.interrupts[0].value
                                break
                    if interrupt_payload is not None:
                        answer = handle_interrupt(interrupt_payload)
                        result = await app.ainvoke(Command(resume=answer), cfg)
                        while "__interrupt__" in result:
                            payload = result["__interrupt__"][0].value
                            result = await app.ainvoke(Command(resume=handle_interrupt(payload)), cfg)

                        console.rule("Final")
                        console.print(f"final_action = {result.get('final_action')}")
                        console.print(f"\n[dim]Replay:[/dim] uv run python -m audit.replay --thread {thread_id}")
                        return
                    else:
                        console.print("[yellow]No pending interrupt found on paused thread.[/yellow]")
                        return
                else:
                    console.print("[green]Thread has already completed.[/green]")
                    console.print(f"final_action = {snapshot.values.get('final_action')}")
                    console.print(f"\n[dim]Replay:[/dim] uv run python -m audit.replay --thread {thread_id}")
                    return

        # Starting a new thread
        if not pr_url:
            console.print("[red]Error: --pr is required when starting a new session.[/red]")
            return

        thread_id = thread_id or str(uuid.uuid4())
        console.rule("[bold]Exercise 4 — SQLite audit trail[/bold]")
        console.print(f"[dim]PR: {pr_url}[/dim]")
        console.print(f"[dim]thread_id = {thread_id}[/dim]\n")

        cfg = {"configurable": {"thread_id": thread_id}}
        result = await app.ainvoke({"pr_url": pr_url, "thread_id": thread_id}, cfg)

        while "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            result = await app.ainvoke(Command(resume=handle_interrupt(payload)), cfg)

        console.rule("Final")
        console.print(f"final_action = {result.get('final_action')}")
        console.print(f"\n[dim]Replay:[/dim] uv run python -m audit.replay --thread {thread_id}")


def main():
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--pr", help="PR URL to review")
    p.add_argument("--thread", help="Resume an existing thread")
    args = p.parse_args()
    asyncio.run(run(args.pr, args.thread))


if __name__ == "__main__":
    main()
