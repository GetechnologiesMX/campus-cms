"""
Generating and redeeming agreement codes.

Redeeming reuses sales.services.sync_access, the same function that gives
access after a Stripe payment, and records the enrollment as an Order with
origin "agreement" and amount 0. Consuming the code, creating the order and
the enrollment happen in one DB transaction: if Open edX doesn't enroll the
user, everything is rolled back and the code is still available.

Removing the access (revoke_access) is the only thing that takes the course
away: voiding a code, deactivating or expiring the agreement only stop new
redeems.
"""
import csv
import io
import logging
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from core import openedx
from sales import services as sales_services
from sales.models import Order

from .models import (
    CODE_ALPHABET,
    RANDOM_LENGTH,
    REDEEMABLE_STATUSES,
    CodigoConvenio,
    Convenio,
    RedeemAttempt,
    normalize_code,
)

log = logging.getLogger(__name__)

# Open edX calls made while the code's row is locked: fail fast instead of
# holding the lock for the default 90 s of a write.
OPENEDX_TIMEOUT = 8

# Failed redeems allowed per window, per user and per IP (several employees
# can share an office IP, so it's higher).
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_FAILED_PER_USER = 10
MAX_FAILED_PER_IP = 30
ATTEMPT_RETENTION = timedelta(hours=24)


class RedeemError(Exception):
    """``code`` and the message are safe to show; ``counts`` = a failed guess for the rate limit."""

    def __init__(self, code, message, status=400, counts=True):
        super().__init__(message)
        self.code = code
        self.status = status
        self.counts = counts


def invalid_code():
    # The same answer for a code that doesn't exist, was used or was voided:
    # the response never tells a guesser that a code exists.
    return RedeemError("invalid_code", "El código no es válido o ya fue utilizado.")


# --- generation ------------------------------------------------------------------


def random_code(prefix):
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(RANDOM_LENGTH))
    return f"{prefix}-{chars[:4]}-{chars[4:]}"


def generate_codes(convenio, count):
    """
    Create ``count`` new available codes, never beyond the agreement's seats
    (voided unused codes don't count, so they can be replaced). The agreement
    row is locked so two admins generating at once can't overshoot.
    """
    if count < 1:
        raise ValueError("Indica cuántos códigos generar (1 o más).")
    with transaction.atomic():
        convenio = Convenio.objects.select_for_update().get(pk=convenio.pk)
        free = convenio.seats - convenio.seats_taken()
        if count > free:
            raise ValueError(f"Solo quedan {max(free, 0)} lugares sin código en este convenio.")
        codes = set()
        while len(codes) < count:
            batch = {random_code(convenio.code_prefix) for _ in range(count - len(codes))}
            batch -= set(CodigoConvenio.objects.filter(code__in=batch).values_list("code", flat=True))
            codes |= batch
        return CodigoConvenio.objects.bulk_create(
            CodigoConvenio(convenio=convenio, code=code) for code in sorted(codes)
        )


def void_codes(codes):
    """
    Void the given codes. A used code keeps its seat and its owner keeps the
    course: voiding (like deactivating or expiring the agreement) only stops
    new redeems. Returns how many changed.
    """
    return codes.filter(status__in=[CodigoConvenio.Status.AVAILABLE, CodigoConvenio.Status.USED]).update(
        status=CodigoConvenio.Status.VOID,
    )


def revoke_access(codes, by=None):
    """
    Take the course away from whoever redeemed these codes. Each code becomes
    "revoked" (it no longer holds a seat, so a new code can be generated in
    its place) and its order "cancelled"; then Open edX unenrolls the user.
    Codes nobody redeemed, or already revoked, are skipped.

    Returns the cancelled orders. One whose access is "error" (Open edX
    didn't answer) is retried by reconcile_orders or "Reintentar" in Ventas;
    the user keeps no access in Campus meanwhile.
    """
    now = timezone.now()
    with transaction.atomic():
        targets = list(
            codes.select_for_update().filter(redeemed_at__isnull=False, order__status=Order.Status.PAID)
            .exclude(status=CodigoConvenio.Status.REVOKED).select_related("order__user")
        )
        CodigoConvenio.objects.filter(pk__in=[code.pk for code in targets]).update(
            status=CodigoConvenio.Status.REVOKED, revoked_at=now, revoked_by=by,
        )
        orders = [code.order for code in targets]
        for order in orders:
            order.status = Order.Status.CANCELLED
            order.save(update_fields=["status", "modified"])
    # Open edX after the commit: no rows stay locked while it answers.
    for code, order in zip(targets, orders, strict=True):
        sales_services.sync_access(order)
        log.info("Agreement code %s: access removed from %s in %s by %s (%s)", code.code, order.user.username,
                 order.course_key, getattr(by, "username", None), order.access)
    return orders


def report(convenio):
    """Seats and code counts for the agreement's report."""
    counts = convenio.codes.aggregate(
        generated=Count("pk"),
        used=Count("pk", filter=Q(redeemed_at__isnull=False) & ~Q(status=CodigoConvenio.Status.REVOKED)),
        available=Count("pk", filter=Q(status=CodigoConvenio.Status.AVAILABLE)),
        void=Count("pk", filter=Q(status=CodigoConvenio.Status.VOID)),
        revoked=Count("pk", filter=Q(status=CodigoConvenio.Status.REVOKED)),
    )
    return {"seats": convenio.seats, **counts, "free_seats": max(convenio.seats - convenio.seats_taken(), 0)}


CSV_HEADER = ["código", "estado", "usuario", "correo", "fecha de canje", "curso", "acceso retirado el"]


def codes_csv(convenios):
    """The codes of these agreements as CSV text (with a BOM, so Excel reads the accents)."""
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    codes = (CodigoConvenio.objects.filter(convenio__in=convenios).select_related("redeemed_by", "product")
             .order_by("convenio_id", "created", "pk"))
    for code in codes:
        writer.writerow([
            code.code,
            code.get_status_display(),
            code.redeemed_by.username if code.redeemed_by else "",
            code.redeemed_by.email if code.redeemed_by else "",
            timezone.localtime(code.redeemed_at).strftime("%Y-%m-%d %H:%M") if code.redeemed_at else "",
            code.product.course_key if code.product else "",
            timezone.localtime(code.revoked_at).strftime("%Y-%m-%d %H:%M") if code.revoked_at else "",
        ])
    return out.getvalue()


# --- rate limit -------------------------------------------------------------------


def check_rate_limit(user, ip):
    since = timezone.now() - ATTEMPT_WINDOW
    recent = RedeemAttempt.objects.filter(created__gte=since)
    too_many = recent.filter(user=user).count() >= MAX_FAILED_PER_USER or (
        ip and recent.filter(ip=ip).count() >= MAX_FAILED_PER_IP
    )
    if too_many:
        raise RedeemError("too_many_attempts", "Demasiados intentos. Espera unos minutos y vuelve a intentarlo.",
                          status=429, counts=False)


def record_failed_attempt(user, ip):
    RedeemAttempt.objects.create(user=user, ip=ip or None)
    purge_old_attempts()


def purge_old_attempts():
    deleted, _ = RedeemAttempt.objects.filter(created__lt=timezone.now() - ATTEMPT_RETENTION).delete()
    return deleted


# --- redeem -----------------------------------------------------------------------


def _already_enrolled(user, product):
    """A paid order in the CMS, or an active verified enrollment made some other way."""
    if sales_services.user_owns(user, product):
        return True
    try:
        with openedx.timeout_cap(OPENEDX_TIMEOUT):
            enrollment = openedx.lms("GET", f"/api/enrollment/v1/enrollment/{user.username},{product.course_key}")
    except openedx.OpenEdxError as exc:
        log.warning("Could not check %s's enrollment in %s: %s", user.username, product.course_key, exc)
        raise RedeemError("unavailable", "No pudimos activar tu acceso en este momento. Intenta de nuevo en unos "
                          "minutos; tu código no se usó.", status=503, counts=False) from exc
    # An audit enrollment isn't the course: redeeming upgrades it to verified.
    return bool(enrollment and enrollment.get("is_active") and enrollment.get("mode") == "verified")


def _validate(raw_code, course_key):
    """The code and product, if the code can be used for that course right now."""
    normalized = normalize_code(raw_code)
    code = normalized and CodigoConvenio.objects.select_related("convenio").filter(code=normalized).first()
    if not code or code.status != CodigoConvenio.Status.AVAILABLE:
        raise invalid_code()
    if not code.convenio.is_current():
        raise RedeemError("expired", "Este código ya no está vigente. Consulta con tu empresa.")
    # The product's status is checked again here, not only when the agreement
    # was saved: a course archived since then can't be redeemed.
    product = code.convenio.products.filter(course_key=course_key, status__in=REDEEMABLE_STATUSES).first()
    if product is None:
        raise RedeemError("course_not_included", "Este código no es válido para este curso.")
    return code, product


def redeem(user, raw_code, course_key, ip=None):
    """
    Consume the code and enroll the user. Returns the Order. Raises RedeemError.

    The code is taken with a conditional UPDATE (available → used): the row
    stays locked until commit, so a second request with the same code waits
    and then finds it used. Open edX calls inside the transaction have a short
    timeout (OPENEDX_TIMEOUT) to keep that lock brief.
    """
    check_rate_limit(user, ip)
    try:
        code, product = _validate(raw_code, course_key)
        if _already_enrolled(user, product):
            raise RedeemError("already_enrolled", "Ya estás inscrito en este curso; tu código no se usó.",
                              status=409, counts=False)
        return _consume(user, code, product)
    except RedeemError as exc:
        if exc.counts:
            record_failed_attempt(user, ip)
        raise


def _consume(user, code, product):
    now = timezone.now()
    enrolled = False
    try:
        with transaction.atomic():
            taken = CodigoConvenio.objects.filter(pk=code.pk, status=CodigoConvenio.Status.AVAILABLE).update(
                status=CodigoConvenio.Status.USED, redeemed_by=user, product=product, redeemed_at=now,
            )
            if not taken:
                raise invalid_code()  # someone else just used it
            try:
                order = Order.objects.create(
                    user=user, product=product, course_key=product.course_key, amount=0,
                    origin=Order.Origin.AGREEMENT, status=Order.Status.PAID, paid_at=now,
                )
            except IntegrityError:
                # A paid order for this course appeared meanwhile (another code, or a purchase).
                raise RedeemError("already_enrolled", "Ya estás inscrito en este curso; tu código no se usó.",
                                  status=409, counts=False) from None
            with openedx.timeout_cap(OPENEDX_TIMEOUT):
                sales_services.sync_access(order)
            if order.access != Order.Access.GRANTED:
                raise RedeemError("enrollment_failed", "No pudimos activar tu acceso en este momento. Intenta de "
                                  "nuevo en unos minutos; tu código no se usó.", status=503, counts=False)
            enrolled = True
            CodigoConvenio.objects.filter(pk=code.pk).update(order=order)
    except Exception:
        if enrolled:
            # Open edX already enrolled the user, but the code and order were
            # rolled back: reconcile_orders will report this enrollment as unpaid.
            log.warning("Agreement code %s rolled back after enrolling %s in %s", code.code, user.username,
                        product.course_key)
        raise
    log.info("Agreement code %s (%s) redeemed by %s in %s", code.code, code.convenio, user.username,
             product.course_key)
    return order
