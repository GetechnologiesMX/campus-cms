from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import stripe
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Product
from promotions import pricing
from promotions.models import Promocion, normalize_code
from sales.models import Order
from sales.tests.test_sales import KEY, SalesTestCase

SESSION = SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.test/1")


def promo(**fields):
    products = fields.pop("products", None)
    defaults = dict(name="Buen Fin", kind=Promocion.Kind.CODE, code="BUENFIN", discount_type="percent",
                    value=Decimal("20"))
    promotion = Promocion.objects.create(**{**defaults, **fields})
    if products:
        promotion.products.set(products)
    return promotion


class PricingTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(title="Contabilidad", number="C1", run="R", course_key=KEY,
                                              status=Product.Status.ON_SALE, price=Decimal("1499.50"))
        self.user = User.objects.create(username="alumna")

    def quote(self, code="", user=None):
        return pricing.quote(self.product, user, code)

    def test_normalize_code(self):
        assert normalize_code(" buen fin25 ") == "BUENFIN25"
        assert normalize_code("ñ!") == ""

    def test_percent_and_amount(self):
        promo(products=[self.product])
        q = self.quote("buenfin")
        assert (q.list_price, q.discount, q.price, q.code) == (
            Decimal("1499.50"), Decimal("299.90"), Decimal("1199.60"), "BUENFIN")
        promo(code="MENOS500", discount_type="amount", value=500, all_products=True)
        assert self.quote("MENOS500").price == Decimal("999.50")

    def test_never_below_the_minimum_charge_unless_free(self):
        promo(code="CASI", discount_type="amount", value=1495, all_products=True)
        assert self.quote("CASI").price == pricing.MIN_CHARGE
        promo(code="GRATIS", value=100, all_products=True)
        assert self.quote("GRATIS").free
        promo(code="MILES", discount_type="amount", value=5000, all_products=True)
        assert self.quote("MILES").free
        promo(name="Oferta", kind="offer", code=None, discount_type="amount", value=5000, all_products=True)
        assert self.quote().price == pricing.MIN_CHARGE  # an offer never gives the course away

    def test_offer_can_not_be_100_percent(self):
        with self.assertRaises(ValidationError):
            Promocion(name="x", kind="offer", discount_type="percent", value=100).clean()
        Promocion(name="x", kind="code", code="X100", discount_type="percent", value=100).clean()

    def test_discounts_dont_stack_the_lowest_price_wins(self):
        promo(name="Temporada", kind="offer", code=None, value=10, all_products=True)
        promo(code="VEINTE", value=20, all_products=True)
        promo(code="CINCO", value=5, all_products=True)
        assert self.quote().promotion.name == "Temporada"
        assert self.quote("VEINTE").price == Decimal("1199.60")
        q = self.quote("CINCO")
        assert q.promotion.name == "Temporada" and q.code_ignored and q.price == Decimal("1349.55")

    def test_offer_dates(self):
        now = timezone.now()
        promo(name="Pasada", kind="offer", code=None, value=50, all_products=True, ends_at=now)
        promo(name="Futura", kind="offer", code=None, value=50, all_products=True, starts_at=now + timedelta(days=1))
        promo(name="Apagada", kind="offer", code=None, value=50, all_products=True, active=False)
        assert self.quote().promotion is None
        promo(name="Del curso", kind="offer", code=None, value=15, products=[self.product],
              starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1))
        assert self.quote().promotion.name == "Del curso"

    def test_code_errors(self):
        now = timezone.now()
        other = Product.objects.create(title="Otro", number="O1", run="R", course_key="course-v1:x+O1+R",
                                       status=Product.Status.ON_SALE, price=100)
        promo(code="OTRO", products=[other])
        promo(code="VIEJO", all_products=True, ends_at=now)
        promo(code="LUEGO", all_products=True, starts_at=now + timedelta(hours=1))
        promo(code="APAGADO", all_products=True, active=False)
        cases = {"NOEXISTE": "invalid_code", "APAGADO": "invalid_code", "VIEJO": "expired",
                 "LUEGO": "not_started", "OTRO": "not_applicable"}
        for code, error in cases.items():
            with self.subTest(code), self.assertRaises(pricing.PromoError) as ctx:
                self.quote(code)
            assert ctx.exception.code == error
            assert ctx.exception.counts == (error == "invalid_code")

    def order(self, promotion, status=Order.Status.PAID, user=None, product=None, **fields):
        return Order.objects.create(user=user or User.objects.create(username=f"u{Order.objects.count()}"),
                                    product=product or self.product, course_key=KEY, amount=1, status=status,
                                    promotion=promotion, **fields)

    def test_uses_count_paid_refunded_and_open_checkouts(self):
        code = promo(code="TRES", all_products=True, max_uses=3, max_uses_per_user=None)
        self.order(code)
        self.order(code, Order.Status.REFUNDED)  # a refund doesn't give the use back
        self.order(code, Order.Status.FAILED)
        old = self.order(code, Order.Status.PENDING)
        Order.objects.filter(pk=old.pk).update(created=timezone.now() - pricing.RESERVATION - timedelta(minutes=1))
        assert pricing.uses(code)[0] == 2
        self.order(code, Order.Status.PENDING)  # someone in the checkout right now
        with self.assertRaises(pricing.PromoError) as ctx:
            self.quote("TRES")
        assert ctx.exception.code == "sold_out"

    def test_uses_per_person(self):
        other = Product.objects.create(title="Otro", number="O1", run="R", course_key="course-v1:x+O1+R",
                                       status=Product.Status.ON_SALE, price=100)
        code = promo(code="UNAVEZ", all_products=True)
        self.order(code, user=self.user, product=other)
        with self.assertRaises(pricing.PromoError) as ctx:
            self.quote("UNAVEZ", user=self.user)
        assert ctx.exception.code == "already_used"
        assert self.quote("UNAVEZ").code == "UNAVEZ"  # someone else can

    def test_own_open_checkout_for_the_same_course_is_not_a_use(self):
        code = promo(code="UNAVEZ", all_products=True)
        self.order(code, Order.Status.PENDING, user=self.user)
        assert self.quote("UNAVEZ", user=self.user).code == "UNAVEZ"

    def test_sold_out_offer_is_not_shown(self):
        offer = promo(name="Primeros 1", kind="offer", code=None, value=50, all_products=True, max_uses=1)
        assert self.quote().promotion == offer
        self.order(offer)
        assert self.quote().promotion is None


class CheckoutWithPromotionTests(SalesTestCase):
    def checkout(self, course_id=KEY, code=None, **extra):
        data = {"course_id": course_id, **({"code": code} if code is not None else {})}
        return self.client.post(reverse("checkout"), data, content_type="application/json",
                                **{**self.auth, **extra})

    @mock.patch("stripe.checkout.Session.create", return_value=SESSION)
    def test_code_lowers_what_stripe_charges(self, create):
        promotion = promo(products=[self.product])
        response = self.checkout(code=" buenfin ")

        assert response.status_code == 201, response.content
        order = Order.objects.get()
        assert (order.amount, order.list_price, order.discount, order.promotion, order.promotion_code) == (
            Decimal("1199.60"), Decimal("1499.50"), Decimal("299.90"), promotion, "BUENFIN")
        kwargs = create.call_args.kwargs
        assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 119960
        assert kwargs["metadata"]["promotion_code"] == "BUENFIN"
        assert kwargs["expires_at"] > (timezone.now() + pricing.RESERVATION).timestamp()

    @mock.patch("stripe.checkout.Session.create", return_value=SESSION)
    def test_offer_applies_without_code(self, create):
        promo(name="Lanzamiento", kind="offer", code=None, value=50, products=[self.product])
        self.checkout()
        assert Order.objects.get().amount == Decimal("749.75")
        assert create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"] == 74975

    @mock.patch("stripe.checkout.Session.create")
    def test_wrong_code_does_not_start_a_checkout(self, create):
        response = self.checkout(code="NOPE")
        assert response.status_code == 400
        assert response.json() == {"code": "invalid_code", "detail": "Este código no existe o ya no está activo."}
        assert not Order.objects.exists()
        create.assert_not_called()

    @mock.patch("stripe.checkout.Session.create")
    def test_wrong_codes_are_rate_limited(self, create):
        for _ in range(pricing.MAX_FAILED_PER_USER):
            self.checkout(code="NOPE")
        promo(products=[self.product])
        assert self.checkout(code="BUENFIN").status_code == 429

    @mock.patch("stripe.checkout.Session.create")
    def test_free_code_enrolls_without_stripe(self, create):
        promo(code="GRATIS", value=100, products=[self.product])
        response = self.checkout(code="gratis")

        assert response.status_code == 201, response.content
        assert response.json()["enrolled"] is True and response.json()["checkout_url"] is None
        order = Order.objects.get()
        assert (order.origin, order.amount, order.status, order.access, order.discount) == (
            Order.Origin.PROMOTION, 0, Order.Status.PAID, Order.Access.GRANTED, Decimal("1499.50"))
        assert self.enrollment() == {"mode": "verified", "is_active": True}
        create.assert_not_called()
        assert self.checkout(code="gratis").json()["code"] == "already_owned"

    @mock.patch("stripe.checkout.Session.create")
    def test_free_code_enrollment_failure_uses_nothing(self, create):
        promo(code="GRATIS", value=100, products=[self.product], max_uses=1)
        self.fake.fail.add("enroll")
        response = self.checkout(code="GRATIS")
        assert response.status_code == 503
        assert response.json()["code"] == "enrollment_failed"
        assert not Order.objects.exists()

    @mock.patch("stripe.checkout.Session.create", return_value=SESSION)
    def test_last_use_is_reserved_during_the_checkout(self, create):
        promo(products=[self.product], max_uses=1)
        assert self.checkout(code="BUENFIN").status_code == 201
        other = self.client.post(reverse("checkout"), {"course_id": KEY, "code": "BUENFIN"},
                                 content_type="application/json",
                                 **self.other_auth())
        assert other.json()["code"] == "sold_out"

    def other_auth(self):
        from core.tests.utils import auth_header
        auth = auth_header(user_id=12, username="beto", email="beto@example.com")
        self.client.get(reverse("me"), **auth)
        return auth

    @mock.patch("stripe.checkout.Session.create", side_effect=stripe.APIConnectionError("down"))
    def test_stripe_down_frees_the_reserved_use(self, create):
        promotion = promo(products=[self.product], max_uses=1)
        assert self.checkout(code="BUENFIN").status_code == 502
        assert Order.objects.get().status == Order.Status.FAILED
        assert pricing.uses(promotion)[0] == 0

    @mock.patch("stripe.checkout.Session.create", return_value=SimpleNamespace(id="cs_2", url="https://stripe.test/2"))
    @mock.patch("stripe.checkout.Session.retrieve", return_value=SimpleNamespace(status="open", url="https://stripe.test/1"))
    def test_open_checkout_is_reused_only_with_the_same_promotion(self, retrieve, create):
        promotion = promo(products=[self.product])
        self.make_order(amount="1199.60", promotion=promotion)
        assert self.checkout(code="BUENFIN").json()["checkout_url"] == "https://stripe.test/1"
        assert self.checkout().json()["checkout_url"] == "https://stripe.test/2"  # without the code: list price


class PublicApiTests(SalesTestCase):
    def test_quote_for_anyone(self):
        promo(name="Lanzamiento", kind="offer", code=None, value=10, products=[self.product])
        promo(products=[self.product])
        url = reverse("promotion-quote")
        data = self.client.post(url, {"course_id": KEY}, content_type="application/json").json()
        assert (data["price"], data["promotion"]["name"], data["code"]) == ("1349.55", "Lanzamiento", "")
        data = self.client.post(url, {"course_id": KEY, "code": "buenfin"}, content_type="application/json").json()
        assert (data["price"], data["discount"], data["code"], data["free"]) == ("1199.60", "299.90", "BUENFIN", False)
        bad = self.client.post(url, {"course_id": KEY, "code": "nope"}, content_type="application/json")
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_code"
        missing = self.client.post(url, {"course_id": "course-v1:x+y+z"}, content_type="application/json")
        assert missing.status_code == 404

    def test_quote_checks_the_per_person_limit_when_logged_in(self):
        promotion = promo(products=[self.product])
        other = Product.objects.create(title="Otro", number="O1", run="R", course_key="course-v1:x+O1+R",
                                       status=Product.Status.ON_SALE, price=100)
        Order.objects.create(user=self.user, product=other, course_key=other.course_key, amount=80,
                             status=Order.Status.PAID, promotion=promotion)
        promotion.all_products = True
        promotion.save()
        response = self.client.post(reverse("promotion-quote"), {"course_id": KEY, "code": "BUENFIN"},
                                    content_type="application/json", **self.auth)
        assert response.json()["code"] == "already_used"

    def test_catalog_shows_the_running_offer(self):
        assert self.client.get(reverse("catalog-detail", args=[KEY])).json()["offer"] is None
        ends = timezone.now() + timedelta(days=3)
        promo(name="Lanzamiento", kind="offer", code=None, value=50, products=[self.product], ends_at=ends)
        promo(products=[self.product])  # codes are never shown
        offer = self.client.get(reverse("catalog-detail", args=[KEY])).json()["offer"]
        assert (offer["name"], offer["price"], offer["discount"]) == ("Lanzamiento", 749.75, 749.75)
        assert offer["ends_at"] is not None
        listed = self.client.get(reverse("catalog-list")).json()
        assert listed[0]["offer"]["price"] == 749.75 and listed[0]["price"] == 1499.5


class ContadorApiTests(SalesTestCase):
    def setUp(self):
        super().setUp()
        call_command("contador", "contadora", stdout=mock.Mock())
        from core.tests.utils import auth_header
        self.admin = auth_header(username="contadora")

    def api(self, method, name, *args, data=None):
        return getattr(self.client, method)(reverse(name, args=args), data, content_type="application/json",
                                            **self.admin)

    def test_only_contadores(self):
        assert self.client.get(reverse("promotion-list"), **self.auth).status_code == 403

    def test_create_code_and_report(self):
        response = self.api("post", "promotion-list", data={
            "name": "Buen Fin", "kind": "code", "code": " buenfin26 ", "discount_type": "percent", "value": "25",
            "products": [self.product.pk], "max_uses": 100,
        })
        assert response.status_code == 201, response.content
        created = response.json()
        assert (created["code"], created["state"], created["max_uses_per_user"]) == ("BUENFIN26", "running", 1)
        assert created["report"] == {"uses": 0, "remaining": 100, "paid": 0, "refunded": 0, "revenue": "0.00",
                                     "discounts": "0.00"}
        Order.objects.create(user=self.user, product=self.product, course_key=KEY, amount="1124.63",
                             discount="374.87", status=Order.Status.PAID, promotion_id=created["id"])
        report = self.api("get", "promotion-detail", created["id"]).json()["report"]
        assert (report["uses"], report["paid"], report["revenue"], report["discounts"]) == (1, 1, "1124.63", "374.87")
        orders = self.client.get(reverse("order-list"), {"promotion": created["id"]}, **self.admin).json()
        assert orders["count"] == 1 and orders["results"][0]["promotion_code"] == ""
        assert self.api("delete", "promotion-detail", created["id"]).status_code == 400

    def test_offer_defaults_and_validation(self):
        response = self.api("post", "promotion-list", data={
            "name": "Temporada", "kind": "offer", "discount_type": "percent", "value": "30", "all_products": True,
        })
        assert response.status_code == 201, response.content
        assert response.json()["max_uses_per_user"] is None and response.json()["code"] is None

        bad = self.api("post", "promotion-list", data={
            "name": "Mal", "kind": "offer", "code": "XYZ", "discount_type": "percent", "value": "100", "products": [],
        })
        assert bad.status_code == 400
        assert {"code", "value"} <= set(bad.json())
        no_courses = self.api("post", "promotion-list", data={
            "name": "Sin cursos", "kind": "code", "code": "SINCURSO", "discount_type": "amount", "value": "100",
        })
        assert set(no_courses.json()) == {"products"}
        dup = self.api("post", "promotion-list", data={
            "name": "Otra", "kind": "code", "code": "temporada", "discount_type": "amount", "value": "1",
            "all_products": True,
        })
        assert dup.status_code == 201
        again = self.api("post", "promotion-list", data={
            "name": "Repetida", "kind": "code", "code": "TEMPORADA", "discount_type": "amount", "value": "1",
            "all_products": True,
        })
        assert again.json()["code"] == ["Ya hay una promoción con este código."]

    def test_dates_and_partial_update(self):
        response = self.api("post", "promotion-list", data={
            "name": "Fechas", "kind": "code", "code": "FECHAS", "discount_type": "amount", "value": "100",
            "all_products": True, "starts_at": "2030-01-02T00:00:00-06:00", "ends_at": "2030-01-01T00:00:00-06:00",
        })
        assert set(response.json()) == {"ends_at"}
        created = self.api("post", "promotion-list", data={
            "name": "Fechas", "kind": "code", "code": "FECHAS", "discount_type": "amount", "value": "100",
            "all_products": True, "starts_at": "2030-01-02T00:00:00-06:00",
        }).json()
        assert created["state"] == "scheduled"
        updated = self.api("patch", "promotion-detail", created["id"], data={"active": False})
        assert updated.status_code == 200 and updated.json()["state"] == "inactive"
        assert set(self.api("patch", "promotion-detail", created["id"], data={"kind": "offer"}).json()) == {"kind"}

    def test_used_promotion_keeps_its_discount(self):
        promotion = promo(products=[self.product])
        Order.objects.create(user=self.user, product=self.product, course_key=KEY, amount=1, status=Order.Status.PAID,
                             promotion=promotion)
        response = self.api("patch", "promotion-detail", promotion.pk, data={"value": "50"})
        assert set(response.json()) == {"value"}
        same = self.api("patch", "promotion-detail", promotion.pk, data={"value": "20.00", "name": "Otro"})
        assert same.status_code == 200
