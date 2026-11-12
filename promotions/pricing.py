"""
What a course costs right now, for someone, with or without a discount code.

The price is decided here and the payment gateway only gets the final amount
(Stripe receives it as ``price_data``; no Stripe coupons), so the rules stay
the same whatever gateway charges it.

Rules:
- Discounts don't stack: with an offer running and a code typed, the lowest
  price wins.
- A paid price never goes below MIN_CHARGE (Stripe's minimum in MXN). Only a
  code can make a course free: that purchase skips the gateway.
- A use counts when the order is paid, also if it's refunded later. While
  someone is in the checkout, their use is reserved for RESERVATION, so a
  code with few uses left isn't oversold. A payment that arrives later (an
  OXXO voucher) is honored even if the uses ran out meanwhile.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q
from django.utils import timezone

from sales.models import Order

from .models import CodeAttempt, Promocion, normalize_code

MIN_CHARGE = Decimal("10.00")
# How long a Stripe checkout with a promotion stays open (Stripe's minimum is
# 30 minutes), and so how long an unpaid order holds a use.
RESERVATION = timedelta(minutes=30)
CENT = Decimal("0.01")

# Wrong codes allowed per window, per user and per IP.
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_FAILED_PER_USER = 10
MAX_FAILED_PER_IP = 30
ATTEMPT_RETENTION = timedelta(hours=24)


class PromoError(Exception):
    """``code`` and the message are safe to show; ``counts`` = a wrong guess, for the rate limit."""

    def __init__(self, code, message, status=400, counts=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.counts = counts


@dataclass
class Quote:
    list_price: Decimal
    discount: Decimal
    price: Decimal
    promotion: Promocion | None = None
    # A valid code that wasn't applied because an offer gives a lower price.
    code_ignored: bool = False

    @property
    def free(self):
        return self.price == 0

    @property
    def code(self):
        return self.promotion.code if self.promotion is not None and self.promotion.code else ""


# --- uses -------------------------------------------------------------------------


def uses(promotion, user=None, product=None):
    """
    Orders that count as uses: paid or refunded, or still in a recent checkout.
    With ``product``, ``user``'s own unpaid order for it is left out: it's the
    one a new checkout replaces.
    """
    orders = Order.objects.filter(promotion=promotion).filter(
        Q(status__in=[Order.Status.PAID, Order.Status.REFUNDED])
        | Q(status=Order.Status.PENDING, created__gte=timezone.now() - RESERVATION)
    )
    if user is not None and product is not None:
        orders = orders.exclude(user=user, product=product, status=Order.Status.PENDING)
    if user is not None:
        return orders.count(), orders.filter(user=user).count()
    return orders.count(), 0


def _sold_out(promotion, user=None, product=None):
    total, mine = uses(promotion, user, product)
    if promotion.max_uses is not None and total >= promotion.max_uses:
        return "sold_out"
    if user is not None and promotion.max_uses_per_user is not None and mine >= promotion.max_uses_per_user:
        return "already_used"
    return None


# --- prices -----------------------------------------------------------------------


def apply(promotion, list_price):
    """``(discount, price)`` for this promotion on this list price."""
    if promotion.discount_type == Promocion.DiscountType.PERCENT:
        discount = (list_price * promotion.value / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        discount = promotion.value
    price = list_price - min(discount, list_price)
    if price <= 0 and promotion.kind == Promocion.Kind.CODE:
        return list_price, Decimal("0.00")
    if price < MIN_CHARGE:
        price = min(list_price, MIN_CHARGE)
    return list_price - price, price


def _running(queryset, now):
    return queryset.filter(active=True).filter(
        Q(starts_at__isnull=True) | Q(starts_at__lte=now), Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    )


def running_offers(product, now=None, lock=False):
    now = now or timezone.now()
    ids = (_running(Promocion.objects.filter(kind=Promocion.Kind.OFFER), now)
           .filter(Q(all_products=True) | Q(products=product)).values_list("pk", flat=True).distinct())
    offers = Promocion.objects.filter(pk__in=list(ids)).order_by("pk")
    return list(offers.select_for_update() if lock else offers)


def best_offer(product, user=None, lock=False):
    """The running offer that gives ``product`` its lowest price, as a Quote, or None."""
    if not product.price:
        return None
    best = None
    for offer in running_offers(product, lock=lock):
        if _sold_out(offer, user, product):
            continue
        discount, price = apply(offer, product.price)
        if best is None or price < best.price:
            best = Quote(product.price, discount, price, offer)
    return best


def check_code(raw_code, product, user=None, lock=False):
    """The discount code's promotion, if it can be used for ``product`` now. Raises PromoError."""
    code = normalize_code(raw_code)
    queryset = Promocion.objects.filter(kind=Promocion.Kind.CODE, code=code)
    promotion = code and (queryset.select_for_update() if lock else queryset).first()
    if not promotion or not promotion.active:
        raise PromoError("invalid_code", "Este código no existe o ya no está activo.", counts=True)
    if not promotion.has_started():
        raise PromoError("not_started", "Este código todavía no está activo.")
    if promotion.has_ended():
        raise PromoError("expired", "Este código ya venció.")
    if not promotion.applies_to(product):
        raise PromoError("not_applicable", "Este código no aplica a este curso.")
    reason = _sold_out(promotion, user, product)
    if reason == "sold_out":
        raise PromoError("sold_out", "Este código ya se agotó.")
    if reason == "already_used":
        raise PromoError("already_used", "Ya usaste este código.")
    return promotion


def quote(product, user=None, code="", lock=False):
    """
    The price ``user`` pays for ``product`` now. ``lock`` (inside a
    transaction) locks the promotions involved, so two checkouts can't both
    take the last use.
    """
    list_price = product.price
    best = Quote(list_price, Decimal("0.00"), list_price)
    offer = best_offer(product, user, lock=lock)
    if offer is not None:
        best = offer
    if code:
        promotion = check_code(code, product, user, lock=lock)
        discount, price = apply(promotion, list_price)
        if price < best.price:
            best = Quote(list_price, discount, price, promotion)
        else:
            best.code_ignored = True
    return best


# --- rate limit -------------------------------------------------------------------


def _user(user):
    return user if user is not None and user.is_authenticated else None


def check_rate_limit(user, ip):
    recent = CodeAttempt.objects.filter(created__gte=timezone.now() - ATTEMPT_WINDOW)
    user = _user(user)
    too_many = (user is not None and recent.filter(user=user).count() >= MAX_FAILED_PER_USER) or (
        ip and recent.filter(ip=ip).count() >= MAX_FAILED_PER_IP
    )
    if too_many:
        raise PromoError("too_many_attempts", "Demasiados intentos. Espera unos minutos y vuelve a intentarlo.",
                         status=429)


def record_failed_attempt(user, ip):
    CodeAttempt.objects.create(user=_user(user), ip=ip or None)
    CodeAttempt.objects.filter(created__lt=timezone.now() - ATTEMPT_RETENTION).delete()


def quote_with_limits(product, user=None, code="", ip=None):
    """quote() for a request: a typed code goes through the rate limit, and a wrong one counts."""
    if not code:
        return quote(product, _user(user))
    check_rate_limit(user, ip)
    try:
        return quote(product, _user(user), code)
    except PromoError as exc:
        if exc.counts:
            record_failed_attempt(user, ip)
        raise
