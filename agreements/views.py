import ipaddress

from django.db.models import Q
from django.http import HttpResponse
from django.utils.text import slugify
from rest_framework import viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from core.permissions import IsContador

from . import services
from .models import Convenio
from .serializers import CodigoConvenioSerializer, ConvenioSerializer


def client_ip(request):
    """
    The browser's IP, for the rate limit. Campus' server calls this API and
    replaces X-Forwarded-For with the client IP; a call that comes in through
    Caddy gets the caller's IP there. The last entry is the one the nearest
    proxy wrote, so that's the one we take.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


@api_view(["POST"])
def redeem(request):
    """
    POST /api/v1/agreements/redeem/  {"code": "ACME-7K3P-Q9XD", "course_id": "course-v1:..."}

    Redeems a company agreement code as the logged-in user and enrolls them.
    → 201 {"order_id", "course_id"}
    Errors {"code", "detail"}: invalid_code | expired | course_not_included (400),
    already_enrolled (409), too_many_attempts (429), unavailable | enrollment_failed (503).
    """
    course_id = str(request.data.get("course_id", ""))
    try:
        order = services.redeem(request.user, str(request.data.get("code", "")), course_id, ip=client_ip(request))
    except services.RedeemError as exc:
        return Response({"code": exc.code, "detail": str(exc)}, status=exc.status)
    return Response({"order_id": order.pk, "course_id": order.course_key}, status=201)


# --- for the contadores (Campus admin) ------------------------------------------------


class ConvenioViewSet(viewsets.ModelViewSet):
    """
    /api/v1/agreements/ — company agreements, for contadores.

    GET    /agreements/{id}/codes/?status=   its codes (who redeemed each, and when)
    POST   /agreements/{id}/generate/        {"count"} → new codes, up to the seats
    POST   /agreements/{id}/void/            {"ids": [...]} → voids those codes
    GET    /agreements/{id}/export/          CSV of its codes
    DELETE only while it has no codes (deactivate it otherwise).

    Voiding a used code, deactivating or expiring the agreement never removes
    anyone's access: it only stops new redeems.
    """

    serializer_class = ConvenioSerializer
    permission_classes = [IsContador]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        queryset = Convenio.objects.prefetch_related("products")
        params = self.request.query_params
        if params.get("q"):
            queryset = queryset.filter(Q(company__icontains=params["q"]) | Q(contact_email__icontains=params["q"]))
        return queryset

    def perform_destroy(self, instance):
        if instance.codes.exists():
            raise ValidationError("Ya tiene códigos: desactívalo en lugar de borrarlo.")
        instance.delete()

    @action(detail=True, methods=["get"])
    def codes(self, request, pk=None):
        codes = self.get_object().codes.select_related("redeemed_by", "product")
        if request.query_params.get("status"):
            codes = codes.filter(status=request.query_params["status"])
        return Response(CodigoConvenioSerializer(codes, many=True).data)

    @action(detail=True, methods=["post"])
    def generate(self, request, pk=None):
        convenio = self.get_object()
        try:
            count = int(request.data.get("count"))
        except (TypeError, ValueError):
            raise ValidationError({"count": "Indica cuántos códigos generar."}) from None
        try:
            codes = services.generate_codes(convenio, count)
        except ValueError as exc:
            raise ValidationError({"count": str(exc)}) from exc
        return Response(CodigoConvenioSerializer(codes, many=True).data, status=201)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        convenio = self.get_object()
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "Elige los códigos a anular."})
        voided = services.void_codes(convenio.codes.filter(pk__in=ids))
        return Response({"voided": voided})

    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        convenio = self.get_object()
        response = HttpResponse(services.codes_csv([convenio]), content_type="text/csv; charset=utf-8")
        name = slugify(convenio.company) or convenio.pk
        response["Content-Disposition"] = f'attachment; filename="codigos-{name}.csv"'
        return response
