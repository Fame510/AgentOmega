import json
import httpx
from typing import List
from app.models import Plan, PlanStep, Action
from app.config import settings

async def generate_plan(goal: str, current_url: str) -> Plan:
    """
    Generates a plan using an LLM; falls back to deterministic rules.
    """
    # 1) Try LLM
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            prompt = f"""
            You are a web automation planner. Given a goal and current URL, produce a JSON plan.
            Goal: {goal}
            Current URL: {current_url}
            Available actions: click, type, select, press, navigate, firecrawl.
            Output format: {{"steps": [{{"description": "...", "action": {{"type": "...", "target_id": "...", "value": "..."}}, "expected_condition": "..."}}]}}
            Only output valid JSON.
            """
            resp = await client.post(
                settings.PLANNER_LLM_URL,
                json={"model": settings.PLANNER_MODEL, "prompt": prompt, "stream": False}
            )
            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("response", "")
                json_str = raw.strip().strip("```json").strip("```").strip()
                plan_dict = json.loads(json_str)
                return Plan(**plan_dict)
    except Exception as e:
        print(f"[Planner] LLM unavailable: {e}")

    # 2) Deterministic fallback – handle Firecrawl intent
    if "scrape" in goal.lower() or "extract" in goal.lower():
        return Plan(steps=[
            PlanStep(
                description="Use Firecrawl to extract data",
                action=Action(type="firecrawl", value=goal),
                expected_condition="data returned"
            )
        ])

    # Generic fallback: click first visible interactive element
    return Plan(steps=[
        PlanStep(
            description="Click first visible interactive element",
            action=Action(type="click"),
            expected_condition="DOM changed"
        )
    ])
