"""Pydantic-aware actions via Burr's ``@pydantic_action`` decorator.

``typed_state_loan.py`` showed Pydantic state with the regular ``@action``
decorator: validation was explicit (construct the model at action entry).
``@pydantic_action`` is the next step: the decorated function's ``state``
argument is itself a Pydantic instance carrying only the action's
declared ``reads`` (as a subset model), and the return value is a
Pydantic instance carrying the ``writes``. Burr handles construction
and merge automatically; you get full editor + type-checker support
inside the action body.

Trade-off vs the regular @action: less idiomatic for very small
projects (the subset-model dance is overkill for two-field state),
genuinely better when state grows large and you want compile-time
guarantees that each action only sees what it should.

Domain: a tiny order-validation pipeline:

    place -> validate_address -> compute_shipping -> compute_total
        -> finalize

Each step uses ``@pydantic_action``. State is a Pydantic ``Order``
model; each function receives a subset model (only the keys it
reads) and returns a subset model (only the keys it writes).

Run:

    uv run python examples/pydantic_actions.py
"""

from __future__ import annotations

from typing import Literal

from burr.core import ApplicationBuilder
from burr.integrations.pydantic import PydanticTypingSystem, pydantic_action
from burr.tracking.client import LocalTrackingClient
from pydantic import BaseModel, EmailStr, Field

from burrmcp import ServingMode, mount

_TRACKER_PROJECT = "pydantic-actions-demo"


# == typed state =====================================================


class Address(BaseModel):
    line1: str = Field(min_length=1)
    city: str = Field(min_length=1)
    region: str = Field(min_length=2, max_length=2, description="2-letter region code")
    postal_code: str = Field(min_length=3)


class Order(BaseModel):
    """Full order state. Each @pydantic_action sees a subset of this."""

    # Inputs (set by place)
    customer_email: EmailStr | None = None
    item_sku: str | None = None
    qty: int = Field(default=0, ge=0)
    unit_price: float = Field(default=0.0, ge=0.0)

    # Address (set by place; validated by validate_address)
    shipping_address: Address | None = None
    address_validated: bool = False
    address_validation_notes: str | None = None

    # Computed (set by compute_shipping)
    shipping_cost: float | None = Field(default=None, ge=0.0)

    # Computed (set by compute_total)
    subtotal: float | None = None
    total: float | None = None

    # Lifecycle
    stage: Literal["new", "placed", "validated", "priced", "finalized"] = "new"


# == @pydantic_action functions ======================================
#
# Each action declares reads/writes by name and (optionally) the
# overall Order model. Burr derives subset models behind the scenes
# so the body sees only its declared reads, and the return must be a
# Pydantic model with the declared writes. Anything outside that
# slice is invisible to the action.


@pydantic_action(
    reads=[],
    writes=[
        "customer_email",
        "item_sku",
        "qty",
        "unit_price",
        "shipping_address",
        "stage",
    ],
    state_input_type=Order,
    state_output_type=Order,
)
def place(
    state: Order,
    customer_email: EmailStr,
    item_sku: str,
    qty: int,
    unit_price: float,
    address_line1: str,
    address_city: str,
    address_region: str,
    address_postal: str,
) -> Order:
    """Place an order. Pydantic constructs Address from the flat args
    and validates each field (region must be 2 chars, postal must be
    >=3, etc.) before the model is returned."""
    address = Address(
        line1=address_line1,
        city=address_city,
        region=address_region,
        postal_code=address_postal,
    )
    return state.model_copy(
        update={
            "customer_email": customer_email,
            "item_sku": item_sku,
            "qty": qty,
            "unit_price": unit_price,
            "shipping_address": address,
            "stage": "placed",
        }
    )


@pydantic_action(
    reads=["shipping_address"],
    writes=["address_validated", "address_validation_notes", "stage"],
    state_input_type=Order,
    state_output_type=Order,
)
def validate_address(state: Order) -> Order:
    """Validate the shipping address. State here is a SUBSET model
    carrying only ``shipping_address``; the rest is invisible to this
    function. Reachable in IDEs as ``state.shipping_address`` with
    full type info."""
    addr = state.shipping_address
    notes_parts: list[str] = []
    valid = True
    if addr is None:
        valid = False
        notes_parts.append("no shipping address on the order")
    else:
        # Toy validation: region must be uppercase 2-letter code.
        if not addr.region.isupper():
            valid = False
            notes_parts.append(f"region {addr.region!r} must be uppercase")
        if not addr.postal_code.replace("-", "").isalnum():
            valid = False
            notes_parts.append("postal_code has invalid characters")
    return state.model_copy(
        update={
            "address_validated": valid,
            "address_validation_notes": "; ".join(notes_parts) or "OK",
            "stage": "validated",
        }
    )


@pydantic_action(
    reads=["qty", "unit_price", "shipping_address"],
    writes=["shipping_cost", "stage"],
    state_input_type=Order,
    state_output_type=Order,
)
def compute_shipping(state: Order) -> Order:
    """Compute shipping cost from quantity and destination region.
    Toy model: $5 flat + $1 per item + $10 surcharge if outside the
    region 'CA'."""
    base = 5.0 + (1.0 * state.qty)
    surcharge = 0.0 if state.shipping_address and state.shipping_address.region == "CA" else 10.0
    return state.model_copy(update={"shipping_cost": base + surcharge, "stage": "priced"})


@pydantic_action(
    reads=["qty", "unit_price", "shipping_cost"],
    writes=["subtotal", "total", "stage"],
    state_input_type=Order,
    state_output_type=Order,
)
def compute_total(state: Order) -> Order:
    """Compute subtotal + total. The subset model that lands here
    has exactly qty, unit_price, and shipping_cost; anything else
    is invisible."""
    subtotal = round(state.qty * state.unit_price, 2)
    total = round(subtotal + (state.shipping_cost or 0.0), 2)
    return state.model_copy(update={"subtotal": subtotal, "total": total, "stage": "priced"})


@pydantic_action(
    reads=["total", "address_validated"],
    writes=["stage"],
    state_input_type=Order,
    state_output_type=Order,
)
def finalize(state: Order) -> Order:
    """Terminal. Refuses to finalize on a failed address validation."""
    if not state.address_validated:
        raise ValueError("cannot finalize: address validation failed")
    return state.model_copy(update={"stage": "finalized"})


# == graph ===========================================================


def build_application():
    return (
        ApplicationBuilder()
        .with_typing(PydanticTypingSystem(Order))
        .with_actions(
            place=place,
            validate_address=validate_address,
            compute_shipping=compute_shipping,
            compute_total=compute_total,
            finalize=finalize,
        )
        .with_transitions(
            ("place", "validate_address"),
            ("validate_address", "compute_shipping"),
            ("compute_shipping", "compute_total"),
            ("compute_total", "finalize"),
        )
        .with_tracker(LocalTrackingClient(project=_TRACKER_PROJECT))
        .with_state(Order())
        .with_entrypoint("place")
        .build()
    )


def build_server():
    return mount(
        build_application,
        mode=ServingMode.STEP,
        name="pydantic-actions",
        instructions=(
            "Order pipeline built with Burr's @pydantic_action "
            "decorator. State is a Pydantic Order model; each action "
            "receives a subset model carrying only its declared reads "
            "and returns a subset model carrying only its writes. "
            "Walk: place(customer_email, item_sku, qty, unit_price, "
            "address_line1, address_city, address_region, "
            "address_postal) -> validate_address -> compute_shipping "
            "-> compute_total -> finalize. burr://graph carries the "
            "full Order JSON schema under state_schema."
        ),
    )


if __name__ == "__main__":
    build_server().run()
