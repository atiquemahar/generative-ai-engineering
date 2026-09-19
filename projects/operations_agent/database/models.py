# projects/operations_agent/database/models.py
from typing import Optional
from sqlalchemy import String, Integer, Float, DateTime, JSON, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime, timezone

class Base(DeclarativeBase):
    pass

class Customer(Base):
    __tablename__ = "customers"

    id:     Mapped[str]     = mapped_column(String, primary_key=True)
    name:   Mapped[str]     = mapped_column(String, nullable=False)
    email:  Mapped[str]     = mapped_column(String, nullable=False)
    tier:   Mapped[str]     = mapped_column(String, default="standard")
    status: Mapped[str]     = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"Customer(id={self.id!r}, name={self.name!r}, tier={self.tier!r})"

class Order(Base):
    __tablename__ = "orders"

    id:             Mapped[str]     = mapped_column(String, primary_key=True) 
    customer_id:    Mapped[str]     = mapped_column(String, ForeignKey("customers.id"), nullable=False)
    product_id:     Mapped[str]     = mapped_column(String, nullable=False)
    quantity:       Mapped[int]     = mapped_column(Integer, nullable=False)
    status:         Mapped[str]     = mapped_column(String, default="processing")
    total_usd:      Mapped[float]   = mapped_column(Float, nullable=False)
    created_at:     Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc)) 

    def __repr__(self) -> str:
        return f"Order(id={self.id!r}, customer_id={self.customer_id!r}, status={self.status!r})"

class Shipment(Base):
    __tablename__ = "shipments"

    id:         Mapped[str]     = mapped_column(String, primary_key=True) 
    order_id:   Mapped[str]     = mapped_column(String, ForeignKey("orders.id"), nullable=False) 
    carrier:    Mapped[str]     = mapped_column(String, nullable=False)
    tracking_number: Mapped[str] = mapped_column(String, nullable=False) 
    status:     Mapped[str]     = mapped_column(String, default="processing")
    estimated_delivery: Mapped[str] = mapped_column(String)

    def __repr__(self) -> str:
        return f"Shipment(order_id={self.order_id!r}, status={self.status!r})"

class Inventory(Base):
    __tablename__ = "inventory" 

    product_id:     Mapped[str]     = mapped_column(String, primary_key=True)  # P001
    product_name:   Mapped[str]     = mapped_column(String, nullable=False) 
    stock:          Mapped[int]     = mapped_column(Integer, default=0) 
    warehouse:      Mapped[str]     = mapped_column(String, nullable=False)

    def __repr__(self) -> str:
        return f"Inventory(product_id={self.product_id!r}, stock={self.stock!r})"

class AuditLog(Base):
    """
    Immutable audit record — INSERT-only. No UPDATE path exists anywhere in the codebase.
    Every field is written once at audit_log node execution time and never changed.

    action_id (UUID) is the idempotency key — unique constraint prevents
    double-execution even on client retries.
    """
    __tablename__ = "audit_logs"

    # ── Identity ──────────────────────────────────────────────────────────
    id:          Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)
    action_id:   Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    session_id:  Mapped[Optional[str]] = mapped_column(String, nullable=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── Request ───────────────────────────────────────────────────────────
    intent:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── Agent decision chain ──────────────────────────────────────────────
    eligible:             Mapped[Optional[bool]] = mapped_column(nullable=True)
    ineligibility_reason: Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    user_role:            Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    proposed_action:      Mapped[Optional[str]]  = mapped_column(String, nullable=True)

    # ── Retrieved policy evidence ─────────────────────────────────────────
    policy_question:         Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    policy_evidence_answer:  Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    policy_confidence:       Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    policy_sources:          Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    policy_retrieval_method: Mapped[Optional[str]]  = mapped_column(String, nullable=True)

    # ── Tool execution ────────────────────────────────────────────────────
    action:            Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    tool_name:         Mapped[Optional[str]]  = mapped_column(String, nullable=True)
    tool_input:        Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    tool_output:       Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    action_executed:   Mapped[Optional[bool]] = mapped_column(nullable=True)
    execution_outcome: Mapped[Optional[str]]  = mapped_column(String, nullable=True)

    # ── Approval ──────────────────────────────────────────────────────────
    approval_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── Errors accumulated during this session ────────────────────────────
    session_errors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── Timestamp (auto — never caller-supplied) ──────────────────────────
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"AuditLog(id={self.id!r}, action_id={self.action_id!r}, "
            f"action={self.action!r}, executed={self.action_executed!r})"
        )     


