"""
AgentOmega hardened engine
==========================
The Aeon_Dux browser runtime fused with the SHACKLE governor. Every browser
action is gated by SHACKLE decide() BEFORE execution:

  ALLOW -> execute, then commit_allow() + account(cost) + audit execution
  DENY  -> trip circuit breaker, audit, abort the run
  HITL  -> emit prompt, await operator decision with a BOUNDED timeout
           (timeout -> safe DENY; the worker can never hang forever)

This is the real-time showcase of SHACKLE governing a live autonomous agent.
"""

import asyncio
import base64
import json
import traceback
from typing import List, Optional, Dict

import httpx
from playwright.async_api import async_playwright, Page, Frame, BrowserContext

from app.config import settings
from app.models import InteractiveElement, Action, PlanStep, Plan
from app.planner import generate_plan
from app.shackle import (
    GuardConfig, HitlMode, Verdict, AuditLedger, ShackleGovernor,
)

if settings.USE_REDIS_SESSION:
    import redis.asyncio as redis
    redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
else:
    redis_client = None


# Cost model: rough per-action estimate so budget guard is meaningful even
# without an upstream cost signal. navigate/click/type are cheap; VLM + firecrawl
# cost more because they call external models/services.
_ACTION_COST_USD = {
    "navigate": 0.0005,
    "click": 0.0005,
    "type": 0.0005,
    "press": 0.0002,
    "select": 0.0005,
    "firecrawl": 0.01,
    "vlm": 0.02,
}


def build_guard_config() -> GuardConfig:
    """Assemble the SHACKLE guard policy from settings."""
    return GuardConfig(
        budget_usd=settings.SHACKLE_BUDGET_USD,
        max_repeat_calls=settings.SHACKLE_MAX_REPEAT_CALLS,
        error_amplification=settings.SHACKLE_ERROR_AMPLIFICATION,
        max_total_calls=settings.SHACKLE_MAX_TOTAL_CALLS,
        hitl_mode=HitlMode(settings.SHACKLE_HITL_MODE),
        hitl_budget_threshold=settings.SHACKLE_HITL_BUDGET_THRESHOLD,
    )


# One shared ledger process-wide (append-only, chained). Sessions write to it
# with their own session_id; verify_chain() covers the whole ledger.
_LEDGER: Optional[AuditLedger] = None


def get_ledger() -> AuditLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = AuditLedger(
            settings.SHACKLE_AUDIT_PATH,
            signing_key_hex=settings.SHACKLE_SIGNING_KEY_HEX or None,
        )
    return _LEDGER


class HardenedAgentEngine:
    def __init__(self, goal: str, session_id: str, websocket_cb=None):
        self.goal = goal
        self.session_id = session_id
        self.websocket_cb = websocket_cb
        self.state = "INITIALIZING"
        self.context: Optional[BrowserContext] = None
        self.current_plan: Optional[Plan] = None
        self.step_index = 0
        self._stop_requested = False

        # SHACKLE governor for this session
        self.governor = ShackleGovernor(session_id, build_guard_config(), get_ledger())

        # HITL coordination
        self._hitl_event: Optional[asyncio.Event] = None
        self._hitl_decision: Optional[str] = None

    async def emit(self, msg: str, stage: str = "INFO", payload: Dict = None):
        if self.websocket_cb:
            await self.websocket_cb({
                "session_id": self.session_id,
                "stage": stage,
                "message": msg,
                "payload": payload or {},
            })

    # ------------------------------------------------------------------
    # HITL: operator responds via the WS with APPROVE / SKIP / ABORT
    # ------------------------------------------------------------------
    def resolve_hitl(self, decision: str) -> None:
        """Called by the server when an HITL_RESPONSE arrives."""
        self._hitl_decision = (decision or "").upper()
        if self._hitl_event is not None:
            self._hitl_event.set()

    async def _await_hitl(self, prompt: str) -> str:
        """Bounded-wait for an operator decision. On timeout -> ABORT (safe).

        This is the fix for the unbounded-await hang: a disconnected or idle
        operator can never freeze the worker; the guard fails closed.
        """
        self._hitl_event = asyncio.Event()
        self._hitl_decision = None
        await self.emit(prompt, "HITL_REQUIRED",
                        {"timeout_s": settings.SHACKLE_HITL_TIMEOUT_S})
        try:
            await asyncio.wait_for(self._hitl_event.wait(),
                                   timeout=settings.SHACKLE_HITL_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.emit("HITL timed out - failing closed (ABORT).", "HITL_TIMEOUT")
            return "ABORT"
        finally:
            self._hitl_event = None
        return self._hitl_decision or "ABORT"

    # ------------------------------------------------------------------
    # SHACKLE gate around a single action
    # ------------------------------------------------------------------
    async def _gate_action(self, action: Action) -> bool:
        """Return True if the action may execute, False if blocked.

        Handles ALLOW/DENY/HITL and performs auditing + state transitions.
        """
        params = {
            "type": action.type,
            "target_id": action.target_id,
            "value": action.value,
            "url": action.url,
            "viewport_x": action.viewport_x,
            "viewport_y": action.viewport_y,
        }
        est_cost = _ACTION_COST_USD.get(action.type, 0.001)
        decision = await self.governor.gate(action.type, params, est_cost)

        if decision.verdict == Verdict.ALLOW:
            self.governor.commit_allow()
            return True

        if decision.verdict == Verdict.HITL:
            operator = await self._await_hitl(
                f"Approval required for '{action.type}': {decision.human_readable}")
            if operator == "APPROVE":
                self.governor.commit_allow()
                await self.governor.ledger.log_decision(
                    self.session_id, action.type, "HITL_APPROVED", "operator approved")
                await self.emit("Operator approved action.", "HITL_APPROVED")
                return True
            if operator == "SKIP":
                await self.governor.ledger.log_decision(
                    self.session_id, action.type, "HITL_SKIPPED", "operator skipped")
                await self.emit("Operator skipped action.", "HITL_SKIPPED")
                return False
            # ABORT (or timeout)
            self.governor.trip("HITL aborted by operator/timeout")
            await self.governor.ledger.log_decision(
                self.session_id, action.type, "HITL_ABORTED", "operator/timeout abort")
            await self.emit("Operator aborted - circuit tripped.", "HITL_ABORTED")
            return False

        # DENY
        self.governor.trip(decision.human_readable)
        await self.emit(
            f"SHACKLE DENY [{decision.deny_reason.value}]: {decision.human_readable}",
            "SHACKLE_DENY")
        return False

    # ------------------------------------------------------------------
    # Session persistence (JSON via Redis, optional)
    # ------------------------------------------------------------------
    async def _save_session(self):
        if redis_client and self.context:
            storage = await self.context.storage_state()
            await redis_client.set(f"session:{self.session_id}", json.dumps(storage))

    async def _load_session(self) -> Optional[dict]:
        if redis_client:
            data = await redis_client.get(f"session:{self.session_id}")
            if data:
                return json.loads(data)
        return None

    # ------------------------------------------------------------------
    # DOM distillation (unchanged Aeon_Dux logic)
    # ------------------------------------------------------------------
    async def distill_dom(self, page: Page) -> List[InteractiveElement]:
        all_elements: List[InteractiveElement] = []

        async def extract_from_frame(frame: Frame, offset_x: float = 0.0, offset_y: float = 0.0):
            js_script = """
            (() => {
                const elements = [];
                function traverse(node) {
                    if (!node) return;
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        const tag = node.tagName.toLowerCase();
                        const isInteractive = ['button','input','a','select','textarea'].includes(tag) ||
                                              node.getAttribute('role') === 'button' ||
                                              node.getAttribute('contenteditable') === 'true';
                        if (isInteractive) {
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            if (rect.width > 0 && rect.height > 0 &&
                                style.visibility !== 'hidden' && style.display !== 'none') {
                                let cid = node.getAttribute('data-compounded-id');
                                if (!cid) {
                                    cid = 'cid-' + Math.random().toString(36).substring(2, 11);
                                    node.setAttribute('data-compounded-id', cid);
                                }
                                elements.push({
                                    id: cid, tag: tag,
                                    text: node.innerText?.trim() || node.placeholder || node.getAttribute('aria-label') || '',
                                    type: node.type || '',
                                    viewport_x: rect.left + rect.width/2,
                                    viewport_y: rect.top + rect.height/2,
                                    page_x: rect.left + window.scrollX + rect.width/2,
                                    page_y: rect.top + window.scrollY + rect.height/2
                                });
                            }
                        }
                        if (node.shadowRoot) { traverse(node.shadowRoot); }
                    }
                    let child = node.firstChild;
                    while (child) { traverse(child); child = child.nextSibling; }
                }
                traverse(document.body);
                return elements;
            })()
            """
            try:
                raw_elements = await frame.evaluate(js_script)
            except Exception:
                return
            for el in raw_elements:
                all_elements.append(InteractiveElement(
                    id=el["id"], tag=el["tag"], text=el["text"], type=el["type"],
                    page_x=el["page_x"] + offset_x, page_y=el["page_y"] + offset_y,
                    viewport_x=el["viewport_x"] + offset_x, viewport_y=el["viewport_y"] + offset_y,
                    frame_path=[frame.url] if frame != page.main_frame else []))

        await extract_from_frame(page.main_frame)

        async def process_frames(frame: Frame, ox: float = 0.0, oy: float = 0.0):
            for child_frame in frame.child_frames:
                iframe_el = await child_frame.frame_element()
                if iframe_el:
                    box = await iframe_el.bounding_box()
                    if box:
                        await extract_from_frame(child_frame, box["x"], box["y"])
                        await process_frames(child_frame, box["x"], box["y"])

        await process_frames(page.main_frame)
        return all_elements

    # ------------------------------------------------------------------
    # Action execution (unchanged) -- only reached AFTER SHACKLE ALLOW
    # ------------------------------------------------------------------
    async def execute_action(self, page: Page, action: Action, step: PlanStep) -> bool:
        for attempt in range(settings.MAX_RETRIES_PER_ACTION):
            try:
                if action.type == "navigate":
                    await page.goto(action.url, wait_until="domcontentloaded")
                elif action.type == "click":
                    if action.target_id:
                        await page.locator(f"[data-compounded-id='{action.target_id}']").click(
                            timeout=settings.DEFAULT_TIMEOUT_MS)
                    elif action.viewport_x is not None and action.viewport_y is not None:
                        await page.mouse.click(action.viewport_x, action.viewport_y)
                    else:
                        raise ValueError("Click needs target_id or viewport coords")
                elif action.type == "type":
                    if action.target_id:
                        await page.locator(f"[data-compounded-id='{action.target_id}']").fill(
                            action.value, timeout=settings.DEFAULT_TIMEOUT_MS)
                    elif action.viewport_x is not None and action.viewport_y is not None:
                        await page.mouse.click(action.viewport_x, action.viewport_y)
                        await page.keyboard.type(action.value)
                elif action.type == "press":
                    await page.keyboard.press(action.value)
                elif action.type == "select":
                    if action.target_id:
                        await page.locator(f"[data-compounded-id='{action.target_id}']").select_option(
                            action.value, timeout=settings.DEFAULT_TIMEOUT_MS)
                elif action.type == "firecrawl":
                    await self._firecrawl_action(action, page)
                await self._verify_condition(page, step.expected_condition)
                return True
            except Exception as e:
                await self.emit(f"Action failed (attempt {attempt+1}): {e}", "WARN")
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

    async def _verify_condition(self, page: Page, condition: str):
        if not condition:
            await page.wait_for_load_state("load", timeout=settings.DEFAULT_TIMEOUT_MS)
        elif "url contains" in condition:
            substr = condition.split("url contains")[-1].strip().strip("'\"")
            await page.wait_for_url(f"**{substr}**", timeout=settings.DEFAULT_TIMEOUT_MS)
        elif "element visible" in condition:
            await page.wait_for_function("""
                async () => {
                    let lastCount = -1;
                    for (let i = 0; i < 3; i++) {
                        const count = document.querySelectorAll('button, input, a, [role="button"]').length;
                        if (count === lastCount && count > 0) return true;
                        lastCount = count;
                        await new Promise(resolve => setTimeout(resolve, 200));
                    }
                    return false;
                }
            """, timeout=settings.DEFAULT_TIMEOUT_MS)
        else:
            await page.wait_for_load_state("load", timeout=settings.DEFAULT_TIMEOUT_MS)

    async def _firecrawl_action(self, action: Action, page: Page):
        if not settings.FIRECRAWL_API_KEY:
            raise ValueError("FIRECRAWL_API_KEY not set")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.FIRECRAWL_API_URL}/scrape",
                headers={"Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}"},
                json={"url": page.url})
            if resp.status_code == 200:
                data = resp.json()
                await self.emit(f"Firecrawl result: {data.get('content', '')[:200]}...", "EXECUTION")
            else:
                raise Exception(f"Firecrawl error: {resp.text}")

    async def vlm_fallback(self, page: Page, anomaly: str) -> Action:
        await self.emit(f"Anomaly: {anomaly}. Invoking Vision Model...", "VISION_FALLBACK")
        screenshot_bytes = await page.screenshot(type="jpeg", quality=80, full_page=False)
        b64_img = base64.b64encode(screenshot_bytes).decode("utf-8")
        prompt = (f"Goal: {self.goal}\nAnomaly: {anomaly}\n"
                  "Return viewport coordinates (x, y) of the element to click. "
                  "JSON: {'x': int, 'y': int}")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(settings.VLM_URL, json={
                    "model": settings.VLM_MODEL, "prompt": prompt,
                    "images": [b64_img], "stream": False, "format": "json"})
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("response", "{}")
                    coords = json.loads(raw.strip().strip("```json").strip("```").strip())
                    return Action(type="click", viewport_x=coords.get("x", 0),
                                  viewport_y=coords.get("y", 0))
        except Exception as e:
            await self.emit(f"VLM error: {e}", "ERROR")
        return Action(type="click", viewport_x=640, viewport_y=360)

    # ------------------------------------------------------------------
    # Main orchestration loop -- SHACKLE gates every action
    # ------------------------------------------------------------------
    async def orchestrate(self):
        async with async_playwright() as p:
            try:
                await self.emit("Launching browser...", "SYSTEM")
                await self.emit(f"SHACKLE governor active: {self.governor.snapshot()}", "SHACKLE")
                user_data_dir = f"./data/sessions/{self.session_id}"
                context_options = {
                    "viewport": {"width": settings.VIEWPORT_WIDTH, "height": settings.VIEWPORT_HEIGHT},
                    "user_agent": settings.USER_AGENT,
                }
                if settings.USE_REDIS_SESSION:
                    stored = await self._load_session()
                    if stored:
                        context_options["storage_state"] = stored

                self.context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir, headless=settings.HEADLESS,
                    **context_options,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                          "--use-fake-ui-for-media-stream",
                          "--disable-features=IsolateOrigins,site-per-process"])

                await self.context.add_init_script("""
                    (() => {
                        const originalAttachShadow = Element.prototype.attachShadow;
                        Element.prototype.attachShadow = function(init) {
                            if (init && init.mode === 'closed') { init.mode = 'open'; }
                            return originalAttachShadow.call(this, init);
                        };
                    })();
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    window.chrome = { runtime: {} };
                """)

                async def on_new_page(new_page):
                    await new_page.wait_for_load_state("domcontentloaded")
                    url = new_page.url
                    if "oauth" in url.lower() or "login" in url.lower():
                        await self.emit(f"OAuth page detected: {url}", "AUTH")
                        await new_page.wait_for_event("close", timeout=120000)
                        await self.emit("OAuth flow completed.", "AUTH")

                self.context.on("page", on_new_page)
                page = await self.context.new_page()
                await page.goto("https://www.google.com", wait_until="domcontentloaded")

                self.current_plan = await generate_plan(self.goal, page.url)
                await self.emit(f"Plan generated: {len(self.current_plan.steps)} steps", "PLAN")

                for idx, step in enumerate(self.current_plan.steps):
                    if self._stop_requested or self.governor.state.circuit_tripped:
                        await self.emit("Halting (stop requested or circuit tripped).", "SYSTEM")
                        break
                    self.step_index = idx
                    await self.emit(f"Step {idx+1}: {step.description}", "THINKING", {"step": idx+1})

                    elements = await self.distill_dom(page)
                    action = step.action
                    if action.target_id and not any(e.id == action.target_id for e in elements):
                        await self.emit(f"Target '{action.target_id}' not found. VLM fallback...", "WARN")
                        action = await self.vlm_fallback(page, "target missing")

                    # SHACKLE GATE (pre-execution authority boundary)
                    if not await self._gate_action(action):
                        await self.emit(f"Step {idx+1} blocked by SHACKLE.", "SHACKLE_BLOCK")
                        break

                    success = await self.execute_action(page, action, step)
                    await self.governor.account(_ACTION_COST_USD.get(action.type, 0.001))
                    await self.governor.ledger.log_execution(
                        self.session_id, action.type, ok=success,
                        cost_usd=_ACTION_COST_USD.get(action.type, 0.001))

                    if not success:
                        await self.emit(f"Step {idx+1} failed. Trying VLM recovery...", "RECOVERY")
                        vlm_action = await self.vlm_fallback(page, "execution failed")
                        if not await self._gate_action(vlm_action):
                            await self.emit("VLM recovery blocked by SHACKLE.", "SHACKLE_BLOCK")
                            break
                        success = await self.execute_action(page, vlm_action, step)
                        await self.governor.account(_ACTION_COST_USD.get("vlm", 0.02))
                        await self.governor.ledger.log_execution(
                            self.session_id, "vlm", ok=success, cost_usd=_ACTION_COST_USD["vlm"])
                        if not success:
                            await self.emit(f"Step {idx+1} permanently failed.", "FATAL_ERROR")
                            break

                    await self.emit(f"Step {idx+1} completed. {self.governor.snapshot()}", "EXECUTION")
                    await asyncio.sleep(0.5)
                    if settings.USE_REDIS_SESSION:
                        await self._save_session()

                await self.emit("Workflow finished.", "SYSTEM")

            except asyncio.CancelledError:
                await self.emit("Workflow cancelled.", "SYSTEM")
                raise
            except Exception:
                await self.emit(f"Critical error: {traceback.format_exc()}", "FATAL_ERROR")
            finally:
                if self.context:
                    await self.context.close()
                await self.emit("Browser resources released.", "SYSTEM_SHUTDOWN")

    async def stop(self):
        self._stop_requested = True
        # Unblock any pending HITL wait so the worker can exit promptly.
        self.resolve_hitl("ABORT")
