"""
仓库管理序列化器
"""
from decimal import Decimal

from rest_framework import serializers
from apps.authentication.models import User
from .models import Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval


class UnitSerializer(serializers.ModelSerializer):
    """单位序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UnitCreateSerializer(serializers.Serializer):
    """单位创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=5, required=True, error_messages={
        'required': '请输入单位名称',
        'blank': '单位名称不能为空',
        'min_length': '单位名称至少1个字',
        'max_length': '单位名称最多5个字',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Unit.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('单位名称已存在')
        else:
            if Unit.objects.filter(name=value).exists():
                raise serializers.ValidationError('单位名称已存在')
        return value


class CategorySerializer(serializers.ModelSerializer):
    """品类序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'unit', 'unit_name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CategoryCreateSerializer(serializers.Serializer):
    """品类创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=10, required=True, error_messages={
        'required': '请输入品类名称',
        'blank': '品类名称不能为空',
        'min_length': '品类名称至少1个字',
        'max_length': '品类名称最多10个字',
    })
    unit = serializers.IntegerField(required=True, error_messages={
        'required': '请选择单位',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Category.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('品类名称已存在')
        else:
            if Category.objects.filter(name=value).exists():
                raise serializers.ValidationError('品类名称已存在')
        return value
    
    def validate_unit(self, value):
        if not Unit.objects.filter(pk=value).exists():
            raise serializers.ValidationError('单位不存在')
        return value


class VarietySerializer(serializers.ModelSerializer):
    """品种序列化器"""
    is_in_stock = serializers.BooleanField(read_only=True)
    unit_name = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Variety
        fields = [
            'id', 'name', 'category', 'category_name', 'unit_name',
            'is_in_stock', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VarietyCreateSerializer(serializers.Serializer):
    """品种创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入品种名称',
        'blank': '品种名称不能为空',
        'min_length': '品种名称至少1个字',
        'max_length': '品种名称最多20个字',
    })
    category = serializers.IntegerField(required=True, error_messages={
        'required': '请选择品类',
    })
    
    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value
    
    def validate(self, data):
        instance = self.context.get('instance')
        name = data['name']
        category_id = data['category']
        
        if instance:
            if Variety.objects.filter(name=name, category_id=category_id).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        else:
            if Variety.objects.filter(name=name, category_id=category_id).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        return data


class GoodsSerializer(serializers.ModelSerializer):
    """货物序列化器"""
    variety_name = serializers.CharField(source='variety.name', read_only=True)
    category_name = serializers.CharField(source='variety.category.name', read_only=True)
    unit_name = serializers.CharField(source='variety.category.unit.name', read_only=True)
    is_warning = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'location',
            'remark', 'is_active', 'is_warning',
            'created_at', 'updated_at'
        ]


class StockInSerializer(serializers.ModelSerializer):
    """入库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    
    class Meta:
        model = StockIn
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'quantity', 'batch_no', 'supplier', 'stock_in_time', 'remark'
        ]


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.display_name', read_only=True)
    approver_owner_name = serializers.CharField(
        source='approver_owner.display_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)

    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity',
            'amount', 'risk_level', 'risk_level_display',
            'status', 'status_display',
            'approver_owner', 'approver_owner_name',
            'stock_out_time', 'remark', 'created_at'
        ]
        read_only_fields = ['status', 'created_at']


class StockOutCreateSerializer(serializers.Serializer):
    """出库申请创建序列化器"""
    goods = serializers.IntegerField(required=True, error_messages={'required': '请选择货物'})
    receiver = serializers.CharField(min_length=1, max_length=100, required=True,
                                     error_messages={'required': '请填写领用人', 'blank': '领用人不能为空'})
    receiver_dept = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'),
                                        required=True, error_messages={'required': '请填写出库数量'})
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False,
                                      allow_null=True, min_value=0)
    risk_level = serializers.ChoiceField(
        choices=[('normal', '普通'), ('important', '重要'), ('critical', '重大')],
        required=False, default='normal')
    approver_owner = serializers.IntegerField(required=False, allow_null=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_goods(self, value):
        if not Goods.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('货物不存在或已停用')
        return value

    def validate_approver_owner(self, value):
        if value is None:
            return value
        if not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('审批负责人不存在或已停用')
        return value

    def validate(self, attrs):
        goods = Goods.objects.get(pk=attrs['goods'])
        if attrs['quantity'] > goods.quantity:
            raise serializers.ValidationError({'quantity': '出库数量不能超过当前库存'})
        return attrs


class ApprovalDecisionSerializer(serializers.Serializer):
    """审批决定入参"""
    decision = serializers.ChoiceField(choices=[('approved', '同意'), ('rejected', '拒绝')],
                                       required=True, error_messages={'required': '请给出审批决定'})
    remark = serializers.CharField(required=False, allow_blank=True, default='')


class WarningSerializer(serializers.ModelSerializer):
    """预警记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    
    class Meta:
        model = Warning
        fields = [
            'id', 'goods', 'goods_name', 'type', 'type_display',
            'message', 'is_read', 'created_at'
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    """审批记录序列化器"""
    approver_name = serializers.CharField(source='approver.display_name', read_only=True)
    actor_name = serializers.CharField(source='actor.display_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    authority_description = serializers.SerializerMethodField()

    class Meta:
        model = Approval
        fields = [
            'id', 'stock_out',
            'approver', 'approver_name',
            'actor', 'actor_name',
            'is_delegated', 'delegation',
            'authority_chain', 'authority_description',
            'status', 'status_display', 'remark', 'created_at', 'updated_at'
        ]

    def get_authority_description(self, obj):
        """清楚说明每次决定的实际操作者与原授权人。"""
        if not obj.actor_id:
            return None
        actor = obj.actor.display_name
        if obj.is_delegated and obj.approver_id:
            return f'实际操作者：{actor}（代理人）；原授权人：{obj.approver.display_name}'
        return f'实际操作者：{actor}（本人审批）；原授权人：{actor}'
