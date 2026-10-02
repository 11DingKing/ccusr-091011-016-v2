"""
代理授权模型
"""
from django.db import models
from django.utils import timezone
from apps.authentication.models import User


# 业务种类注册表：目前仅出库审批，后续业务扩展时在此登记
BUSINESS_TYPE_CHOICES = [
    ('stock_out', '出库审批'),
]
BUSINESS_TYPE_CODES = {code for code, _ in BUSINESS_TYPE_CHOICES}

# 风险等级（按风险从低到高排列，数值越大风险越高）
RISK_LEVEL_CHOICES = [
    ('low', '低风险'),
    ('medium', '中风险'),
    ('high', '高风险'),
    ('critical', '极高风险'),
]
RISK_LEVEL_RANK = {code: rank for rank, (code, _label) in enumerate(RISK_LEVEL_CHOICES, start=1)}

# 授权链最大段数（含根授权），防止多段代理无限延伸
MAX_CHAIN_DEPTH = 3


class Delegation(models.Model):
    """代理授权模型"""

    STATUS_CHOICES = [
        ('active', '生效中'),
        ('revoked', '已撤销'),
    ]

    grantor = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='granted_delegations', verbose_name='授权人'
    )
    agent = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='received_delegations', verbose_name='代理人'
    )
    business_types = models.JSONField('业务种类', default=list)
    max_quantity = models.DecimalField(
        '单次数量上限', max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='为空表示不限制'
    )
    max_risk_level = models.CharField(
        '风险等级上限', max_length=20, choices=RISK_LEVEL_CHOICES,
        null=True, blank=True, help_text='为空表示不限制'
    )
    valid_from = models.DateTimeField('生效时间')
    valid_to = models.DateTimeField('失效时间')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='sub_delegations', verbose_name='上级授权'
    )
    root = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='chain_delegations', verbose_name='授权链根'
    )
    allow_sub_delegation = models.BooleanField('允许转委托', default=False)
    reason = models.TextField('授权事由', blank=True)
    revoked_at = models.DateTimeField('撤销时间', null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='revoked_delegations', verbose_name='撤销人'
    )
    revoke_reason = models.TextField('撤销原因', blank=True)
    # 终止（撤销/到期）后未完成事项是否已重检，防止重复处理
    reassign_processed_at = models.DateTimeField('事项重检处理时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'dg_delegation'
        verbose_name = '代理授权'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                check=~models.Q(grantor=models.F('agent')),
                name='delegation_no_self_grant'
            ),
            models.CheckConstraint(
                check=models.Q(valid_to__gt=models.F('valid_from')),
                name='delegation_valid_time_range'
            ),
        ]

    def __str__(self):
        return f"{self.grantor} -> {self.agent}"

    # ---------- 状态判断 ----------

    def is_expired(self, at=None):
        """是否已超出失效时间"""
        at = at or timezone.now()
        return at > self.valid_to

    def is_effective(self, at=None):
        """授权本身是否未被撤销且处于有效时间内"""
        at = at or timezone.now()
        return self.status == 'active' and self.valid_from <= at <= self.valid_to

    # ---------- 授权链 ----------

    def get_chain(self):
        """返回从根授权到当前授权的完整链条（含自身）"""
        chain = []
        node = self
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    @property
    def chain_depth(self):
        """授权链段数（根授权为 1）"""
        return len(self.get_chain())

    @property
    def original_grantor(self):
        """原授权人（授权链根的授权人）"""
        return self.root.grantor if self.root_id else self.grantor

    @property
    def original_grantor_id(self):
        return self.root.grantor_id if self.root_id else self.grantor_id

    def chain_user_ids(self):
        """链条上出现过的所有用户（各段授权人与代理人）"""
        ids = set()
        for link in self.get_chain():
            ids.add(link.grantor_id)
            ids.add(link.agent_id)
        return ids

    def chain_grantor_ids(self):
        """链条上所有授权人（含根授权人）"""
        return {link.grantor_id for link in self.get_chain()}

    def chain_effective(self, at=None):
        """链条上每一段授权都有效时整链才有效"""
        at = at or timezone.now()
        return all(link.is_effective(at) for link in self.get_chain())

    # ---------- 范围校验 ----------

    def scope_covers(self, business_type, quantity=None, risk_level=None):
        """仅校验业务范围（业务种类/数量/风险等级），不校验时效"""
        if business_type not in (self.business_types or []):
            return False
        if self.max_quantity is not None and quantity is not None:
            if quantity > self.max_quantity:
                return False
        if self.max_risk_level is not None and risk_level is not None:
            if RISK_LEVEL_RANK.get(risk_level, 0) > RISK_LEVEL_RANK[self.max_risk_level]:
                return False
        return True

    def covers(self, business_type, quantity=None, risk_level=None, at=None):
        """判断授权在指定时刻是否可覆盖指定业务、数量与风险等级"""
        return self.chain_effective(at) and self.scope_covers(business_type, quantity, risk_level)
