from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from catalog.models import Product
from core.http import client_ip
from core.permissions import IsContador

from . import pricing
from .models import Promocion
from .serializers import PromocionSerializer


def quote_data(price_quote):
    promotion = price_quote.promotion
    return {
        "list_price": str(price_quote.list_price),
        "discount": str(price_quote.discount),
        "price": str(price_quote.price),
        "free": price_quote.free,
        "code": price_quote.code,
        "code_ignored": price_quote.code_ignored,
        "promotion": {
            "name": promotion.name,
            "kind": promotion.kind,
            "ends_at": promotion.ends_at,
        } if promotion is not None else None,
    }


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def quote(request):
    """
    POST /api/v1/promotions/quote/  {"course_id", "code" (optional)} — public.

    The price of a course on sale now, with the running offer or the code.
    Logged in, the per-person limit is checked too. → 200 {list_price,
    discount, price, free, code, code_ignored, promotion}. Errors {"code",
    "detail"}: invalid_code | not_started | expired | not_applicable |
    sold_out | already_used (400), too_many_attempts (429), not_purchasable (404).
    """
    product = Product.objects.filter(
        course_key=str(request.data.get("course_id", "")), status=Product.Status.ON_SALE, price__isnull=False,
    ).first()
    if product is None:
        return Response({"code": "not_purchasable", "detail": "Este curso no está a la venta."}, status=404)
    code = str(request.data.get("code") or "")[:64]
    try:
        price_quote = pricing.quote_with_limits(product, request.user, code, ip=client_ip(request))
    except pricing.PromoError as exc:
        return Response({"code": exc.code, "detail": str(exc)}, status=exc.status)
    return Response(quote_data(price_quote))


class PromocionViewSet(viewsets.ModelViewSet):
    """
    /api/v1/promotions/ — offers and discount codes, for contadores. ?q= by
    name or code, ?kind=offer|code. DELETE only while nobody used it
    (deactivate it otherwise). Its orders: /api/v1/orders/?promotion={id}.
    """

    serializer_class = PromocionSerializer
    permission_classes = [IsContador]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        queryset = Promocion.objects.prefetch_related("products")
        params = self.request.query_params
        if params.get("q"):
            queryset = queryset.filter(Q(name__icontains=params["q"]) | Q(code__icontains=params["q"]))
        if params.get("kind"):
            queryset = queryset.filter(kind=params["kind"])
        return queryset

    def perform_destroy(self, instance):
        if instance.orders.exists():
            raise ValidationError("Ya se usó: desactívala en lugar de borrarla.")
        instance.delete()
