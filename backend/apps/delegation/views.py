"""
代理授权视图
"""
import logging
from django.db.models import Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from apps.core.response import success_response, error_response
from .models import Delegation
from .serializers import (
    DelegationSerializer, DelegationCreateSerializer, DelegationRevokeSerializer
)
from .services import create_delegation, revoke_delegation, sweep_expired_delegations

logger = logging.getLogger('apps')


def _paginate(request, queryset):
    """与项目其他列表视图一致的手工分页"""
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 10))
    start = (page - 1) * page_size
    end = start + page_size
    return queryset.count(), queryset[start:end], page, page_size


class DelegationListView(APIView):
    """代理授权列表与创建视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Delegation.objects.select_related(
            'grantor', 'agent', 'revoked_by', 'root'
        ).order_by('-created_at')

        user = request.user
        if user.is_admin:
            # 管理员可查看全部，也可按范围筛选自己发出/收到的授权
            scope = request.query_params.get('scope')
            if scope == 'granted':
                queryset = queryset.filter(grantor=user)
            elif scope == 'received':
                queryset = queryset.filter(agent=user)
        else:
            queryset = queryset.filter(Q(grantor=user) | Q(agent=user))

        now = timezone.now()
        status_filter = request.query_params.get('status')
        if status_filter == 'active':
            queryset = queryset.filter(status='active', valid_from__lte=now, valid_to__gte=now)
        elif status_filter == 'revoked':
            queryset = queryset.filter(status='revoked')
        elif status_filter == 'expired':
            queryset = queryset.filter(status='active', valid_to__lt=now)

        total, delegations, page, page_size = _paginate(request, queryset)
        serializer = DelegationSerializer(delegations, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })

    def post(self, request):
        """创建代理授权（含转委托）"""
        if not request.user.is_admin:
            return error_response(message='只有具备审批权限的管理员才能发起授权', code=403)

        serializer = DelegationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            first_error = list(errors.values())[0]
            if isinstance(first_error, list):
                first_error = first_error[0]
            return error_response(message=str(first_error))

        data = serializer.validated_data
        delegation = create_delegation(
            grantor=request.user,
            agent=data['agent'],
            business_types=data['business_types'],
            max_quantity=data.get('max_quantity'),
            max_risk_level=data.get('max_risk_level'),
            valid_from=data['valid_from'],
            valid_to=data['valid_to'],
            allow_sub_delegation=data.get('allow_sub_delegation', False),
            reason=data.get('reason', ''),
            parent=data.get('parent'),
        )

        return success_response(data=DelegationSerializer(delegation).data, message='创建成功')


class DelegationDetailView(APIView):
    """代理授权详情视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        delegation = Delegation.objects.select_related(
            'grantor', 'agent', 'revoked_by', 'root'
        ).filter(pk=pk).first()
        if not delegation:
            return error_response(message='代理授权不存在', code=404)

        user = request.user
        if not user.is_admin and user.id not in (delegation.grantor_id, delegation.agent_id):
            return error_response(message='无权限查看该授权', code=403)

        return success_response(data=DelegationSerializer(delegation).data)


class DelegationRevokeView(APIView):
    """代理授权撤销视图"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        delegation = Delegation.objects.select_related('grantor', 'root').filter(pk=pk).first()
        if not delegation:
            return error_response(message='代理授权不存在', code=404)

        user = request.user
        # 授权人本人、授权链根授权人或管理员可以撤销
        if not (user.is_admin
                or user.id == delegation.grantor_id
                or user.id == delegation.original_grantor_id):
            return error_response(message='无权限撤销该授权', code=403)

        serializer = DelegationRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            first_error = list(errors.values())[0]
            if isinstance(first_error, list):
                first_error = first_error[0]
            return error_response(message=str(first_error))

        fallback = serializer.validated_data.get('fallback_approver')
        if fallback is not None:
            if not fallback.is_active:
                return error_response(message='接替审批人账号已被禁用')
            if not fallback.is_admin:
                return error_response(message='接替审批人必须具备审批权限（管理员）')
            if fallback.id == delegation.agent_id:
                return error_response(message='接替审批人不能是被撤销授权的代理人')
            if fallback.id == delegation.grantor_id:
                return error_response(message='接替审批人不能是授权人本人')

        result = revoke_delegation(
            delegation=delegation,
            revoked_by=user,
            reason=serializer.validated_data.get('reason', ''),
            fallback_approver=fallback,
        )

        return success_response(data={
            'reassigned': result['reassigned'],
            'reverted': result['reverted'],
        }, message='撤销成功')


class DelegationSweepView(APIView):
    """到期授权处理视图：重检未完成事项，使其重新获得合法审批人"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        processed = sweep_expired_delegations()

        logger.info(f"User {request.user.username} swept expired delegations: {processed}")

        return success_response(
            data={'processed': processed},
            message=f'已处理 {processed} 条到期授权'
        )
