import re
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from agreements import services
from agreements.models import CodigoConvenio, Convenio, RedeemAttempt, normalize_code
from agreements.views import client_ip
from catalog.models import Product
from catalog.tests.fake_openedx import FakeOpenEdx
from core.tests.utils import auth_header, use_test_jwt_keys
from sales.management.commands.reconcile_orders import Command as ReconcileOrders
from sales.models import Order

KEY = "course-v1:Campus+CONT101+2026_T1"
OTHER_KEY = "course-v1:Campus+FIN201+2026_T1"
CODE_RE = re.compile(r"^ACME-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}$")


class AgreementSetup:
    """Shared fixtures for TestCase and TransactionTestCase."""

    def make_world(self):
        self.fake = FakeOpenEdx().install(self)
        self.fake.modes[KEY] = {"verified": {"mode_slug": "verified"}}
        self.product = Product.objects.create(title="Contabilidad básica", number="CONT101", run="2026_T1",
                                              course_key=KEY, status=Product.Status.ON_SALE, price="1499.50")
        self.other = Product.objects.create(title="Finanzas", number="FIN201", run="2026_T1",
                                            course_key=OTHER_KEY, status=Product.Status.ON_SALE, price="999")
        self.convenio = Convenio.objects.create(
            company="ACME", contact_email="rh@acme.test", seats=5, code_prefix="ACME",
            expires_at=timezone.localdate() + timedelta(days=30),
        )
        self.convenio.products.add(self.product)
        self.code = CodigoConvenio.objects.create(convenio=self.convenio, code="ACME-7K3P-Q9XD")


@use_test_jwt_keys
class RedeemTests(AgreementSetup, TestCase):
    def setUp(self):
        self.make_world()
        self.auth = auth_header(user_id=11, username="alumna", email="alumna@example.com")
        self.client.get(reverse("me"), **self.auth)  # first login creates the CMS user
        self.user = User.objects.get(username="alumna")

    def redeem(self, code="ACME-7K3P-Q9XD", course_id=KEY, **extra):
        return self.client.post(reverse("agreement-redeem"), {"code": code, "course_id": course_id},
                                content_type="application/json", **{**self.auth, **extra})

    def assert_not_consumed(self):
        self.code.refresh_from_db()
        assert self.code.status == CodigoConvenio.Status.AVAILABLE
        assert self.code.redeemed_by is None and self.code.order is None
        assert not Order.objects.filter(origin=Order.Origin.AGREEMENT).exists()

    def test_redeem_enrolls_and_records_a_zero_order(self):
        response = self.redeem(" acme 7k3p q9xd ")  # typed loosely

        assert response.status_code == 201, response.content
        order = Order.objects.get(pk=response.json()["order_id"])
        assert (order.origin, order.amount, order.status, order.access) == (
            Order.Origin.AGREEMENT, 0, Order.Status.PAID, Order.Access.GRANTED)
        assert order.paid_at is not None and order.stripe_checkout_session_id is None
        self.code.refresh_from_db()
        assert (self.code.status, self.code.redeemed_by, self.code.product, self.code.order) == (
            CodigoConvenio.Status.USED, self.user, self.product, order)
        assert self.code.redeemed_at is not None
        assert self.fake.enrollment("alumna", KEY) == {"mode": "verified", "is_active": True}

    def test_created_course_can_be_redeemed(self):
        # Included in the agreement but not sold to the public.
        self.product.status = Product.Status.CREATED
        self.product.save()
        assert self.redeem().status_code == 201

    def test_used_code_is_rejected_like_an_unknown_one(self):
        self.code.status = CodigoConvenio.Status.USED
        self.code.save()
        used = self.redeem()
        unknown = self.redeem("ACME-2222-3333")
        voided = CodigoConvenio.objects.create(convenio=self.convenio, code="ACME-4444-5555",
                                               status=CodigoConvenio.Status.VOID)
        assert used.status_code == unknown.status_code == self.redeem(voided.code).status_code == 400
        assert used.json() == unknown.json() == {"code": "invalid_code",
                                                 "detail": "El código no es válido o ya fue utilizado."}
        assert self.redeem("hola").json()["code"] == "invalid_code"

    def test_expired_or_inactive_agreement(self):
        self.convenio.expires_at = timezone.localdate() - timedelta(days=1)
        self.convenio.save()
        response = self.redeem()
        assert (response.status_code, response.json()["code"]) == (400, "expired")
        self.assert_not_consumed()

        self.convenio.expires_at = timezone.localdate()  # the last day still counts
        self.convenio.active = False
        self.convenio.save()
        assert self.redeem().json()["code"] == "expired"
        self.assert_not_consumed()

    def test_course_not_included(self):
        response = self.redeem(course_id=OTHER_KEY)
        assert (response.status_code, response.json()["code"]) == (400, "course_not_included")
        self.assert_not_consumed()

    def test_product_status_is_checked_when_redeeming(self):
        self.product.status = Product.Status.ARCHIVED
        self.product.save()
        assert self.redeem().json()["code"] == "course_not_included"
        self.assert_not_consumed()

    def test_already_enrolled_does_not_consume_the_code(self):
        # Bought it with Stripe before.
        Order.objects.create(user=self.user, product=self.product, course_key=KEY, amount="1499.50",
                             status=Order.Status.PAID)
        response = self.redeem()
        assert (response.status_code, response.json()["code"]) == (409, "already_enrolled")
        self.code.refresh_from_db()
        assert self.code.status == CodigoConvenio.Status.AVAILABLE
        assert not RedeemAttempt.objects.exists()  # not a guess

    def test_verified_enrollment_from_elsewhere_counts_as_enrolled(self):
        self.fake.enrollments[("alumna", KEY)] = {"mode": "verified", "is_active": True}
        assert self.redeem().json()["code"] == "already_enrolled"
        self.assert_not_consumed()

    def test_audit_enrollment_is_upgraded(self):
        self.fake.enrollments[("alumna", KEY)] = {"mode": "audit", "is_active": True}
        assert self.redeem().status_code == 201
        assert self.fake.enrollment("alumna", KEY)["mode"] == "verified"

    def test_enrollment_failure_does_not_consume_the_code(self):
        self.fake.fail.add("enroll")
        response = self.redeem()
        assert (response.status_code, response.json()["code"]) == (503, "enrollment_failed")
        self.assert_not_consumed()
        assert not RedeemAttempt.objects.exists()

        self.fake.fail.clear()
        assert self.redeem().status_code == 201  # the same code works once Open edX is back

    def test_lms_down_while_checking_enrollment(self):
        self.fake.fail.add("read-enrollment")
        assert (self.redeem().status_code, self.redeem().json()["code"]) == (503, "unavailable")
        self.assert_not_consumed()

    def test_rollback_after_enrolling_is_logged(self):
        original = QuerySet.update

        def failing_update(queryset, **kwargs):
            if "order" in kwargs:
                raise RuntimeError("DB went away")
            return original(queryset, **kwargs)

        with mock.patch.object(QuerySet, "update", failing_update), \
                self.assertLogs("agreements.services", "WARNING") as logs, self.assertRaises(RuntimeError):
            self.redeem()
        assert "alumna" in logs.output[0] and KEY in logs.output[0]
        self.assert_not_consumed()

    def test_code_taken_between_validation_and_consume(self):
        # What the second of two simultaneous requests sees: the code was
        # available when it was validated, but the conditional UPDATE finds it used.
        stale = CodigoConvenio.objects.get(pk=self.code.pk)
        CodigoConvenio.objects.filter(pk=self.code.pk).update(status=CodigoConvenio.Status.USED)
        with self.assertRaises(services.RedeemError) as raised:
            services._consume(self.user, stale, self.product)
        assert raised.exception.code == "invalid_code"
        assert not Order.objects.exists()

    def test_requires_login(self):
        response = self.client.post(reverse("agreement-redeem"), {"code": "ACME-7K3P-Q9XD", "course_id": KEY},
                                    content_type="application/json")
        assert response.status_code == 401

    def test_rate_limit_per_user(self):
        for _ in range(services.MAX_FAILED_PER_USER):
            assert self.redeem("ACME-2222-3333").status_code == 400
        response = self.redeem()  # even the right code, until the window passes
        assert (response.status_code, response.json()["code"]) == (429, "too_many_attempts")
        self.assert_not_consumed()

        RedeemAttempt.objects.update(created=timezone.now() - services.ATTEMPT_WINDOW)
        assert self.redeem().status_code == 201

    def test_rate_limit_per_ip(self):
        other = User.objects.create(username="otro")
        RedeemAttempt.objects.bulk_create(RedeemAttempt(user=other, ip="203.0.113.9")
                                          for _ in range(services.MAX_FAILED_PER_IP))
        assert self.redeem(HTTP_X_FORWARDED_FOR="203.0.113.9").status_code == 429
        assert self.redeem(HTTP_X_FORWARDED_FOR="203.0.113.10").status_code == 201

    def test_old_attempts_are_purged(self):
        old = RedeemAttempt.objects.create(user=self.user)
        RedeemAttempt.objects.filter(pk=old.pk).update(created=timezone.now() - timedelta(hours=25))
        self.redeem("ACME-2222-3333")
        assert not RedeemAttempt.objects.filter(pk=old.pk).exists()

        RedeemAttempt.objects.update(created=timezone.now() - timedelta(hours=25))
        call_command("purge_redeem_attempts", stdout=mock.Mock())
        assert not RedeemAttempt.objects.exists()


class HelpersTests(TestCase):
    def test_normalize_code(self):
        assert normalize_code("acme 7k3p-q9xd") == "ACME-7K3P-Q9XD"
        assert normalize_code("ACME7K3PQ9XD") == "ACME-7K3P-Q9XD"
        assert normalize_code("7K3PQ9XD") is None
        assert normalize_code("") is None

    def test_client_ip_takes_the_entry_of_the_nearest_proxy(self):
        request = mock.Mock(META={"HTTP_X_FORWARDED_FOR": "1.2.3.4, 198.51.100.7", "REMOTE_ADDR": "10.0.0.2"})
        assert client_ip(request) == "198.51.100.7"
        assert client_ip(mock.Mock(META={"REMOTE_ADDR": "10.0.0.2"})) == "10.0.0.2"
        assert client_ip(mock.Mock(META={"HTTP_X_FORWARDED_FOR": "nope"})) is None


class GenerateTests(AgreementSetup, TestCase):
    def setUp(self):
        self.make_world()  # 5 seats, 1 code already

    def test_generates_readable_unique_codes_up_to_the_seats(self):
        codes = services.generate_codes(self.convenio, 4)
        assert len({c.code for c in codes}) == 4
        assert all(CODE_RE.match(c.code) for c in codes)
        with self.assertRaisesMessage(ValueError, "Solo quedan 0 lugares"):
            services.generate_codes(self.convenio, 1)

    def test_voided_unused_codes_free_their_seat_but_used_ones_dont(self):
        services.generate_codes(self.convenio, 4)
        self.code.status = CodigoConvenio.Status.VOID
        self.code.save()
        used = self.convenio.codes.exclude(pk=self.code.pk).first()
        CodigoConvenio.objects.filter(pk=used.pk).update(status=CodigoConvenio.Status.VOID, redeemed_at=timezone.now())
        assert len(services.generate_codes(self.convenio, 1)) == 1  # only the unused one is replaced
        with self.assertRaises(ValueError):
            services.generate_codes(self.convenio, 1)


class AdminTests(AgreementSetup, TestCase):
    def setUp(self):
        self.make_world()
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(self.admin)

    def test_generate_view(self):
        url = reverse("admin:agreements_convenio_generate", args=[self.convenio.pk])
        assert self.client.get(url).status_code == 200
        response = self.client.post(url, {"count": 10})
        assert "Solo quedan 4 lugares" in response.content.decode()
        response = self.client.post(url, {"count": 4})
        assert response.status_code == 302
        assert self.convenio.codes.count() == 5

    def test_export_csv(self):
        user = User.objects.create(username="beto")
        CodigoConvenio.objects.filter(pk=self.code.pk).update(
            status=CodigoConvenio.Status.USED, redeemed_by=user, product=self.product, redeemed_at=timezone.now())
        response = self.client.get(reverse("admin:agreements_convenio_export", args=[self.convenio.pk]))
        lines = response.content.decode("utf-8-sig").splitlines()
        assert lines[0] == "código,estado,usuario,correo,fecha de canje,curso,acceso retirado el"
        assert lines[1].startswith("ACME-7K3P-Q9XD,Usado,beto,,")

    def test_void_action_and_report(self):
        response = self.client.post(reverse("admin:agreements_codigoconvenio_changelist"), {
            "action": "void_codes", "_selected_action": [self.code.pk],
        })
        assert response.status_code == 302
        self.code.refresh_from_db()
        assert self.code.status == CodigoConvenio.Status.VOID
        page = self.client.get(reverse("admin:agreements_convenio_change", args=[self.convenio.pk])).content.decode()
        assert "Lugares comprados: <b>5</b>" in page and "anulados: 1" in page

    def test_revoke_access_action(self):
        student = User.objects.create(username="alumna")
        services.redeem(student, self.code.code, KEY)
        response = self.client.post(reverse("admin:agreements_codigoconvenio_changelist"), {
            "action": "revoke_access", "_selected_action": [self.code.pk],
        })
        assert response.status_code == 302
        self.code.refresh_from_db()
        assert (self.code.status, self.code.revoked_by) == (CodigoConvenio.Status.REVOKED, self.admin)
        assert self.fake.enrollment("alumna", KEY) == {"mode": "verified", "is_active": False}

    def test_only_existing_courses_can_be_included(self):
        draft = Product.objects.create(title="Borrador", number="X1", run="R")
        form_url = reverse("admin:agreements_convenio_add")
        response = self.client.post(form_url, {
            "company": "Beta", "contact_email": "a@beta.test", "products": [draft.pk], "seats": 3,
            "code_prefix": "beta", "expires_at": "2030-01-01", "active": "on", "notes": "",
            "codes-TOTAL_FORMS": 0, "codes-INITIAL_FORMS": 0,
        })
        assert response.status_code == 200  # form redisplayed with the error
        assert not Convenio.objects.filter(company="Beta").exists()


@use_test_jwt_keys
class ContadorApiTests(AgreementSetup, TestCase):
    """What the Campus admin (/admin/convenios) calls, as a contador."""

    def setUp(self):
        self.make_world()
        call_command("contador", "contadora", stdout=mock.Mock())
        self.auth = auth_header(username="contadora")

    def api(self, method, name, *args, data=None):
        url = reverse(name, args=args)
        return getattr(self.client, method)(url, data, content_type="application/json", **self.auth)

    def test_only_contadores(self):
        other = auth_header(user_id=12, username="alumno", email="alumno@example.com")
        assert self.client.get(reverse("agreement-list"), **other).status_code == 403

    def test_create_list_and_report(self):
        response = self.api("post", "agreement-list", data={
            "company": "Beta SA", "contact_email": "rh@beta.test", "products": [self.product.pk], "seats": 3,
            "code_prefix": " beta ", "expires_at": "2030-01-31", "active": True, "notes": "Factura 123",
        })
        assert response.status_code == 201, response.content
        created = response.json()
        assert created["code_prefix"] == "BETA"
        assert created["report"] == {"seats": 3, "generated": 0, "used": 0, "available": 0, "void": 0,
                                     "revoked": 0, "free_seats": 3}
        assert created["products_detail"][0]["course_key"] == KEY
        assert len(self.api("get", "agreement-list").json()) == 2

    def test_rejects_draft_courses_and_bad_prefix(self):
        draft = Product.objects.create(title="Borrador", number="X1", run="R")
        response = self.api("post", "agreement-list", data={
            "company": "Beta", "contact_email": "rh@beta.test", "products": [draft.pk], "seats": 3,
            "code_prefix": "B0", "expires_at": "2030-01-31",
        })
        assert response.status_code == 400
        assert set(response.json()) == {"products", "code_prefix"}

    def test_generate_codes_void_and_export(self):
        response = self.api("post", "agreement-generate", self.convenio.pk, data={"count": 10})
        assert response.status_code == 400
        assert response.json()["count"] == "Solo quedan 4 lugares sin código en este convenio."
        response = self.api("post", "agreement-generate", self.convenio.pk, data={"count": 4})
        assert response.status_code == 201 and len(response.json()) == 4

        response = self.api("post", "agreement-void", self.convenio.pk, data={"ids": [self.code.pk]})
        assert response.json() == {"voided": 1}
        codes = self.api("get", "agreement-codes", self.convenio.pk).json()
        assert [c["status"] for c in codes].count("void") == 1
        assert self.api("get", "agreement-detail", self.convenio.pk).json()["report"]["free_seats"] == 1

        csv_text = self.client.get(reverse("agreement-export", args=[self.convenio.pk]), **self.auth)
        assert csv_text["Content-Type"].startswith("text/csv")
        assert len(csv_text.content.decode("utf-8-sig").splitlines()) == 6

    def revoke(self, *codes):
        return self.api("post", "agreement-revoke", self.convenio.pk, data={"ids": [c.pk for c in codes]})

    def test_revoke_access_unenrolls_and_frees_the_seat(self):
        student = User.objects.create(username="alumna", email="alumna@example.com")
        order = services.redeem(student, self.code.code, KEY)
        unused = CodigoConvenio.objects.create(convenio=self.convenio, code="ACME-2222-3333")

        response = self.revoke(self.code, unused)

        assert response.status_code == 200, response.content
        assert response.json() == {"revoked": 1, "pending": 0}  # the unused code is skipped
        assert self.fake.enrollment("alumna", KEY) == {"mode": "verified", "is_active": False}
        order.refresh_from_db()
        assert (order.status, order.access) == (Order.Status.CANCELLED, Order.Access.REVOKED)
        self.code.refresh_from_db()
        assert self.code.status == CodigoConvenio.Status.REVOKED
        assert self.code.revoked_at is not None and self.code.revoked_by.username == "contadora"
        assert self.code.redeemed_by == student  # the history stays
        unused.refresh_from_db()
        assert unused.status == CodigoConvenio.Status.AVAILABLE

        report = self.api("get", "agreement-detail", self.convenio.pk).json()["report"]
        assert (report["used"], report["revoked"], report["free_seats"]) == (0, 1, 4)
        codes = {c["code"]: c for c in self.api("get", "agreement-codes", self.convenio.pk).json()}
        assert codes[self.code.code]["access"] == "revoked"
        assert codes[self.code.code]["revoked_by"] == "contadora"
        # Doing it again changes nothing.
        assert self.revoke(self.code).json() == {"revoked": 0, "pending": 0}

    def test_after_revoking_the_student_can_redeem_a_new_code(self):
        student = User.objects.create(username="alumna")
        services.redeem(student, self.code.code, KEY)
        self.revoke(self.code)
        new = CodigoConvenio.objects.create(convenio=self.convenio, code="ACME-2222-3333")
        order = services.redeem(student, new.code, KEY)
        assert (order.status, order.access) == (Order.Status.PAID, Order.Access.GRANTED)
        assert self.fake.enrollment("alumna", KEY) == {"mode": "verified", "is_active": True}

    def test_revoke_with_open_edx_down_is_retried(self):
        student = User.objects.create(username="alumna")
        order = services.redeem(student, self.code.code, KEY)
        self.fake.fail.add("unenroll")

        assert self.revoke(self.code).json() == {"revoked": 1, "pending": 1}
        order.refresh_from_db()
        assert (order.status, order.access) == (Order.Status.CANCELLED, Order.Access.ERROR)

        self.fake.fail.clear()
        ReconcileOrders(stdout=mock.Mock(), stderr=mock.Mock()).retry_access()  # what reconcile_orders retries
        order.refresh_from_db()
        assert order.access == Order.Access.REVOKED
        assert self.fake.enrollment("alumna", KEY)["is_active"] is False

    def test_seats_cant_drop_below_codes_and_delete_needs_no_codes(self):
        response = self.api("patch", "agreement-detail", self.convenio.pk, data={"seats": 0})
        assert response.status_code == 400
        assert self.api("delete", "agreement-detail", self.convenio.pk).status_code == 400
        self.code.delete()
        assert self.api("delete", "agreement-detail", self.convenio.pk).status_code == 204


@skipUnlessDBFeature("has_select_for_update")  # SQLite has no row locks: run on MySQL (Tutor)
class ConcurrentRedeemTests(AgreementSetup, TransactionTestCase):
    def setUp(self):
        self.make_world()
        self.users = [User.objects.create(username=f"empleado{i}") for i in range(2)]

    def test_one_code_two_simultaneous_requests(self):
        barrier = threading.Barrier(2)
        results = []
        slow_enroll = self.fake.lms

        def lms(method, path, **kwargs):
            if path == "/api/enrollment/v1/enrollment":
                threading.Event().wait(0.3)  # keep the first transaction open while the second arrives
            return slow_enroll(method, path, **kwargs)

        def worker(user):
            try:
                barrier.wait()
                services.redeem(user, "ACME-7K3P-Q9XD", KEY)
                results.append("ok")
            except services.RedeemError as exc:
                results.append(exc.code)
            finally:
                connection.close()

        with mock.patch("core.openedx.lms", side_effect=lms):
            threads = [threading.Thread(target=worker, args=(user,)) for user in self.users]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert sorted(results) == ["invalid_code", "ok"]
        assert Order.objects.filter(origin=Order.Origin.AGREEMENT).count() == 1
        self.code.refresh_from_db()
        assert self.code.status == CodigoConvenio.Status.USED
