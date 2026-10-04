# Customer Operations Agent

A production-pattern LangGraph workflow that handles customer service operations — order lookups, refund processing, support tickets — with role-based access control, deterministic eligibility logic, human-in-the-loop approval for all write actions, and an immutable audit trail.

> Built as Project 2 of 4 in a structured [80-day Generative AI Engineer roadmap](../../README.md) — Days 28–50.
> Architecture decisions, evaluation results, and security analysis are in the [repo README](../../README.md).

---

## Demo

**[▶ Watch the demo — Customer Operations Agent](https://github.com/your-username/your-repo/releases/download/v0.2.0/Operations.Agent.Demo.video.mp4)**

---

## What it does

A supervisor submits: *"Issue a full refund for order O003 — the item was defective."*

The agent validates the request, identifies the customer and order, retrieves shipment data from the database, queries the policy knowledge base for refund eligibility rules, runs a deterministic eligibility calculation in Python, proposes `issue_refund` with arguments, pauses at a structural interrupt gate for human approval, executes the refund only after approval, writes an immutable audit record, and returns a natural-language response.

The LLM never decides eligibility and never executes a write action without approval. Python controls both.

---

## Architecture

```
START
  │
  ▼
request_validator ◄──────────────────────────────────────┐
  │── needs_clarification + count < MAX                   │
  │         └─► clarification_node ──────────────────────┘
  │                 (graph stays alive via checkpointer)
  │── needs_clarification + count >= MAX ──► END (give up)
  │
  └── valid request
        └─► identify_customer_and_order
                  │── not found ──► clarification_node
                  └── resolved
                        └─► retrieve_operational_data
                                  └─► retrieve_policy_evidence
                                        (KnowledgeAgent — hybrid + semantic rerank)
                                            └─► calculate_eligibility
                                                  (deterministic Python — never LLM)
                                                      │── ineligible
                                                      │     └─► communicate_result ──► END
                                                      └── eligible
                                                            └─► propose_action (LLM)
                                                                      └─► human_approval_interrupt
                                                                            │── approved
                                                                            │     └─► role_guard
                                                                            │           │── denied
                                                                            │           │     └─► communicate_result
                                                                            │           └── permitted
                                                                            │                 └─► execute_action
                                                                            │                       └─► audit_log
                                                                            │                             └─► communicate_result ──► END
                                                                            └── rejected
                                                                                  └─► communicate_result ──► END
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph `StateGraph` + `InMemorySaver` checkpointer |
| LLM | Azure OpenAI (`gpt-5-mini` via `AzureChatOpenAI`) |
| Policy retrieval | Project 1 `KnowledgeAgent` — hybrid + semantic rerank |
| Database | SQLAlchemy ORM + SQLite (production: PostgreSQL) |
| API | FastAPI + Uvicorn |
| Frontend | Streamlit |
| Validation | Pydantic v2 |
| Secret management | Azure Key Vault + ACA Managed Identity |
| Deployment | Azure Container Apps (ACA) + Azure Container Registry (ACR) |
| Observability | Azure Application Insights + OpenTelemetry |
| Testing | pytest (40-scenario eval + 5 security tests) |

---

## Key Engineering Decisions

→ Full rationale in [LangGraph Design Decisions](../../docs/langgraph-design-decisions.md)

### 1. The approval gate is structural, not a prompt instruction

The `human_approval_interrupt` node calls LangGraph's `interrupt()` primitive. This **suspends graph execution and persists state** to the checkpointer. Nothing runs until an external `Command(resume=True/False)` call arrives. The approval boundary exists in the graph topology — no user message, prompt injection, or model hallucination can route around it.

Write tools (`issue_refund`, `create_support_ticket`) are not in the `ToolNode` — they are called exclusively inside `execute_action`, which is only reachable through `human_approval_interrupt → role_guard → execute_action`. The Day 38 evaluation ran 5 unauthorized action scenarios and 2 adversarial scenarios: **approval compliance was 100% across all 7**.

### 2. Eligibility is deterministic Python — the LLM explains, Python decides

`calculate_eligibility` never calls the LLM. It calls `calculate_refund_eligibility()` from `workflow/eligibility.py`, which parses the policy text returned by KnowledgeAgent and applies an explicit decision tree:

- Is the order status `"delivered"`? If not → ineligible.
- Is the order within the return window parsed from policy text? If not → ineligible.
- Does the refund amount require manager approval? If yes → flag for supervisor role.

The LLM receives `eligible=True/False` as a state field. It explains the decision in `communicate_result` but cannot override it.

### 3. Role guard is a second structural layer after approval

`role_guard` runs between `human_approval_interrupt` and `execute_action`. It calls `is_action_permitted(user_role, proposed_action)` — a Python lookup against a static `TOOL_PERMISSIONS` dict — and blocks execution if the role is not permitted, regardless of what the LLM proposed.

This closes the gap where the LLM could hallucinate a disallowed action past the prompt-level restriction. The Day 49 security test verified **all 8 injection attack patterns blocked structurally** — the test does not require LLM calls because the structural block is in Python.

### 4. Idempotency via UUID action_id + unique DB constraint

`execute_action` checks `is_duplicate_action(action_id)` before calling any write tool. `action_id` is a UUID generated inside `human_approval_interrupt` at approval time. It is stored in `AgentState`, passed to `execute_action`, and written as a `UNIQUE` column in `audit_logs`. A duplicate `action_id` causes the tool call to be skipped and a `ToolMessage` explaining the skip to be returned. The check is a SQL query — no semantic comparison, no float-vs-string ambiguity.

### 5. Audit log is a dedicated node, not a side effect

`audit_log` is a standalone node that runs after every `execute_action`. It writes 9 required field categories to `AuditLog`:

| Category | Columns |
|---|---|
| Request | `intent`, `request_text` |
| Agent decision | `eligible`, `proposed_action`, `user_role` |
| Policy evidence | `policy_question`, `policy_retrieval_method` |
| Tool name | `tool_name` |
| Tool inputs | `tool_input` (JSON) |
| Tool output | `tool_output` (JSON) |
| Approval | `approval_status` |
| Timestamp | `timestamp` (auto, DB default) |
| Final action | `action`, `action_executed`, `execution_outcome` |

The AuditLog table is INSERT-only. No UPDATE path exists. The Day 49 security test verifies no PII columns (`customer_email`, `card_number`, `ssn`) are present in the schema.

### 6. Secret management via Azure Key Vault — fail-fast at startup

`config.py` defines a `Settings` dataclass with `from_env()`. It is called once in the FastAPI lifespan before any request is served. If any required environment variable is missing (e.g. a Key Vault reference failed to resolve), `ValueError` is raised immediately and the container fails its `HEALTHCHECK`. ACA marks the revision unhealthy and rolls back automatically. The app never silently starts with missing credentials.

Secret flow in production:
```
Key Vault secret → ACA Key Vault reference → ACA env var → os.environ → Settings.from_env()
```

`settings.redacted_repr()` logs `SET/MISSING` per variable on startup — never actual values.

### 7. Policy retrieval reuses Project 1 KnowledgeAgent

`retrieve_policy_evidence` calls `KnowledgeAgent.ask()` from Project 1 with an intent-mapped query string. If the KnowledgeAgent call fails for any reason, a hardcoded safe fallback policy answer is returned — the graph never blocks on a RAG failure. The fallback is conservative (30-day window, $500 manager approval threshold) so the eligibility calculation degrades safely rather than granting refunds incorrectly.

---

## Security Properties

→ Full analysis in [security/checklist.py](security/checklist.py)

| Property | Implementation | Test |
|---|---|---|
| Role-based access | `TOOL_PERMISSIONS` dict — Python lookup, not prompt | Day 49 Test 2 |
| Write action approval | LangGraph `interrupt()` — structural suspend | Day 38 eval — 100% compliance |
| Input validation | Pydantic `args_schema` on every write tool | Day 49 Test 3 |
| Idempotency | UUID `action_id` + `UNIQUE` DB constraint | Day 38 S33 |
| Audit logging | Dedicated `audit_log` node — INSERT-only | Day 49 Test 4 |
| PII handling | `customer_email` excluded from `audit_logs` | Day 49 Test 4 |
| Injection resistance | Role guard blocks execution regardless of LLM output | Day 49 Test 5 |
| Secret management | Azure Key Vault + Managed Identity, no plaintext secrets | Day 48 |

**Security checklist result: 5/5 tools passed all 6 checklist items.**

**Injection resilience: 8/8 attack patterns blocked structurally** (direct override, role claim, delimiter injection, fictional framing, data exfiltration, direct tool invocation, prompt extraction, jailbreak).

---

## Evaluation Results

→ Full results in [Day 38 Implementation Report](../../evaluations/day38_implementation_report.md)

| Metric | Result |
|---|---:|
| Route accuracy | 90.0% |
| Tool argument validity | 85.2% |
| **Approval compliance** | **100.0%** |
| **Task completion rate** | **97.5%** |
| Clarification rate | 100.0% |
| Avg unnecessary tool calls | 0.23 |
| Runner errors | 0 |

| Category | Passed | Total |
|---|---:|---:|
| Successful resolution | 15 | 15 |
| Missing information | 8 | 8 |
| Unauthorized action | 5 | 5 |
| Tool failure | 5 | 5 |
| Ambiguous intent | 5 | 5 |
| Adversarial | 2 | 2 |

---

## Project Structure

```
projects/operations_agent/
│
├── api/
│   ├── main.py                   # FastAPI lifespan: get_settings() → create_tables() → seed_data()
│   ├── schemas.py                # Pydantic request/response models
│   └── routers/
│       ├── health.py             # GET /health — liveness + DB probe
│       ├── sessions.py           # POST /sessions, /messages, /approve
│       └── metrics.py            # GET /metrics/dashboard — 12 observability metrics
│
├── workflow/
│   ├── graph.py                  # 12-node StateGraph — all edges and conditional routing
│   ├── state.py                  # WorkflowState TypedDict — 25+ fields with reducer annotations
│   ├── eligibility.py            # Deterministic refund eligibility engine
│   └── roles.py                  # TOOL_PERMISSIONS dict — is_action_permitted() structural check
│
├── graph/
│   └── request_validator.py      # Intent classifier + field extractor — runs before LLM
│
├── tools/
│   ├── db_tools.py               # Read tools: get_order_db, get_shipment_db, get_customer_db
│   ├── read_tools.py             # Read-only tool definitions with Pydantic args_schema
│   └── write_tools.py            # Write tools: issue_refund, create_support_ticket (IssueRefundInput, CreateTicketInput)
│
├── database/
│   ├── engine.py                 # SQLAlchemy engine + SessionLocal
│   ├── models.py                 # Customer, Order, Shipment, Inventory, AuditLog ORM models
│   └── seed.py                   # create_tables() + seed_data() — idempotent, called at startup
│
├── security/
│   ├── __init__.py
│   └── checklist.py              # ToolSecurityProfile registry + 6-point checklist runner
│
├── monitoring/
│   └── tracing.py                # GraphTracer — OpenTelemetry spans around LLM calls
│
├── errors/
│   └── handlers.py               # is_duplicate_action(), log_rejection()
│
├── config.py                     # Settings dataclass — fail-fast validation at startup
├── state.py                      # WorkflowState (alias entry point)
└── streamlit_app.py              # Demo UI — role selector, chat, approval card, audit trail
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness + DB probe |
| `POST` | `/sessions` | Start a new workflow session |
| `POST` | `/sessions/{thread_id}/messages` | Send follow-up (awaiting clarification) |
| `POST` | `/sessions/{thread_id}/approve` | Approve or reject proposed action |
| `GET` | `/metrics/dashboard` | 12 observability metrics |

### `POST /sessions` — Request

```json
{
  "message": "Issue a full refund for order O003, the item was defective",
  "user_role": "supervisor",
  "thread_id": "optional-uuid-to-resume"
}
```

### `POST /sessions` — Response (awaiting approval)

```json
{
  "thread_id": "3ef8b862-...",
  "status": "awaiting_approval",
  "response": "I've reviewed this request and propose the action below.",
  "intent": "refund",
  "eligible": true,
  "proposed_action": "issue_refund",
  "proposed_args": {
    "order_id": "O003",
    "amount": 299.99,
    "reason": "Full refund for defective item reported by customer"
  },
  "execution_outcome": null
}
```

### `POST /sessions/{thread_id}/approve` — Request

```json
{ "approved": true }
```

### Response after approval

```json
{
  "status": "complete",
  "execution_outcome": "success",
  "response": "I've issued a full refund of $299.99 for order O003..."
}
```

---

## Role Permissions

| Role | Read Orders | Read Customers | Create Ticket | Issue Refund |
|---|---|---|---|---|
| `customer` | Own only | No | No | No |
| `support` | All | Yes | Yes | No |
| `supervisor` | All | Yes | Yes | Yes |

Role is set at session start and enforced at two layers: the `propose_action` system prompt (soft — LLM won't offer disallowed actions) and `role_guard` (hard — Python blocks execution regardless of LLM output).

---

## Setup

### Prerequisites

- Python 3.11+
- Azure OpenAI deployment (`gpt-4o`, `text-embedding-3-small`)
- Azure AI Search instance
- Azure subscription (for ACA deployment)

### Environment variables

```env
AZURE_OPENAI_API_KEY=your_key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
MODEL_DEPLOYMENT_NAME=gpt-4o
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_API_KEY=your_search_key
AZURE_EMBEDDING_DEPLOYMENT=text-embedding-3-small
DATABASE_URL=sqlite:///./operations_agent.db      # optional, defaults to SQLite
```

### Install dependencies

```bash
pip install -r requirements.txt
```

---

## Running Locally

**Terminal 1 — FastAPI backend:**
```bash
uvicorn projects.operations_agent.api.main:app --reload
```

**Terminal 2 — Streamlit demo UI:**
```bash
streamlit run projects/operations_agent/streamlit_app.py
```

Open `http://localhost:8501`. The demo data reference expander in the UI shows all customer, order, and tracking IDs.

---

## Running Tests

```bash
# Day 38 — 40-scenario agent evaluation
python evaluations/operations_agent_eval_runner.py

# Day 49 — security checklist (no Azure credentials needed)
pytest experiments/day49_security.py -v

# Day 49 — live injection test against deployed ACA
$env:ACA_URL = "https://operations-agent.wonderfulrock-0bf0bcd3.eastus.azurecontainerapps.io"
pytest experiments/day49_security.py -v --integration
```

---

## Deployment

The agent is containerized and deployed to Azure Container Apps with secrets managed through Azure Key Vault.

```bash
# Build and push image
docker build -t operations-agent .
az acr build --registry opsagentacr1 --image operations-agent:latest .

# Key Vault setup (run once)
.\deploy\setup_keyvault.ps1

# Container App already running at:
# https://operations-agent.wonderfulrock-0bf0bcd3.eastus.azurecontainerapps.io
```

---

## Build Log

| Day | What was built |
|---|---|
| 28–30 | LangGraph foundations — StateGraph, nodes, edges, InMemorySaver checkpointer |
| 31–33 | Tool-calling loop — read tools (order, customer, shipment, inventory, policy) |
| 34–35 | State persistence — multi-turn sessions, thread isolation, checkpoint history |
| 36–37 | Human approval gate — `interrupt()`, `Command(resume=...)`, approval/rejection paths |
| 38 | 40-scenario evaluation — route accuracy, approval compliance, task completion |
| 39 | Failure handling — tool timeout, DB failure, invalid args, duplicate actions |
| 40 | Phase 4 → Phase 5 transition: full business workflow design |
| 41 | 12-node workflow skeleton — all nodes wired, `WorkflowState` with 25+ fields |
| 42 | Clarification loop — history-aware re-validation, give-up guard |
| 43 | Role-based access control — `TOOL_PERMISSIONS`, `is_action_permitted()`, `role_guard` node |
| 44 | Durable audit logging — 9-category `AuditLog` schema, dedicated `audit_log` node |
| 45 | Observability — OpenTelemetry spans, Azure Application Insights, `/metrics/dashboard` |
| 46 | Containerization — Dockerfile, FastAPI lifespan, `/health` HEALTHCHECK |
| 47 | Azure deployment — ACR + ACA via Azure Portal, 5-test deployment verification |
| 48 | Secret management — Azure Key Vault, Managed Identity, `config.py` fail-fast validation |
| 49 | Security review — 6-point tool checklist, injection resilience test, PII audit |
| 50 | Demo UI (Streamlit), Loom recording, README |
