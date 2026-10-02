"""
代理授权视图：授权的授予、撤销、查询，以及审批授权链台账查询。
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.authentication.models import User
from apps.core.response import success_response, error_response
from .models import (
    DelegationPolicy,
    ApprovalAct,
    effective_delegations_for,
    expire_due_policies,
)
from .serializers import (
    DelegationPolicySerializer,
    DelegationCreateSerializer,
    ApprovalActSerializer,
)

logger = logging.getLogger("apps")


def _paginate(request, queryset):
    page = max(int(request.query_params.get("page", 1)), 1)
    page_size = min(max(int(request.query_params.get("page_size", 10)), 1), 100)
    start = (page - 1) * page_size
    return queryset[start:start + page_size], queryset.count(), page, page_size


class DelegationListCreateView(APIView):
    """代理授权列表 / 授予授权"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        expire_due_policies()
        qs = DelegationPolicy.objects.select_related("principal", "agent").all()

        direction = request.query_params.get("direction")
        if direction == "granted":
            # 我授予他人的
            qs = qs.filter(principal=request.user)
        elif direction == "received":
            # 我收到的
            qs = qs.filter(agent=request.user)

        status = request.query_params.get("status")
        if status in ("active", "expired", "revoked"):
            qs = qs.filter(status=status)

        business_type = request.query_params.get("business_type")
        if business_type:
            qs = qs.filter(business_type=business_type)

        items, total, page, page_size = _paginate(request, qs)
        return success_response(data={
            "list": DelegationPolicySerializer(items, many=True).data,
            "total": total, "page": page, "page_size": page_size,
        })

    def post(self, request):
        # 仅具备审批角色者可将审批权授予他人
        if not request.user.is_admin:
            return error_response(
                message="只有审批负责人才可以授予代理授权", code=403)

        serializer = DelegationCreateSerializer(
            data=request.data, context={"principal": request.user})
        if not serializer.is_valid():
            first = next(iter(serializer.errors.values()))[0]
            return error_response(message=str(first))

        data = serializer.validated_data
        agent = User.objects.get(pk=data["agent_id"])
        try:
            with transaction.atomic():
                policy = DelegationPolicy.objects.create(
                    principal=request.user,
                    agent=agent,
                    business_type=data["business_type"],
                    max_amount=data.get("max_amount"),
                    max_risk_level=data.get("max_risk_level", ""),
                    start_time=data.get("start_time"),
                    end_time=data["end_time"],
                    reason=data.get("reason", ""),
                )
        except DjangoValidationError as exc:
            if hasattr(exc, "message_dict"):
                message = "; ".join(
                    str(v[0]) if isinstance(v, list) else str(v)
                    for v in exc.message_dict.values())
            else:
                message = "; ".join(exc.messages)
            return error_response(message=message)

        logger.info(
            "用户 %s 授予 %s 代理授权 #%s（%s）",
            request.user.username, agent.username, policy.id,
            policy.get_business_type_display(),
        )
        return success_response(
            data=DelegationPolicySerializer(policy).data, message="授权成功")


class DelegationDetailView(APIView):
    """授权详情 / 撤销"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            policy = DelegationPolicy.objects.select_related(
                "principal", "agent").get(pk=pk)
        except DelegationPolicy.DoesNotExist:
            return error_response(message="授权不存在", code=404)
        return success_response(data=DelegationPolicySerializer(policy).data)

    def post(self, request, pk):
        return self._revoke(request, pk)

    def delete(self, request, pk):
        return self._revoke(request, pk)

    def _revoke(self, request, pk):
        try:
            policy = DelegationPolicy.objects.get(pk=pk)
        except DelegationPolicy.DoesNotExist:
            return error_response(message="授权不存在", code=404)

        # 仅原授权人本人或超管可撤销
        if policy.principal_id != request.user.id and not request.user.is_super_admin:
            return error_response(message="只有原授权人可以撤销该授权", code=403)

        if not policy.revoke(revoked_by=request.user):
            return error_response(message="该授权已终结，无法撤销")
        logger.info("用户 %s 撤销了代理授权 #%s", request.user.username, pk)
        return success_response(data=DelegationPolicySerializer(policy).data,
                                message="授权已撤销")


class MyEffectiveDelegationsView(APIView):
    """我当前持有的有效代理授权（供前端展示"代谁处理"）"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        policies = effective_delegations_for(request.user, "stock_out_approval")
        return success_response(
            data=DelegationPolicySerializer(policies, many=True).data)


class ApprovalActQueryView(APIView):
    """
    审批授权链台账查询：每次决定都清楚标注
    实际操作者(operator)与原授权人(original_grantor)。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ApprovalAct.objects.select_related(
            "operator", "original_grantor", "delegation").all()

        subject_id = request.query_params.get("subject_id")
        if subject_id:
            qs = qs.filter(subject_id=subject_id)
        business_type = request.query_params.get("business_type")
        if business_type:
            qs = qs.filter(business_type=business_type)
        delegated = request.query_params.get("delegated")
        if delegated == "true":
            qs = qs.exclude(delegation__isnull=True)
        elif delegated == "false":
            qs = qs.filter(delegation__isnull=True)
        operator_id = request.query_params.get("operator_id")
        if operator_id:
            qs = qs.filter(operator_id=operator_id)
        grantor_id = request.query_params.get("original_grantor_id")
        if grantor_id:
            qs = qs.filter(original_grantor_id=grantor_id)

        items, total, page, page_size = _paginate(request, qs)
        return success_response(data={
            "list": ApprovalActSerializer(items, many=True).data,
            "total": total, "page": page, "page_size": page_size,
        })
