"""
代理授权模型：审批负责人可在出差等期间将审批权有限授予代理人。

设计要点：
- 授权限定代理人、业务种类、金额/风险范围与有效时间；
- 只有"以自身身份持有审批权"的人才能授权；正凭他人授权行事的
  代理人不得再作为授权人转授，从模型层杜绝多段代理（A→B→C）、
  循环代理（A→B、B→A）以及借多段代理绕过职责分离；
- 每次代理决定都固化完整授权链（实际操作者 + 原授权人）；
- 授权可随时撤销；到期/撤销后未决事项的授权依据自动失效，
  需重新寻找合法审批人。
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.authentication.models import User


#: 可代理的业务种类：当前仅出库审批，预留其它审批业务
BUSINESS_STOCK_OUT_APPROVAL = "stock_out_approval"
BUSINESS_CHOICES = [
    (BUSINESS_STOCK_OUT_APPROVAL, "出库审批"),
]

#: 授权状态
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_CHOICES = [
    (STATUS_ACTIVE, "生效中"),
    (STATUS_EXPIRED, "已到期"),
    (STATUS_REVOKED, "已撤销"),
]

#: 风险等级全序：数值越大风险越高
RISK_LEVEL_ORDER = {
    "normal": 1,      # 普通
    "important": 2,   # 重要
    "critical": 3,    # 重大
}
RISK_LEVEL_CHOICES = [
    ("normal", "普通"),
    ("important", "重要"),
    ("critical", "重大"),
]


def iso(value):
    return value.isoformat() if value else None


class DelegationPolicy(models.Model):
    """代理授权策略：授权人(principal) -> 代理人(agent)的有限授权。"""

    principal = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name="granted_delegations", verbose_name="原授权人",
    )
    agent = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name="received_delegations", verbose_name="代理人",
    )
    business_type = models.CharField(
        "可处理业务种类", max_length=40, choices=BUSINESS_CHOICES,
        default=BUSINESS_STOCK_OUT_APPROVAL,
    )
    # None 表示不限金额
    max_amount = models.DecimalField(
        "单笔金额上限", max_digits=14, decimal_places=2, null=True, blank=True,
        help_text="为空表示不限金额",
    )
    # 普通/重要/重大 -> 允许处理的最高风险等级；空串表示不限
    max_risk_level = models.CharField(
        "可处理最高风险等级", max_length=20, blank=True, default="",
        choices=RISK_LEVEL_CHOICES, help_text="为空表示不限风险等级",
    )
    start_time = models.DateTimeField("生效时间", null=True, blank=True)
    end_time = models.DateTimeField("失效时间")
    status = models.CharField(
        "状态", max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE,
    )
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="revoked_delegations", verbose_name="撤销人",
    )
    reason = models.CharField("授权事由", max_length=200, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "dlg_policy"
        verbose_name = "代理授权策略"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.principal_id} -> {self.agent_id} {self.get_business_type_display()}"

    # ---------- 校验 ----------

    def clean(self):
        errors = {}

        # 禁止自己给自己授权
        if self.principal_id and self.agent_id and self.principal_id == self.agent_id:
            errors["agent"] = "不允许自己给自己授权"

        if self.end_time:
            # 仅当显式指定生效时间时要求严格晚于生效时间；
            # end_time 已过的授权按"创建即过期"处理（见 save），
            # 定时任务也可正常把到期授权写回。
            if self.start_time and self.end_time <= self.start_time:
                errors["end_time"] = "失效时间必须晚于生效时间"

        if self.max_amount is not None and self.max_amount < 0:
            errors["max_amount"] = "金额上限不能为负数"

        if self.max_risk_level and self.max_risk_level not in RISK_LEVEL_ORDER:
            errors["max_risk_level"] = "非法的风险等级"

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.status == STATUS_ACTIVE:
            # 禁止多段代理 / 循环代理（双向时间窗检查，覆盖任意建立顺序）：
            # - 授权人在窗口内不得正凭他人授权行事（不得转授）；
            # - 代理人在窗口内不得已向第三人转授。
            self._assert_no_chained_delegation()
            if self.end_time and timezone.now() >= self.end_time:
                self.status = STATUS_EXPIRED
        super().save(*args, **kwargs)

    def _assert_no_chained_delegation(self):
        self._assert_principal_not_acting_as_agent()
        self._assert_agent_not_redelegating()

    def _window_overlaps(self, other_start, other_end):
        new_start = self.start_time or timezone.now()
        return other_end > new_start and (other_start is None or other_start < self.end_time)

    def _assert_agent_not_redelegating(self):
        """代理人若在新授权窗口内已把权力授予第三人，则禁止。"""
        if not self.agent_id or not self.end_time:
            return
        outgoing = DelegationPolicy.objects.filter(
            principal_id=self.agent_id,
            business_type=self.business_type,
            status=STATUS_ACTIVE,
        ).exclude(agent_id=self.agent_id)
        if self.pk:
            outgoing = outgoing.exclude(pk=self.pk)
        for other in outgoing.only("start_time", "end_time"):
            if self._window_overlaps(other.start_time, other.end_time):
                raise ValidationError({
                    "agent": "代理人在授权有效期内已向他人转授审批权，禁止形成多段代理",
                })

    def _assert_principal_not_acting_as_agent(self):
        """
        禁止多段/循环代理：若授权人在新授权的有效时间窗内，正（或将）
        凭他人的授权作为代理人行事，则不得向外转授。比较两个授权
        时间窗是否重叠，使"未来生效"的授权也无法打时间差。
        """
        if not self.principal_id or not self.end_time:
            return
        new_start = self.start_time or timezone.now()
        new_end = self.end_time

        incoming = DelegationPolicy.objects.filter(
            agent_id=self.principal_id,
            business_type=self.business_type,
            status=STATUS_ACTIVE,
        ).exclude(principal_id=self.principal_id)
        if self.pk:
            incoming = incoming.exclude(pk=self.pk)

        for other in incoming.only("start_time", "end_time"):
            other_start = other.start_time
            # 窗口 [start, end) 重叠判定
            if other.end_time > new_start and (other_start is None or other_start < new_end):
                raise ValidationError({
                    "principal": "该用户在授权有效期内正（或将）凭他人授权行事，授权不得转授（禁止多段/循环代理）",
                })

    # ---------- 状态与范围 ----------

    def is_effective(self, at=None):
        """当前/指定时刻是否仍可作为审批权的合法来源。"""
        at = at or timezone.now()
        if self.status != STATUS_ACTIVE:
            return False
        if self.start_time and at < self.start_time:
            return False
        if at >= self.end_time:
            return False
        return True

    def covers(self, business_type, amount, risk_level):
        """是否覆盖指定业务、金额与风险等级。"""
        if self.business_type != business_type:
            return False
        if amount is not None and self.max_amount is not None \
                and amount > self.max_amount:
            return False
        if risk_level and self.max_risk_level and \
                RISK_LEVEL_ORDER[risk_level] > RISK_LEVEL_ORDER[self.max_risk_level]:
            return False
        return True

    def refresh_status(self, at=None, save=True):
        """到期自动置为 expired（不覆盖已撤销）。"""
        at = at or timezone.now()
        if self.status == STATUS_ACTIVE and at >= self.end_time:
            self.status = STATUS_EXPIRED
            if save:
                super().save(update_fields=["status", "updated_at"])
        return self

    def revoke(self, revoked_by=None, at=None):
        """撤销授权；已终结状态不再改变。"""
        if self.status != STATUS_ACTIVE:
            return False
        self.status = STATUS_REVOKED
        self.revoked_at = at or timezone.now()
        self.revoked_by = revoked_by
        super().save(update_fields=["status", "revoked_at", "revoked_by", "updated_at"])
        return True

    # ---------- 授权链 ----------

    def chain_snapshot(self):
        """
        返回授权链快照。代理不得转授，故合法链恒为一跳：
        [原授权人(principal)] -> [实际操作者(agent)]。
        """
        return {
            "policy_id": self.id,
            "business_type": self.business_type,
            "length": 1,
            "chain": [
                {"role": "principal", "id": self.principal_id,
                 "name": self.principal.display_name},
                {"role": "agent", "id": self.agent_id,
                 "name": self.agent.display_name},
            ],
            "scope": {
                "max_amount": str(self.max_amount) if self.max_amount is not None else None,
                "max_risk_level": self.max_risk_level or None,
            },
            "start_time": iso(self.start_time),
            "end_time": iso(self.end_time),
            "status": self.status,
        }


class ApprovalAct(models.Model):
    """
    一次审批行为及其授权链凭证。

    - operator：登录系统、实际点击审批的人（实际操作者）；
    - original_grantor：该审批权的原始持有人（本人审批时等于 operator；
      代理审批时为授权人）；
    - delegation：代理审批所依据的授权策略，null 表示本人行使自身职权；
    - authority_chain：授权链 JSON 快照，策略事后被撤销/删除也不影响凭证。
    """

    DECISION_CHOICES = [
        ("approved", "同意"),
        ("rejected", "拒绝"),
    ]

    # 与具体业务单据解耦：用业务种类 + 单据标识关联
    business_type = models.CharField("业务种类", max_length=40, choices=BUSINESS_CHOICES)
    subject_id = models.BigIntegerField("业务单据ID")

    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name="approval_acts", verbose_name="实际操作者",
    )
    original_grantor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name="authority_for_acts", verbose_name="原授权人",
    )
    delegation = models.ForeignKey(
        DelegationPolicy, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approval_acts", verbose_name="依据的代理授权",
    )
    decision = models.CharField("决定", max_length=20, choices=DECISION_CHOICES)
    amount = models.DecimalField(
        "涉及金额", max_digits=14, decimal_places=2, null=True, blank=True)
    risk_level = models.CharField("风险等级", max_length=20, blank=True, default="")
    authority_chain = models.JSONField("授权链快照", default=dict, blank=True)
    remark = models.TextField("审批意见", blank=True)
    acted_at = models.DateTimeField("操作时间", auto_now_add=True)

    class Meta:
        db_table = "dlg_approval_act"
        verbose_name = "审批行为凭证"
        verbose_name_plural = verbose_name
        ordering = ["-acted_at"]
        indexes = [
            models.Index(fields=["business_type", "subject_id"]),
        ]

    def __str__(self):
        via = f" 代 {self.original_grantor_id}" if self.delegation_id else ""
        return f"{self.get_decision_display()} by {self.operator_id}{via}"

    @property
    def is_delegated(self):
        return self.delegation_id is not None

    def authority_summary(self):
        """给查询结果用的人类可读说明。"""
        op = self.operator.display_name if self.operator else f"用户#{self.operator_id}"
        if self.is_delegated:
            grantor = (self.original_grantor.display_name
                       if self.original_grantor else "-")
            return f"实际操作者：{op}（代理人）；原授权人：{grantor}"
        return f"实际操作者：{op}（本人行使职权）；原授权人：{op}"


def effective_delegations_for(agent, business_type, at=None):
    """某代理人当前可凭以行事的有效授权（同业务种类）。"""
    at = at or timezone.now()
    policies = DelegationPolicy.objects.select_related("principal", "agent").filter(
        agent=agent,
        business_type=business_type,
        status=STATUS_ACTIVE,
        end_time__gt=at,
    ).filter(
        models.Q(start_time__isnull=True) | models.Q(start_time__lte=at)
    )
    return [p for p in policies if p.is_effective(at)]


def resolve_delegation(agent, business_type, amount, risk_level,
                       principal_ids=None, at=None):
    """
    在代理人当前的有效授权中，找出覆盖该笔业务/金额/风险、
    且原授权人属于 principal_ids（具备自身审批资格者）的一条。

    找不到返回 None。授权链只解析一跳——代理人不得转授，
    因此不存在也不允许沿多段授权继续追溯。
    """
    for policy in effective_delegations_for(agent, business_type, at):
        if principal_ids is not None and policy.principal_id not in principal_ids:
            continue
        if policy.covers(business_type, amount, risk_level):
            return policy
    return None


def expire_due_policies(at=None):
    """把所有已到失效时间的授权置为过期，返回受影响条数（供定时任务调用）。"""
    at = at or timezone.now()
    count = 0
    for policy in DelegationPolicy.objects.filter(status=STATUS_ACTIVE, end_time__lte=at):
        policy.status = STATUS_EXPIRED
        policy.save(update_fields=["status", "updated_at"])
        count += 1
    return count
