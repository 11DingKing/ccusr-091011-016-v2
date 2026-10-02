"""
代理授权序列化器
"""
from rest_framework import serializers
from django.utils import timezone
from apps.authentication.models import User
from .models import Delegation, BUSINESS_TYPE_CHOICES, RISK_LEVEL_CHOICES


class DelegationSerializer(serializers.ModelSerializer):
    """代理授权序列化器"""
    grantor_name = serializers.CharField(source='grantor.username', read_only=True)
    agent_name = serializers.CharField(source='agent.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    display_status = serializers.SerializerMethodField()
    is_effective = serializers.SerializerMethodField()
    business_type_displays = serializers.SerializerMethodField()
    max_risk_level_display = serializers.SerializerMethodField()
    revoked_by_name = serializers.SerializerMethodField()
    chain = serializers.SerializerMethodField()

    class Meta:
        model = Delegation
        fields = [
            'id', 'grantor', 'grantor_name', 'agent', 'agent_name',
            'business_types', 'business_type_displays',
            'max_quantity', 'max_risk_level', 'max_risk_level_display',
            'valid_from', 'valid_to', 'allow_sub_delegation',
            'parent', 'root', 'reason',
            'status', 'status_display', 'display_status', 'is_effective',
            'revoked_at', 'revoked_by', 'revoked_by_name', 'revoke_reason',
            'chain', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_display_status(self, obj):
        """展示状态：已撤销/未生效/已到期/生效中"""
        now = timezone.now()
        if obj.status == 'revoked':
            return '已撤销'
        if now < obj.valid_from:
            return '未生效'
        if obj.is_expired(now):
            return '已到期'
        return '生效中'

    def get_is_effective(self, obj):
        return obj.is_effective()

    def get_business_type_displays(self, obj):
        mapping = dict(BUSINESS_TYPE_CHOICES)
        return [mapping.get(code, code) for code in (obj.business_types or [])]

    def get_max_risk_level_display(self, obj):
        return obj.get_max_risk_level_display() if obj.max_risk_level else None

    def get_revoked_by_name(self, obj):
        return obj.revoked_by.username if obj.revoked_by else None

    def get_chain(self, obj):
        """授权链：从根授权到当前授权的完整链条"""
        return [{
            'id': link.id,
            'grantor': link.grantor_id,
            'grantor_name': link.grantor.username,
            'agent': link.agent_id,
            'agent_name': link.agent.username,
            'valid_from': link.valid_from,
            'valid_to': link.valid_to,
        } for link in obj.get_chain()]


class DelegationCreateSerializer(serializers.Serializer):
    """代理授权创建序列化器"""
    agent = serializers.IntegerField(required=True, error_messages={
        'required': '请选择代理人',
    })
    business_types = serializers.ListField(
        child=serializers.CharField(), allow_empty=False, required=True,
        error_messages={'required': '请选择可代理的业务种类', 'empty': '请选择可代理的业务种类'}
    )
    max_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    max_risk_level = serializers.ChoiceField(
        choices=RISK_LEVEL_CHOICES, required=False, allow_null=True
    )
    valid_from = serializers.DateTimeField(required=True, error_messages={
        'required': '请选择生效时间',
    })
    valid_to = serializers.DateTimeField(required=True, error_messages={
        'required': '请选择失效时间',
    })
    allow_sub_delegation = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    parent = serializers.IntegerField(required=False, allow_null=True)

    def validate_agent(self, value):
        try:
            return User.objects.get(pk=value)
        except User.DoesNotExist:
            raise serializers.ValidationError('代理人不存在')

    def validate_parent(self, value):
        if value is None:
            return None
        try:
            return Delegation.objects.get(pk=value)
        except Delegation.DoesNotExist:
            raise serializers.ValidationError('上级授权不存在')


class DelegationRevokeSerializer(serializers.Serializer):
    """代理授权撤销序列化器"""
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    fallback_approver = serializers.IntegerField(required=False, allow_null=True)

    def validate_fallback_approver(self, value):
        if value is None:
            return None
        try:
            return User.objects.get(pk=value)
        except User.DoesNotExist:
            raise serializers.ValidationError('接替审批人不存在')
