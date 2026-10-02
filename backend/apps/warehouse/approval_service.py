"""
审批授权服务：统一处理"谁有权审批这张单据"。

核心规则：
1. 职责分离：出库申请人(operator)不能审批自己提交的单据，
   本人审批与代理审批一视同仁；
2. 代理审批必须持有覆盖该业务种类、金额、风险等级且在有效期内的授权；
3. 授权实时解析——授权被撤销或到期后，未决单据立即失去代理依据，
   必须重新寻找合法审批人（负责人本人或其新的有效代理人）；
4. 代理人不得转授（见 DelegationPolicy 的保存校验），因此授权链
   至多一跳，无法借多段代理绕过职责分离。
"""
from django.db import transaction
from django.utils import timezone

from apps.authentication.models import User
from apps.delegation.models import (
    BUSINESS_STOCK_OUT_APPROVAL,
    DelegationPolicy,
    ApprovalAct,
)


class ApprovalError(Exception):
    """审批不合法时抛出，消息可直接返回前端。"""


def eligible_owner_ids(stock_out):
    """
    凭借自身角色即具备该单据审批资格的用户ID集合，
    并已排除出库申请人（职责分离）。
    """
    qs = User.objects.filter(
        role__in=["superadmin", "admin"], is_active=True
    )
    if stock_out.operator_id:
        qs = qs.exclude(id=stock_out.operator_id)
    return set(qs.values_list("id", flat=True))


def _self_eligible(user, stock_out):
    return (
        user
        and user.is_active
        and user.is_admin
        and user.id != stock_out.operator_id
    )


def find_current_handler(stock_out, at=None):
    """
    重新寻找单据当前的合法处理人。

    返回 dict：
      {"kind": "self", "user": owner, "owner": owner, "policy": None}
      {"kind": "delegated", "user": agent, "owner": principal, "policy": policy}
      {"kind": None, ...}  # 无人可处理，需上报/重新指派
    """
    at = at or timezone.now()
    owner = stock_out.approver_owner
    amount = stock_out.amount
    risk = stock_out.risk_level or "normal"
    eligible = eligible_owner_ids(stock_out)

    def _valid(policy):
        return policy.is_effective(at) and policy.covers(
            BUSINESS_STOCK_OUT_APPROVAL, amount, risk)

    def _as_handler(policy):
        # 代理人不能是申请人本人，代理人须在职
        if policy.agent_id == stock_out.operator_id or not policy.agent.is_active:
            return None
        return {"kind": "delegated", "user": policy.agent,
                "owner": policy.principal, "policy": policy}

    # 1. 已登记负责人授予的、覆盖本单据的有效代理优先（出差代办场景）
    if owner and owner.id in eligible:
        owner_policy = DelegationPolicy.objects.select_related("principal", "agent").filter(
            business_type=BUSINESS_STOCK_OUT_APPROVAL,
            status="active",
            principal=owner,
            end_time__gt=at,
            agent__is_active=True,
        ).order_by("created_at")
        for policy in owner_policy:
            if not _valid(policy):
                continue
            handler = _as_handler(policy)
            if handler:
                return handler

        # 2. 负责人本人可处理
        if _self_eligible(owner, stock_out):
            return {"kind": "self", "user": owner, "owner": owner, "policy": None}

    # 3. 无指定负责人（或其已不具资格）：在其他合格负责人的授权代理中寻找
    policies = DelegationPolicy.objects.select_related("principal", "agent").filter(
        business_type=BUSINESS_STOCK_OUT_APPROVAL,
        status="active",
        end_time__gt=at,
        principal_id__in=eligible,
        agent__is_active=True,
    ).order_by("created_at")
    for policy in policies:
        handler = _as_handler(policy)
        if handler and _valid(policy):
            return handler

    return {"kind": None, "user": None, "owner": owner, "policy": None}


def validate_actor(stock_out, actor, at=None):
    """
    校验操作者对该单据的审批权，返回 (审批权持有人, 授权策略或None)。
    任何不合法情形抛 ApprovalError。
    """
    at = at or timezone.now()

    if not actor or not actor.is_active:
        raise ApprovalError("操作者不存在或已被停用")

    if stock_out.operator_id and actor.id == stock_out.operator_id:
        raise ApprovalError("申请人不能审批自己提交的出库单（职责分离）")

    # 本人行使自身审批权
    if _self_eligible(actor, stock_out):
        return actor, None

    # 代理审批：实时解析有效授权（撤销/到期即刻失效）
    eligible = eligible_owner_ids(stock_out)
    policies = DelegationPolicy.objects.select_related("principal", "agent").filter(
        agent=actor,
        business_type=BUSINESS_STOCK_OUT_APPROVAL,
        principal_id__in=eligible,
    )
    active = [p for p in policies if p.is_effective(at)]
    if not active:
        raise ApprovalError("当前没有覆盖该业务的有效代理授权，授权可能已撤销或到期")

    covering = [
        p for p in active
        if p.covers(BUSINESS_STOCK_OUT_APPROVAL, stock_out.amount,
                    stock_out.risk_level or "normal")
    ]
    if not covering:
        raise ApprovalError("该单据超出代理授权的金额或风险范围")

    policy = covering[0]
    return policy.principal, policy


def _personal_chain(user, stock_out):
    return {
        "policy_id": None,
        "business_type": BUSINESS_STOCK_OUT_APPROVAL,
        "length": 0,
        "kind": "personal",
        "chain": [{"role": "self", "id": user.id, "name": user.display_name}],
        "subject_id": stock_out.id,
    }


@transaction.atomic
def perform_approval(stock_out, actor, decision, remark="", at=None):
    """
    执行一次审批并固化授权链。decision: 'approved' / 'rejected'。
    返回创建的 Approval。
    """
    from .models import Approval, StockOut

    locked = StockOut.objects.select_for_update().get(pk=stock_out.pk)
    if locked.status != "pending":
        raise ApprovalError("该出库单已审批，不能重复操作")

    owner, policy = validate_actor(locked, actor, at)

    chain = policy.chain_snapshot() if policy else _personal_chain(actor, locked)
    if policy:
        chain["subject_id"] = locked.id

    approval = Approval.objects.create(
        stock_out=locked,
        approver=owner,          # 原授权人 / 审批权持有人
        actor=actor,             # 实际操作者
        delegation=policy,
        authority_chain=chain,
        is_delegated=policy is not None,
        status=decision,
        remark=remark,
    )

    # 通用授权链台账（跨业务种类可查）
    ApprovalAct.objects.create(
        business_type=BUSINESS_STOCK_OUT_APPROVAL,
        subject_id=locked.id,
        operator=actor,
        original_grantor=owner,
        delegation=policy,
        decision=decision,
        amount=locked.amount,
        risk_level=locked.risk_level or "",
        authority_chain=chain,
        remark=remark,
    )

    locked.status = "approved" if decision == "approved" else "rejected"
    locked.save(update_fields=["status", "updated_at"] if hasattr(locked, "updated_at")
                else ["status"])
    return approval
