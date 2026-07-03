import asyncio
import base64
import json
import traceback
import uuid
import httpx
from typing import List, Optional, Dict
from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Frame

from app.config import settings
from app.models import InteractiveElement, Action, PlanStep, Plan
from app.planner import generate_plan
from app.agents import AgentSpec, registry
from app.shackle import (
    Decision, GuardConfig, HitlMode, SessionState, ToolCall, Verdict,
    apply_allow, apply_deny, apply_post_exec, decide, hash_params, ledger,
)

COST_PER_ACTION_USD = 0.005

# Optional Redis integration
if settings.USE_REDIS_SESSION:
    import redis.asyncio as redis
    redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
else:
    redis_client = None

class HardenedAgentEngine:
    def __init__(self, goal: str, session_id: str, websocket_cb=None,
                 agent_spec: Optional[AgentSpec] = None):
        self.goal = goal
        self.session_id = session_id
        self.websocket_cb = websocket_cb
        self.state = "INITIALIZING"
        self.context: Optional[BrowserContext] = None
        self.current_plan: Optional[Plan] = None
        self.step_index = 0
        self._stop_requested = False
        self._nonce = 0

        self.agent_spec = agent_spec
        policy = agent_spec.policy if agent_spec else None
        self.guard_config = GuardConfig(
            budget_usd=policy.budget_usd if policy else 0.25,
            max_repeat_calls=policy.max_repeat_calls if policy else 3,
            error_amplification=policy.error_amplification if policy else True,
            timeout_seconds=policy.timeout_seconds if policy else 180,
            max_total_calls=policy.max_total_calls if policy else 50,
            hitl_mode=HitlMode(policy.hitl_mode) if policy else HitlMode.ON_DENY,
            hitl_budget_threshold=policy.hitl_budget_threshold if policy else 0.2,
        )
        self.guard_state = SessionState(
            session_id=session_id,
            agent_id=agent_spec.id if agent_spec else "adhoc",
            budget_initial_usd=self.guard_config.budget_usd,
            budget_remaining_usd=self.guard_config.budget_usd,
        )
        self._hitl_event = asyncio.Event()
        self._hitl_response: Optional[str] = None

    def submit_hitl_response(self, choice: str):
        """Called by the server when the operator answers a HITL prompt."""
        self._hitl_response = choice
        self._hitl_event.set()

    def governance_snapshot(self) -> Dict:
        s = self.guard_state
        return {
            "agent_id": s.agent_id,
            "budget_initial": s.budget_initial_usd,
            "budget_spent": round(s.budget_spent_usd, 6),
            "budget_remaining": round(s.budget_remaining_usd, 6),
            "total_calls": s.total_calls,
            "circuit_tripped": s.circuit_tripped,
            "trip_reason": s.circuit_trip_reason,
        }

    async def _gate(self, action: Action, step: PlanStep) -> bool:
        """SHACKLE pre-execution gate. Returns True if the action may run."""
        self._nonce += 1
        params = action.model_dump(exclude_none=True)
        call = ToolCall(
            tool_name=action.type,
            tool_params_hash=hash_params(params),
            estimated_cost_usd=COST_PER_ACTION_USD,
            nonce=self._nonce,
            tool_params_raw=json.dumps(params),
        )
        decision = decide(self.guard_state, call, self.guard_config)

        ledger.append({
            "session_id": self.session_id,
            "agent_id": self.guard_state.agent_id,
            "tool": call.tool_name,
            "verdict": decision.verdict.value,
            "reason": decision.human_readable,
            "budget_remaining": round(self.guard_state.budget_remaining_usd, 6),
            "total_calls": self.guard_state.total_calls,
        })
        await self.emit(
            f"{decision.verdict.value}: {decision.human_readable}",
            "GOVERNOR",
            {"verdict": decision.verdict.value, "governance": self.governance_snapshot()},
        )

        if decision.verdict == Verdict.HITL:
            self._hitl_event.clear()
            self._hitl_response = None
            await self.emit(
                f"Human approval required — {decision.human_readable}",
                "HITL",
                {"tool": call.tool_name, "params": params,
                 "governance": self.governance_snapshot()},
            )
            await self._hitl_event.wait()
            choice = self._hitl_response or "abort"
            ledger.append({
                "session_id": self.session_id,
                "agent_id": self.guard_state.agent_id,
                "tool": call.tool_name,
                "verdict": f"HITL_{choice.upper()}",
                "reason": "Operator decision",
                "budget_remaining": round(self.guard_state.budget_remaining_usd, 6),
                "total_calls": self.guard_state.total_calls,
            })
            if choice == "approve":
                apply_allow(self.guard_state, call)
                apply_post_exec(self.guard_state, COST_PER_ACTION_USD)
                return True
            if choice == "skip":
                return False
            apply_deny(self.guard_state, "Operator abort via HITL")
            self._stop_requested = True
            return False

        if decision.verdict == Verdict.DENY:
            apply_deny(self.guard_state, decision.human_readable)
            self._stop_requested = True
            await self.emit(
                f"Circuit breaker TRIPPED: {decision.human_readable}",
                "CIRCUIT_TRIPPED",
                {"governance": self.governance_snapshot()},
            )
            return False

        apply_allow(self.guard_state, call)
        apply_post_exec(self.guard_state, COST_PER_ACTION_USD)
        return True

    async def emit(self, msg: str, stage: str = "INFO", payload: Dict = None):
        if self.websocket_cb:
            await self.websocket_cb({
                "session_id": self.session_id,
                "stage": stage,
                "message": msg,
                "payload": payload or {}
            })

    # ------------------------------------------------------------
    # Session persistence (JSON)
    # ------------------------------------------------------------
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

    # ------------------------------------------------------------
    # DOM distillation – single JS pass per frame
    # ------------------------------------------------------------
    async def distill_dom(self, page: Page) -> List[InteractiveElement]:
        """
        Runs the entire traversal inside the browser, returning JSON.
        Handles open & closed shadow roots (via init script), and iframes.
        """
        all_elements = []

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
                                    id: cid,
                                    tag: tag,
                                    text: node.innerText?.trim() || node.placeholder || node.getAttribute('aria-label') || '',
                                    type: node.type || '',
                                    viewport_x: rect.left + rect.width/2,
                                    viewport_y: rect.top + rect.height/2,
                                    page_x: rect.left + window.scrollX + rect.width/2,
                                    page_y: rect.top + window.scrollY + rect.height/2
                                });
                            }
                        }
                        if (node.shadowRoot) {
                            traverse(node.shadowRoot);
                        }
                    }
                    let child = node.firstChild;
                    while (child) {
                        traverse(child);
                        child = child.nextSibling;
                    }
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
                    id=el["id"],
                    tag=el["tag"],
                    text=el["text"],
                    type=el["type"],
                    page_x=el["page_x"] + offset_x,
                    page_y=el["page_y"] + offset_y,
                    viewport_x=el["viewport_x"] + offset_x,
                    viewport_y=el["viewport_y"] + offset_y,
                    frame_path=[frame.url] if frame != page.main_frame else []
                ))

        # Main frame
        await extract_from_frame(page.main_frame)

        # Recursively process child frames – use box coordinates directly (they are viewport-relative)
        async def process_frames(frame: Frame, parent_offset_x: float = 0.0, parent_offset_y: float = 0.0):
            for child_frame in frame.child_frames:
                iframe_el = await child_frame.frame_element()
                if iframe_el:
                    box = await iframe_el.bounding_box()
                    if box:
                        # Use absolute coordinates directly as they are viewport-relative
                        await extract_from_frame(child_frame, box["x"], box["y"])
                        await process_frames(child_frame, box["x"], box["y"])

        await process_frames(page.main_frame)

        return all_elements

    # ------------------------------------------------------------
    # Action execution with retries and verification
    # ------------------------------------------------------------
    async def execute_action(self, page: Page, action: Action, step: PlanStep) -> bool:
        for attempt in range(settings.MAX_RETRIES_PER_ACTION):
            try:
                if action.type == "navigate":
                    await page.goto(action.url, wait_until="domcontentloaded")
                elif action.type == "click":
                    if action.target_id:
                        loc = page.locator(f"[data-compounded-id='{action.target_id}']")
                        await loc.click(timeout=settings.DEFAULT_TIMEOUT_MS)
                    elif action.viewport_x is not None and action.viewport_y is not None:
                        await page.mouse.click(action.viewport_x, action.viewport_y)
                    else:
                        raise ValueError("Click needs target_id or viewport coords")
                elif action.type == "type":
                    if action.target_id:
                        loc = page.locator(f"[data-compounded-id='{action.target_id}']")
                        await loc.fill(action.value, timeout=settings.DEFAULT_TIMEOUT_MS)
                    else:
                        if action.viewport_x is not None and action.viewport_y is not None:
                            await page.mouse.click(action.viewport_x, action.viewport_y)
                            await page.keyboard.type(action.value)
                elif action.type == "press":
                    await page.keyboard.press(action.value)
                elif action.type == "select":
                    if action.target_id:
                        loc = page.locator(f"[data-compounded-id='{action.target_id}']")
                        await loc.select_option(action.value, timeout=settings.DEFAULT_TIMEOUT_MS)
                elif action.type == "firecrawl":
                    await self._firecrawl_action(action, page)

                # Verification
                await self._verify_condition(page, step.expected_condition)
                return True

            except Exception as e:
                await self.emit(f"Action failed (attempt {attempt+1}): {e}", "WARN")
                await asyncio.sleep(0.5 * (attempt + 1))

        return False

    async def _verify_condition(self, page: Page, condition: str):
        if "url contains" in condition:
            substr = condition.split("url contains")[-1].strip().strip("'\"")
            await page.wait_for_url(f"**{substr}**", timeout=settings.DEFAULT_TIMEOUT_MS)
        elif "element visible" in condition:
            # Async DOM stability check – non‑blocking
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
            # Fallback: wait for load event only
            await page.wait_for_load_state("load", timeout=settings.DEFAULT_TIMEOUT_MS)

    # ------------------------------------------------------------
    # Firecrawl integration
    # ------------------------------------------------------------
    async def _firecrawl_action(self, action: Action, page: Page):
        if not settings.FIRECRAWL_API_KEY:
            raise ValueError("FIRECRAWL_API_KEY not set")
        # Example: scrape current page content
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.FIRECRAWL_API_URL}/scrape",
                headers={"Authorization": f"Bearer {settings.FIRECRAWL_API_KEY}"},
                json={"url": page.url}
            )
            if resp.status_code == 200:
                data = resp.json()
                await self.emit(f"Firecrawl result: {data.get('content', '')[:200]}...", "EXECUTION")
            else:
                raise Exception(f"Firecrawl error: {resp.text}")

    # ------------------------------------------------------------
    # VLM fallback
    # ------------------------------------------------------------
    async def vlm_fallback(self, page: Page, anomaly: str) -> Action:
        await self.emit(f"Anomaly: {anomaly}. Invoking Vision Model...", "VISION_FALLBACK")
        screenshot_bytes = await page.screenshot(type="jpeg", quality=80, full_page=False)
        b64_img = base64.b64encode(screenshot_bytes).decode("utf-8")

        prompt = f"Goal: {self.goal}\nAnomaly: {anomaly}\nReturn viewport coordinates (x, y) of the element to click. JSON: {{'x': int, 'y': int}}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    settings.VLM_URL,
                    json={
                        "model": settings.VLM_MODEL,
                        "prompt": prompt,
                        "images": [b64_img],
                        "stream": False,
                        "format": "json"
                    }
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("response", "{}")
                    coords = json.loads(raw.strip().strip("```json").strip("```").strip())
                    return Action(type="click", viewport_x=coords.get("x", 0), viewport_y=coords.get("y", 0))
        except Exception as e:
            await self.emit(f"VLM error: {e}", "ERROR")
        return Action(type="click", viewport_x=640, viewport_y=360)  # fallback

    # ------------------------------------------------------------
    # Main orchestration loop
    # ------------------------------------------------------------
    async def orchestrate(self):
        async with async_playwright() as p:
            try:
                await self.emit("Launching browser...", "SYSTEM")
                user_data_dir = f"./data/sessions/{self.session_id}"

                # Load persisted session if any
                context_options = {
                    "viewport": {"width": settings.VIEWPORT_WIDTH, "height": settings.VIEWPORT_HEIGHT},
                    "user_agent": settings.USER_AGENT,
                }
                if settings.USE_REDIS_SESSION:
                    stored = await self._load_session()
                    if stored:
                        context_options["storage_state"] = stored

                self.context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=settings.HEADLESS,
                    **context_options,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--use-fake-ui-for-media-stream",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )

                # Stealth patches + shadow DOM interceptor
                await self.context.add_init_script("""
                    // attachShadow monkey‑patch (closed -> open)
                    (() => {
                        const originalAttachShadow = Element.prototype.attachShadow;
                        Element.prototype.attachShadow = function(init) {
                            if (init && init.mode === 'closed') {
                                init.mode = 'open';
                            }
                            return originalAttachShadow.call(this, init);
                        };
                    })();
                    // WebDriver override
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    // Plugins
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    // Languages
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    // Chrome object
                    window.chrome = { runtime: {} };
                """)

                # Handle external auth windows
                async def on_new_page(new_page):
                    await new_page.wait_for_load_state("domcontentloaded")
                    url = new_page.url
                    if "oauth" in url.lower() or "login" in url.lower():
                        await self.emit(f"OAuth page detected: {url}", "AUTH")
                        # Wait for the page to close or redirect back
                        await new_page.wait_for_event("close", timeout=120000)
                        await self.emit("OAuth flow completed.", "AUTH")

                self.context.on("page", on_new_page)

                page = await self.context.new_page()
                start_url = self.agent_spec.start_url if self.agent_spec else "https://www.google.com"
                await page.goto(start_url, wait_until="domcontentloaded")

                self.current_plan = await generate_plan(self.goal, page.url)
                await self.emit(f"Plan generated: {len(self.current_plan.steps)} steps", "PLAN")

                for idx, step in enumerate(self.current_plan.steps):
                    if self._stop_requested:
                        await self.emit("Stop requested – aborting.", "SYSTEM")
                        break

                    self.step_index = idx
                    await self.emit(f"Step {idx+1}: {step.description}", "THINKING", {"step": idx+1})

                    elements = await self.distill_dom(page)

                    # If target_id missing, use VLM
                    action = step.action
                    if action.target_id:
                        if not any(e.id == action.target_id for e in elements):
                            await self.emit(f"Target '{action.target_id}' not found. VLM fallback...", "WARN")
                            action = await self.vlm_fallback(page, "target missing")

                    allowed = await self._gate(action, step)
                    if not allowed:
                        if self._stop_requested:
                            break
                        await self.emit(f"Step {idx+1} skipped by governor.", "GOVERNOR")
                        continue

                    success = await self.execute_action(page, action, step)
                    if not success:
                        await self.emit(f"Step {idx+1} failed. Trying VLM recovery...", "RECOVERY")
                        vlm_action = await self.vlm_fallback(page, "execution failed")
                        if not await self._gate(vlm_action, step):
                            if self._stop_requested:
                                break
                            continue
                        success = await self.execute_action(page, vlm_action, step)
                        if not success:
                            await self.emit(f"Step {idx+1} permanently failed.", "FATAL_ERROR")
                            break

                    await self.emit(f"Step {idx+1} completed.", "EXECUTION")
                    await asyncio.sleep(0.5)

                    # Save session after each step (if Redis enabled)
                    if settings.USE_REDIS_SESSION:
                        await self._save_session()

                if self.agent_spec:
                    registry.record_run(self.agent_spec.id, tripped=self.guard_state.circuit_tripped)
                await self.emit("Workflow finished.", "SYSTEM",
                                {"governance": self.governance_snapshot()})

            except asyncio.CancelledError:
                await self.emit("Workflow cancelled.", "SYSTEM")
                raise
            except Exception as e:
                await self.emit(f"Critical error: {traceback.format_exc()}", "FATAL_ERROR")
            finally:
                if self.context:
                    await self.context.close()
                await self.emit("Browser resources released.", "SYSTEM_SHUTDOWN")

    async def stop(self):
        self._stop_requested = True
