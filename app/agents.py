"""Agent registry: build, persist, and manage governed agents."""

import json
import os
import time
import uuid
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class AgentPolicy(BaseModel):
    budget_usd: float = Field(0.25, ge=0)
    max_repeat_calls: int = Field(3, ge=0)
    max_total_calls: int = Field(50, ge=0)
    timeout_seconds: int = Field(180, ge=0)
    error_amplification: bool = True
    hitl_mode: str = Field("on_deny", pattern="^(never|on_deny|on_threshold|always)$")
    hitl_budget_threshold: float = Field(0.2, ge=0, le=1)


class AgentSpec(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    goal_template: str = ""
    start_url: str = "https://www.google.com"
    policy: AgentPolicy = AgentPolicy()
    created_at: float = 0.0
    runs: int = 0
    trips: int = 0


class AgentRegistry:
    def __init__(self, path: str = "./data/agents.json"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._agents: Dict[str, AgentSpec] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                for raw in json.load(f):
                    spec = AgentSpec(**raw)
                    self._agents[spec.id] = spec

    def _save(self):
        with open(self.path, "w") as f:
            json.dump([a.model_dump() for a in self._agents.values()], f, indent=2)

    def list(self) -> List[AgentSpec]:
        return sorted(self._agents.values(), key=lambda a: a.created_at, reverse=True)

    def get(self, agent_id: str) -> Optional[AgentSpec]:
        return self._agents.get(agent_id)

    def create(self, spec: AgentSpec) -> AgentSpec:
        spec.id = spec.id or f"agent-{uuid.uuid4().hex[:8]}"
        spec.created_at = spec.created_at or time.time()
        self._agents[spec.id] = spec
        self._save()
        return spec

    def update(self, agent_id: str, spec: AgentSpec) -> Optional[AgentSpec]:
        if agent_id not in self._agents:
            return None
        spec.id = agent_id
        spec.created_at = self._agents[agent_id].created_at
        spec.runs = self._agents[agent_id].runs
        spec.trips = self._agents[agent_id].trips
        self._agents[agent_id] = spec
        self._save()
        return spec

    def delete(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            self._save()
            return True
        return False

    def record_run(self, agent_id: str, tripped: bool = False):
        spec = self._agents.get(agent_id)
        if spec:
            spec.runs += 1
            if tripped:
                spec.trips += 1
            self._save()


registry = AgentRegistry()
