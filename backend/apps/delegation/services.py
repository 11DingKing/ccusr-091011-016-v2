"""
代理授权业务逻辑
"""
import logging
from django.db import transaction
from django.utils import timezone
from apps.core.exceptions import BusinessException, PermissionException
from apps.warehouse.models import Approval, ApprovalEvent
from .models import (
    Delegation, BUSINESS_TYPE_CODES, RISK_LEVEL_RANK, MAX_CHAIN_DEPTH
)

logger = logging.getLogger('apps')


def create_delegation(*, grantor, agent, business_types, max_quantity=None,
                      max_risk_level=None, valid_from, valid_to,
                      allow_sub_delegation=False, reason='', parent=None):
    """
    创建代理授权。

    校验规则：
    - 禁止自己给自己授权
    - 授权人/代理人必须具备审批资质（管理员）且账号有效
    - 有效时间必须合法且失效时间晚于当前时间
    - 转委托时：只能由上级授权的代理人发起、上级必须允许转委托且有效、
      范围只能收窄不能扩大、禁止循环代理、授权链长度受限
    """
    now = timezone.now()

    if grantor.id == agent.id:
        raise BusinessException('不能给自己授权')
    if not grantor.is_active or not grantor.is_admin:
        raise PermissionException('授权人必须具备审批权限（管理员）')
    if not agent.is_active:
        raise BusinessException('代理人账号已被禁用')
    if not agent.is_admin:
        raise BusinessException('代理人必须具备审批资质（管理员）')
    if valid_to <= valid_from:
        raise BusinessException('失效时间必须晚于生效时间')
    if valid_to <= now:
        raise BusinessException('失效时间必须晚于当前时间')

    business_types = list(dict.fromkeys(business_types or []))
    if not business_types:
        raise BusinessException('请选择可代理的业务种类')
    unknown = set(business_types) - BUSINESS_TYPE_CODES
    if unknown:
        raise BusinessException(f'存在不支持的业务种类: {",".join(sorted(unknown))}')

    if max_quantity is not None and max_quantity <= 0:
        raise BusinessException('数量上限必须大于0')
    if max_risk_level is not None and max_risk_level not in RISK_LEVEL_RANK:
        raise BusinessException('无效的风险等级')

    root = None
    if parent is not None:
        if parent.agent_id != grantor.id:
            raise PermissionException('只有上级授权的代理人才能转委托')
        if not parent.allow_sub_delegation:
            raise BusinessException('上级授权不允许转委托')
        if not parent.chain_effective(now):
            raise BusinessException('上级授权已失效，无法转委托')
        # 禁止循环代理：新代理人不得出现在授权链上
        if agent.id in parent.chain_user_ids():
            raise BusinessException('禁止循环代理：代理人已存在于授权链中')
        if parent.chain_depth + 1 > MAX_CHAIN_DEPTH:
            raise BusinessException(f'授权链最长 {MAX_CHAIN_DEPTH} 段，不能再转委托')
        # 转委托范围只能收窄，不能扩大
        if not set(business_types) <= set(parent.business_types or []):
            raise BusinessException('转委托的业务种类不能超出上级授权范围')
        if valid_from < parent.valid_from or valid_to > parent.valid_to:
            raise BusinessException('转委托的有效时间不能超出上级授权范围')
        if parent.max_quantity is not None:
            if max_quantity is None or max_quantity > parent.max_quantity:
                raise BusinessException('转委托的数量上限不能超过上级授权')
        if parent.max_risk_level is not None:
            if max_risk_level is None or RISK_LEVEL_RANK[max_risk_level] > RISK_LEVEL_RANK[parent.max_risk_level]:
                raise BusinessException('转委托的风险等级上限不能超过上级授权')
        root = parent.root if parent.root_id else parent

    delegation = Delegation.objects.create(
        grantor=grantor,
        agent=agent,
        business_types=business_types,
        max_quantity=max_quantity,
        max_risk_level=max_risk_level,
        valid_from=valid_from,
        valid_to=valid_to,
        allow_sub_delegation=allow_sub_delegation,
        reason=reason,
        parent=parent,
        root=root,
    )

    logger.info(
        f"User {grantor.username} created delegation {delegation.id} "
        f"to {agent.username} (parent={parent.id if parent else None})"
    )
    return delegation


def _collect_descendants(delegation):
    """收集某授权的全部下级转委托"""
    descendants = []
    for child in delegation.sub_delegations.all():
        descendants.append(child)
        descendants.extend(_collect_descendants(child))
    return descendants


@transaction.atomic
def revoke_delegation(*, delegation, revoked_by, reason='', fallback_approver=None, at=None):
    """
    撤销代理授权：
    1. 级联撤销全部下级转委托
    2. 根授权终止时，未完成（待审批）事项重新寻找合法审批人：
       指定接替审批人则逐项改派（校验职责分离），否则退回原授权人本人
    """
    at = at or timezone.now()
    delegation = Delegation.objects.select_for_update().get(pk=delegation.pk)
    if delegation.status == 'revoked':
        raise BusinessException('该授权已被撤销，请勿重复操作')

    descendants = _collect_descendants(delegation)

    delegation.status = 'revoked'
    delegation.revoked_at = at
    delegation.revoked_by = revoked_by
    delegation.revoke_reason = reason
    delegation.save(update_fields=['status', 'revoked_at', 'revoked_by', 'revoke_reason', 'updated_at'])

    for item in descendants:
        if item.status != 'active':
            continue
        item.status = 'revoked'
        item.revoked_at = at
        item.revoked_by = revoked_by
        item.revoke_reason = '上级授权已撤销'
        item.save(update_fields=['status', 'revoked_at', 'revoked_by', 'revoke_reason', 'updated_at'])

    result = {'reassigned': [], 'reverted': []}
    # 仅根授权终止才触发事项重检；下级授权终止后上级链路仍然有效
    if delegation.parent_id is None and delegation.reassign_processed_at is None:
        result = _reassign_pending_approvals(
            delegation=delegation,
            actor=revoked_by,
            reason=reason or '代理授权已撤销',
            fallback=fallback_approver,
            at=at,
            exclude_ids={delegation.id, *[d.id for d in descendants]},
        )
        delegation.reassign_processed_at = at
        delegation.save(update_fields=['reassign_processed_at'])

    logger.info(
        f"User {revoked_by.username} revoked delegation {delegation.id}, "
        f"cascaded {len(descendants)} sub-delegations, "
        f"reassigned {len(result['reassigned'])} approvals, reverted {len(result['reverted'])}"
    )
    return result


def sweep_expired_delegations(now=None):
    """
    处理已到期的根授权：未完成事项重新寻找合法审批人（退回原授权人）。
    可由管理命令、定时任务或管理接口触发，重复执行安全（幂等）。
    """
    now = now or timezone.now()
    expired_roots = Delegation.objects.filter(
        parent__isnull=True,
        status='active',
        valid_to__lt=now,
        reassign_processed_at__isnull=True,
    )
    processed = 0
    for delegation in expired_roots:
        with transaction.atomic():
            exclude_ids = {delegation.id, *[d.id for d in _collect_descendants(delegation)]}
            _reassign_pending_approvals(
                delegation=delegation,
                actor=None,
                reason='代理授权已到期',
                fallback=None,
                at=now,
                exclude_ids=exclude_ids,
            )
            delegation.reassign_processed_at = now
            delegation.save(update_fields=['reassign_processed_at'])
        processed += 1
        logger.info(f"Delegation {delegation.id} expired, pending approvals re-examined")
    return processed


def _reassign_pending_approvals(*, delegation, actor, reason, fallback, at, exclude_ids):
    """
    授权终止后重检该授权人名下的待审批事项：
    - 该授权覆盖不到的事项、或仍有其他有效授权可处理的事项，不受影响
    - 指定接替审批人且不与出库操作人冲突时改派，否则退回原授权人本人
    """
    grantor = delegation.grantor
    result = {'reassigned': [], 'reverted': []}
    pending = Approval.objects.filter(
        approver=grantor, status='pending'
    ).select_related('stock_out')

    for approval in pending:
        stock_out = approval.stock_out
        if not delegation.scope_covers('stock_out', stock_out.quantity, stock_out.risk_level):
            continue
        if _has_other_effective_delegation(approval, exclude_ids, at):
            continue

        if fallback is not None and fallback.id != stock_out.operator_id:
            previous = approval.approver
            approval.approver = fallback
            approval.save(update_fields=['approver', 'updated_at'])
            ApprovalEvent.objects.create(
                approval=approval, event='reassigned', actor=actor,
                detail={
                    'delegation_id': delegation.id,
                    'from_approver_id': previous.id if previous else None,
                    'from_approver': previous.username if previous else None,
                    'to_approver_id': fallback.id,
                    'to_approver': fallback.username,
                    'reason': reason,
                }
            )
            result['reassigned'].append(approval.id)
        else:
            detail = {
                'delegation_id': delegation.id,
                'approver_id': grantor.id,
                'approver': grantor.username,
                'reason': f'{reason}，事项退回原审批人',
            }
            if fallback is not None and fallback.id == stock_out.operator_id:
                detail['fallback_rejected'] = '接替审批人与出库操作人为同一人，违反职责分离'
            ApprovalEvent.objects.create(
                approval=approval, event='reverted', actor=actor, detail=detail
            )
            result['reverted'].append(approval.id)
    return result


def _has_other_effective_delegation(approval, exclude_ids, at):
    """判断该审批事项是否仍存在其他有效代理授权可以处理"""
    stock_out = approval.stock_out
    candidates = Delegation.objects.filter(
        status='active', valid_from__lte=at, valid_to__gte=at,
    ).exclude(pk__in=exclude_ids).select_related('root')
    for candidate in candidates:
        if candidate.original_grantor_id != approval.approver_id:
            continue
        if candidate.covers('stock_out', stock_out.quantity, stock_out.risk_level, at):
            return True
    return False


def find_signing_delegation(*, user, approval, at=None):
    """
    查找 user 可用于签署该审批的有效代理授权。
    授权链必须根植于该审批的审批人（原授权人），且覆盖业务种类、数量与风险等级。
    """
    at = at or timezone.now()
    stock_out = approval.stock_out
    candidates = Delegation.objects.filter(
        agent=user,
        status='active',
        valid_from__lte=at,
        valid_to__gte=at,
    ).select_related('root', 'parent')
    for delegation in candidates:
        if delegation.original_grantor_id != approval.approver_id:
            continue
        if delegation.covers('stock_out', stock_out.quantity, stock_out.risk_level, at):
            return delegation
    return None


def assert_signing_sod(*, approval, signer, delegation=None):
    """
    职责分离检查：出库操作人不得参与该出库单的审批决定，
    无论是本人直接审批，还是作为授权链上的任一授权人/代理人。
    """
    operator_id = approval.stock_out.operator_id
    if operator_id is None:
        return
    involved = {signer.id}
    if delegation is not None:
        involved |= delegation.chain_grantor_ids()
    if operator_id in involved:
        raise PermissionException('职责分离：出库操作人不得参与该单的审批决定')
