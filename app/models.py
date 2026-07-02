from typing import Optional, List, Literal
from pydantic import BaseModel

class InteractiveElement(BaseModel):
    id: str
    tag: str
    text: str
    type: str = ""
    page_x: float
    page_y: float
    viewport_x: float
    viewport_y: float
    frame_path: List[str] = []

class Action(BaseModel):
    type: Literal["click", "type", "select", "press", "navigate", "firecrawl"]
    target_id: Optional[str] = None
    value: Optional[str] = None
    viewport_x: Optional[float] = None
    viewport_y: Optional[float] = None
    url: Optional[str] = None

class PlanStep(BaseModel):
    description: str
    action: Action
    expected_condition: str

class Plan(BaseModel):
    steps: List[PlanStep]
