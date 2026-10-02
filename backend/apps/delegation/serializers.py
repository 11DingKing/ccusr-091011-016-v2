"""
代理授权序列化器
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.authentication.models import User
from .models import (
    BUSINESS_CHOICES,
    RISK_LEVEL_CHOICES,
    DelegationPolicy,
    ApprovalAct,
)


class DelegationPolicySerializer(serializers.ModelSerializer):
    """代理授权策略输出"""

    principal_name = serializers.CharField(source="principal.display_name", read_only=True)
    agent_name = serializers.CharField(source="agent.display_name", read_only=True)
    business_type_display = serializers.CharField(source="get_business_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    risk_level_display = serializers.SerializerMethodField()
    effective_now = serializers.SerializerMethodField()

    class Meta:
        model = DelegationPolicy
        fields = [
            "id", "principal", "principal_name",
            "agent", "agent_name",
            "business_type", "business_type_display",
            "max_amount", "max_risk_level", "risk_level_display",
            "start_time", "end_time",
            "status", "status_display", "effective_now",
            "revoked_at", "revoked_by", "reason", "created_at",
        ]

    def get_risk_level_display(self, obj):
        return dict(RISK_LEVEL_CHOICES).get(obj.max_risk_level, "不限")

    def get_effective_now(self, obj):
        return obj.is_effective()


class DelegationCreateSerializer(serializers.Serializer):
    """创建代理授权"""

    agent_id = serializers.IntegerField(label="代理人ID")
    business_type = serializers.ChoiceField(
        choices=BUSINESS_CHOICES, default="stock_out_approval")
    max_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True, min_value=0)
    max_risk_level = serializers.ChoiceField(
        choices=RISK_LEVEL_CHOICES, required=False, allow_blank=True, default="")
    start_time = serializers.DateTimeField(required=False, allow_null=True)
    end_time = serializers.DateTimeField(required=True)
    reason = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default="")

    def validate_agent_id(self, value):
        if not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError("代理人不存在或已停用")
        return value

    def validate(self, attrs):
        principal = self.context["principal"]
        if attrs["agent_id"] == principal.id:
            # 双重保险：模型层另有校验
            raise serializers.ValidationError({"agent_id": "不允许自己给自己授权"})
        start = attrs.get("start_time")
        end = attrs["end_time"]
        now = timezone.now()
        if end <= (start or now):
            raise serializers.ValidationError(
                {"end_time": "失效时间必须晚于生效时间（或当前时间）"})
        if start and start < now - timedelta(minutes=1):
            raise serializers.ValidationError({"start_time": "生效时间不能早于当前时间"})
        return attrs


class ApprovalActSerializer(serializers.ModelSerializer):
    """审批行为凭证：清楚区分实际操作者与原授权人"""

    operator_name = serializers.CharField(source="operator.display_name", read_only=True)
    original_grantor_name = serializers.CharField(
        source="original_grantor.display_name", read_only=True)
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)
    business_type_display = serializers.CharField(
        source="get_business_type_display", read_only=True)
    is_delegated = serializers.BooleanField(read_only=True)
    authority_description = serializers.SerializerMethodField()
    chain = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalAct
        fields = [
            "id", "business_type", "business_type_display", "subject_id",
            "operator", "operator_name",
            "original_grantor", "original_grantor_name",
            "is_delegated", "decision", "decision_display",
            "amount", "risk_level",
            "authority_description", "chain",
            "remark", "acted_at",
        ]

    def get_authority_description(self, obj):
        return obj.authority_summary()

    def get_chain(self, obj):
        return obj.authority_chain
