"""Request and response shapes for the REST API.

Every field carries a description: these classes ARE the Swagger page, so a
change here is a change to the published contract.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- requests --


class TroubleshootRequest(BaseModel):
    """Start a source-to-destination reachability run."""

    source: str = Field(..., description="Source device name or IP address",
                        examples=["10.10.1.20"])
    destination: str = Field(..., description="Destination device name or IP",
                             examples=["172.20.5.10"])
    protocol: Literal["TCP", "UDP", "HTTP"] = Field(
        "TCP", description="Transport the firewall check should ask about")
    port: Optional[str] = Field(
        "22", description="Destination port for the policy check; omit for 'any'",
        examples=["443"])

    def to_prompt(self) -> str:
        """The natural-language request the agent's prompt is written around."""
        parts = [f"troubleshoot {self.source.strip()} to {self.destination.strip()}"]
        if self.protocol:
            parts.append(self.protocol)
        if self.port and str(self.port).strip():
            parts.append(str(self.port).strip())
        return " ".join(parts)


class AskRequest(BaseModel):
    """Ask a question. With `run_id` the agent answers from that run's context."""

    question: str = Field(..., description="Free-text question",
                          examples=["why is it blocked?"])
    run_id: Optional[str] = Field(
        None, description="Answer in the context of this run. Omit for a "
                          "standalone question with no run history.")


class ApprovalRequest(BaseModel):
    """Approve or reject the device command a run is waiting on."""

    approved: bool = Field(..., description="true runs the command, false skips it")


# --------------------------------------------------------------- responses --


class StageView(BaseModel):
    key: str = Field(..., description="cmdb | ping | trace | policy | done")
    label: str
    status: Literal["pending", "running", "done", "failed", "skipped"]
    detail: str = ""


class CommandView(BaseModel):
    """One command the agent issued, with its result."""

    cmd: str = Field(..., description="The command as it ran")
    device: Optional[str] = Field(
        None, description="Where it ran: a device, 'agent host', or the tool")
    region: Optional[str] = None
    status: Literal["running", "done", "failed"]
    detail: str = Field("", description="One-line summary of the result")
    thought: str = Field("", description="The model's reasoning for this step")
    output: str = Field("", description="Raw command output, truncated")
    kind: Optional[str] = Field(
        None, description="cmdb | policy | ping | trace (basic commands only)")
    step: int = 0


class PathNode(BaseModel):
    label: str
    ip: Optional[str] = None
    kind: Literal["source", "hop", "dest", "dead"]


class PathView(BaseModel):
    nodes: list[PathNode] = []
    line: str = Field("", description="The chain rendered as one line")
    reached: Optional[bool] = None
    truncated: bool = Field(
        False, description="True when hops past the last one never answered")


class WorkflowView(BaseModel):
    """Everything the progress panel renders."""

    title: str = ""
    steps: list[StageView] = []
    params: dict = Field({}, description="What this run is about")
    basics: list[CommandView] = Field([], description="The standard workflow")
    checks: list[CommandView] = Field([], description="Deeper-check commands")
    summary: dict = Field({}, description="Facts parsed from raw CLI, not the model")
    path: PathView = PathView()
    local: bool = Field(
        False, description="True when the source was not in the CMDB and the "
                           "probes ran from the agent machine instead")
    report: Optional[dict] = None
    deepReport: Optional[dict] = None
    maxDeep: int = 10


class PendingApproval(BaseModel):
    """A device command parked waiting for a human decision."""

    id: str = Field(..., description="Pass to POST /api/runs/{id}/approvals/{aid}")
    tool: str
    command: str = Field(..., description="Exactly what will run")
    device_ip: Optional[str] = None
    region: Optional[str] = None


class RunSummary(BaseModel):
    """A run without its detail, for listings."""

    id: str
    status: Literal["running", "waiting_approval", "waiting_clipboard",
                    "done", "error"]
    kind: Literal["troubleshoot", "lookup", "question", "deep"]
    request: str = Field(..., description="The request that started it")
    created_at: float
    updated_at: float


class RunView(RunSummary):
    """A run in full: progress, commands, verdict, and anything it is waiting on."""

    workflow: WorkflowView
    report: Optional[dict] = Field(
        None, description="The structured verdict once the run concludes")
    deep_report: Optional[dict] = Field(
        None, description="The deeper-checks verdict, if they were run")
    answer: str = Field("", description="Plain-text answer, for question runs")
    pending_approval: Optional[PendingApproval] = None
    offer_deep: bool = Field(
        False, description="True when reachability is unconfirmed and deeper "
                           "checks are worth running")
    error: Optional[str] = None
    unavailable_servers: list[str] = Field(
        [], description="MCP servers that could not be reached for this run")


class DeviceResponse(BaseModel):
    """A CMDB lookup result."""

    query: str
    found: bool
    record: Optional[Any] = Field(
        None, description="The slimmed CMDB record; credentials are dropped at "
                          "the MCP boundary and never appear here")
    raw: str = Field("", description="The tool's reply as text")


class HealthResponse(BaseModel):
    ok: bool
    llm_mode: str = Field(..., description="clipboard (human relay) or api")
    mocks: bool = Field(..., description="True when the mock MCP servers are in use")
    masking: bool = Field(..., description="Address masking on outgoing prompts")
    active_run: Optional[str] = Field(
        None, description="The run currently holding the agent, if any")


class AcceptedResponse(BaseModel):
    """Returned when work was started or a decision was recorded."""

    run_id: str
    status: str
