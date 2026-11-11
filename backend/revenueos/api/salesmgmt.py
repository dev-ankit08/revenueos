"""Territories and routing, quotas and rep performance, win/loss, products and deal line items."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Contact, Opportunity, OpportunityLine, Product, Quota, Territory
from ..services import salesmgmt
from ..services.approvals import audit
from ..tools.crm import get_crm
from .workspace import caller, require_admin

router = APIRouter(prefix="/api", tags=["sales management"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


# ------------------------------------------------------------------ territories

class TerritoryIn(BaseModel):
    name: str
    priority: int = 100
    countries: list[str] | None = None
    industries: list[str] | None = None
    min_employees: int | None = None
    max_employees: int | None = None
    members: list[str] = Field(default_factory=list)
    active: bool = True


def _territory(t: Territory) -> dict:
    return {"id": t.id, "name": t.name, "priority": t.priority, "countries": t.countries, "industries": t.industries,
            "min_employees": t.min_employees, "max_employees": t.max_employees, "members": t.members,
            "active": t.active}


@router.get("/territories")
def list_territories(db: Session = Depends(get_db)):
    return [_territory(t) for t in db.scalars(select(Territory).order_by(Territory.priority, Territory.id))]


@router.post("/territories", dependencies=[Depends(require_admin)])
def create_territory(body: TerritoryIn, request: Request, db: Session = Depends(get_db)):
    t = Territory(**body.model_dump())
    db.add(t)
    db.flush()
    audit(db, _actor(request), "territory.create", "territory", t.id, name=t.name)
    db.commit()
    return _territory(t)


@router.put("/territories/{tid}", dependencies=[Depends(require_admin)])
def update_territory(tid: int, body: TerritoryIn, request: Request, db: Session = Depends(get_db)):
    t = db.get(Territory, tid)
    if not t:
        raise HTTPException(404, "Territory not found")
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    audit(db, _actor(request), "territory.update", "territory", t.id)
    db.commit()
    return _territory(t)


@router.delete("/territories/{tid}", dependencies=[Depends(require_admin)])
def delete_territory(tid: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(Territory, tid)
    if t:
        db.delete(t)
        audit(db, _actor(request), "territory.delete", "territory", tid)
        db.commit()
    return {"deleted": tid}


@router.post("/routing/run", dependencies=[Depends(require_admin)])
def run_routing(db: Session = Depends(get_db)):
    """Assign an owner to every unowned lead using the territories."""
    n = salesmgmt.route_unowned(db)
    db.commit()
    return {"assigned": n}


class OwnerIn(BaseModel):
    owner: str | None


@router.put("/leads/{contact_id}/owner")
def set_owner(contact_id: int, body: OwnerIn, request: Request, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    c.owner = body.owner or None
    audit(db, _actor(request), "lead.owner", "contact", c.id, owner=c.owner)
    db.commit()
    return {"contact_id": c.id, "owner": c.owner}


# ------------------------------------------------------------------ quotas

class QuotaIn(BaseModel):
    owner: str
    period: str = Field(pattern=r"^\d{4}-Q[1-4]$")
    amount: float = Field(ge=0)


@router.get("/quotas")
def list_quotas(period: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Quota).order_by(Quota.period.desc(), Quota.owner)
    if period:
        stmt = stmt.where(Quota.period == period)
    return [{"id": q.id, "owner": q.owner, "period": q.period, "amount": q.amount} for q in db.scalars(stmt)]


@router.put("/quotas", dependencies=[Depends(require_admin)])
def set_quota(body: QuotaIn, request: Request, db: Session = Depends(get_db)):
    q = db.scalar(select(Quota).where(Quota.owner == body.owner, Quota.period == body.period))
    if q is None:
        q = Quota(owner=body.owner, period=body.period, amount=body.amount)
        db.add(q)
    q.amount = body.amount
    audit(db, _actor(request), "quota.set", "quota", f"{body.owner}:{body.period}", amount=body.amount)
    db.commit()
    return {"owner": q.owner, "period": q.period, "amount": q.amount}


@router.get("/insights/reps")
def reps(period: str | None = None, db: Session = Depends(get_db)):
    return {"period": period or salesmgmt.current_period(), "reps": salesmgmt.rep_performance(db, period)}


@router.get("/insights/win-loss")
def win_loss(db: Session = Depends(get_db)):
    return salesmgmt.win_loss(db)


# ------------------------------------------------------------------ products and line items

class ProductIn(BaseModel):
    name: str
    sku: str | None = None
    description: str | None = None
    unit_price: float = Field(ge=0)
    currency: str = "USD"
    billing: str = Field(default="one_time", pattern="^(one_time|monthly|annual)$")
    active: bool = True


def _product(p: Product) -> dict:
    return {"id": p.id, "name": p.name, "sku": p.sku, "description": p.description, "unit_price": p.unit_price,
            "currency": p.currency, "billing": p.billing, "active": p.active}


@router.get("/products")
def list_products(db: Session = Depends(get_db)):
    return [_product(p) for p in db.scalars(select(Product).order_by(Product.name))]


@router.post("/products", dependencies=[Depends(require_admin)])
def create_product(body: ProductIn, db: Session = Depends(get_db)):
    p = Product(**body.model_dump())
    db.add(p)
    db.commit()
    return _product(p)


@router.put("/products/{pid}", dependencies=[Depends(require_admin)])
def update_product(pid: int, body: ProductIn, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return _product(p)


class LineIn(BaseModel):
    product_id: int | None = None
    description: str | None = None
    quantity: float = Field(default=1, gt=0)
    unit_price: float | None = Field(default=None, ge=0)  # defaults to the product's list price
    discount_pct: float = Field(default=0, ge=0, le=100)


@router.get("/deals/{opp_id}/lines")
def list_lines(opp_id: int, db: Session = Depends(get_db)):
    return salesmgmt.lines_for(db, opp_id)


@router.post("/deals/{opp_id}/lines")
def add_line(opp_id: int, body: LineIn, db: Session = Depends(get_db)):
    o = db.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "Deal not found")
    price = body.unit_price
    if body.product_id:
        p = db.get(Product, body.product_id)
        if not p:
            raise HTTPException(404, "Product not found")
        price = p.unit_price if price is None else price
    if price is None:
        raise HTTPException(422, "unit_price is required for a line without a product")
    db.add(OpportunityLine(opportunity_id=o.id, product_id=body.product_id, description=body.description,
                           quantity=body.quantity, unit_price=price, discount_pct=body.discount_pct))
    db.flush()
    salesmgmt.recompute_amount(db, o)
    get_crm().upsert_opportunity(o)
    db.commit()
    return {"amount": o.amount, "lines": salesmgmt.lines_for(db, o.id)}


@router.delete("/deals/{opp_id}/lines/{line_id}")
def delete_line(opp_id: int, line_id: int, db: Session = Depends(get_db)):
    line = db.get(OpportunityLine, line_id)
    o = db.get(Opportunity, opp_id)
    if not line or not o or line.opportunity_id != opp_id:
        raise HTTPException(404, "Line not found")
    db.delete(line)
    db.flush()
    salesmgmt.recompute_amount(db, o)
    db.commit()
    return {"amount": o.amount, "lines": salesmgmt.lines_for(db, o.id)}
