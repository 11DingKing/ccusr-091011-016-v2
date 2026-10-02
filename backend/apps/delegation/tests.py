"""
代理授权测试用例
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.core.exceptions import BusinessException, PermissionException
from apps.warehouse.models import (
    Approval, ApprovalEvent, Category, Goods, StockOut, Unit, Variety
)
from .models import Delegation
from .services import create_delegation, revoke_delegation, sweep_expired_delegations


class DelegationFixture(TestCase):
    """代理授权测试基类：准备用户、货物与常用辅助方法"""

    def setUp(self):
        self.leader = User.objects.create_user('leader', 'pass12345', role='admin')
        self.deputy = User.objects.create_user('deputy', 'pass12345', role='admin')
        self.third = User.objects.create_user('third', 'pass12345', role='admin')
        self.fourth = User.objects.create_user('fourth', 'pass12345', role='admin')
        self.operator = User.objects.create_user('operator', 'pass12345', role='user')
        self.client = APIClient()
        self.now = timezone.now()

        self.unit = Unit.objects.create(name='件', created_by=self.leader)
        self.category = Category.objects.create(name='受控器材', unit=self.unit, created_by=self.leader)
        self.variety = Variety.objects.create(name='记录终端', category=self.category, created_by=self.leader)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name='执法记录终端',
            code='DEV-001',
            quantity=Decimal('100'),
            warning_threshold=Decimal('5'),
        )

    def login(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(user)}')

    def make_stock_out(self, approver=None, quantity=Decimal('5'), risk_level='low', operator=None):
        """直接通过ORM准备出库单与待审批记录"""
        operator = operator or self.operator
        approver = approver or self.leader
        stock_out = StockOut.objects.create(
            goods=self.goods,
            operator=operator,
            receiver='领用人',
            quantity=quantity,
            risk_level=risk_level,
        )
        approval = Approval.objects.create(stock_out=stock_out, approver=approver)
        return stock_out, approval

    def make_delegation(self, grantor=None, agent=None, **kwargs):
        grantor = grantor or self.leader
        agent = agent or self.deputy
        defaults = dict(
            business_types=['stock_out'],
            valid_from=self.now - timedelta(hours=1),
            valid_to=self.now + timedelta(days=7),
        )
        defaults.update(kwargs)
        return create_delegation(grantor=grantor, agent=agent, **defaults)

    def decide(self, user, approval, action='approved'):
        self.login(user)
        return self.client.post(
            f'/api/approvals/{approval.id}/decide/',
            {'action': action},
            format='json'
        )


class DelegationCreateTest(DelegationFixture):
    """代理授权创建校验"""

    def post_delegation(self, user, **overrides):
        self.login(user)
        payload = {
            'agent': self.deputy.id,
            'business_types': ['stock_out'],
            'valid_from': (self.now - timedelta(hours=1)).isoformat(),
            'valid_to': (self.now + timedelta(days=3)).isoformat(),
            'reason': '出差代办',
        }
        payload.update(overrides)
        return self.client.post('/api/delegations/', payload, format='json')

    def test_create_delegation_via_api(self):
        response = self.post_delegation(
            self.leader, max_quantity='20', max_risk_level='medium'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['grantor'], self.leader.id)
        self.assertEqual(data['agent'], self.deputy.id)
        self.assertEqual(data['business_types'], ['stock_out'])
        self.assertEqual(data['max_risk_level'], 'medium')
        self.assertEqual(data['display_status'], '生效中')
        self.assertTrue(data['is_effective'])

    def test_self_delegation_rejected(self):
        response = self.post_delegation(self.leader, agent=self.leader.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('不能给自己授权', response.json()['message'])

    def test_non_admin_grantor_rejected(self):
        response = self.post_delegation(self.operator)
        self.assertEqual(response.status_code, 403)

    def test_non_admin_agent_rejected(self):
        response = self.post_delegation(self.leader, agent=self.operator.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('审批资质', response.json()['message'])

    def test_invalid_time_range_rejected(self):
        response = self.post_delegation(
            self.leader,
            valid_from=(self.now + timedelta(days=3)).isoformat(),
            valid_to=(self.now + timedelta(days=1)).isoformat(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('失效时间必须晚于生效时间', response.json()['message'])

    def test_past_valid_to_rejected(self):
        response = self.post_delegation(
            self.leader,
            valid_from=(self.now - timedelta(days=3)).isoformat(),
            valid_to=(self.now - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('失效时间必须晚于当前时间', response.json()['message'])

    def test_unknown_business_type_rejected(self):
        response = self.post_delegation(self.leader, business_types=['stock_in'])
        self.assertEqual(response.status_code, 400)
        self.assertIn('不支持的业务种类', response.json()['message'])

    def test_empty_business_types_rejected(self):
        response = self.post_delegation(self.leader, business_types=[])
        self.assertEqual(response.status_code, 400)


class SubDelegationTest(DelegationFixture):
    """转委托（多段代理）校验"""

    def test_sub_delegation_success(self):
        parent = self.make_delegation(
            allow_sub_delegation=True,
            max_quantity=Decimal('50'),
            max_risk_level='high',
        )
        child = create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            max_quantity=Decimal('20'),
            max_risk_level='medium',
            valid_from=self.now - timedelta(minutes=30),
            valid_to=self.now + timedelta(days=3),
            parent=parent,
        )
        self.assertEqual(child.root_id, parent.id)
        self.assertEqual(child.original_grantor_id, self.leader.id)
        self.assertEqual(child.chain_depth, 2)

    def test_sub_delegation_requires_parent_permission(self):
        parent = self.make_delegation(allow_sub_delegation=False)
        with self.assertRaisesMessage(BusinessException, '不允许转委托'):
            create_delegation(
                grantor=self.deputy,
                agent=self.third,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=parent,
            )

    def test_sub_delegation_only_by_parent_agent(self):
        parent = self.make_delegation(allow_sub_delegation=True)
        with self.assertRaisesMessage(PermissionException, '代理人才能转委托'):
            create_delegation(
                grantor=self.third,
                agent=self.fourth,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=parent,
            )

    def test_sub_delegation_quantity_cannot_widen(self):
        parent = self.make_delegation(
            allow_sub_delegation=True, max_quantity=Decimal('50')
        )
        with self.assertRaisesMessage(BusinessException, '数量上限'):
            create_delegation(
                grantor=self.deputy,
                agent=self.third,
                business_types=['stock_out'],
                max_quantity=Decimal('80'),
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=parent,
            )

    def test_sub_delegation_risk_cannot_widen(self):
        parent = self.make_delegation(
            allow_sub_delegation=True, max_risk_level='medium'
        )
        with self.assertRaisesMessage(BusinessException, '风险等级'):
            create_delegation(
                grantor=self.deputy,
                agent=self.third,
                business_types=['stock_out'],
                max_risk_level='high',
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=parent,
            )

    def test_sub_delegation_time_cannot_widen(self):
        parent = self.make_delegation(allow_sub_delegation=True)
        with self.assertRaisesMessage(BusinessException, '有效时间'):
            create_delegation(
                grantor=self.deputy,
                agent=self.third,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=30),
                parent=parent,
            )

    def test_circular_delegation_rejected(self):
        """A→B 后禁止 B→A"""
        parent = self.make_delegation(allow_sub_delegation=True)
        with self.assertRaisesMessage(BusinessException, '循环代理'):
            create_delegation(
                grantor=self.deputy,
                agent=self.leader,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=parent,
            )

    def test_multi_hop_circular_delegation_rejected(self):
        """A→B→C 后禁止 C→A、C→B"""
        first = self.make_delegation(allow_sub_delegation=True)
        second = create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
            allow_sub_delegation=True,
        )
        with self.assertRaisesMessage(BusinessException, '循环代理'):
            create_delegation(
                grantor=self.third,
                agent=self.leader,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=second,
            )
        with self.assertRaisesMessage(BusinessException, '循环代理'):
            create_delegation(
                grantor=self.third,
                agent=self.deputy,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(days=1),
                parent=second,
            )

    def test_chain_depth_limit(self):
        """授权链最长 3 段，第 4 段被拒绝"""
        first = self.make_delegation(allow_sub_delegation=True)
        second = create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
            allow_sub_delegation=True,
        )
        third_link = create_delegation(
            grantor=self.third,
            agent=self.fourth,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=1),
            parent=second,
            allow_sub_delegation=True,
        )
        self.assertEqual(third_link.chain_depth, 3)
        extra_user = User.objects.create_user('fifth', 'pass12345', role='admin')
        with self.assertRaisesMessage(BusinessException, '最长'):
            create_delegation(
                grantor=self.fourth,
                agent=extra_user,
                business_types=['stock_out'],
                valid_from=self.now,
                valid_to=self.now + timedelta(hours=12),
                parent=third_link,
            )


class DelegatedSigningTest(DelegationFixture):
    """代理签署与授权链记录"""

    def test_direct_approval(self):
        _, approval = self.make_stock_out()
        response = self.decide(self.leader, approval)
        self.assertEqual(response.status_code, 200)

        approval.refresh_from_db()
        self.assertEqual(approval.status, 'approved')
        self.assertEqual(approval.actual_operator_id, self.leader.id)
        self.assertFalse(approval.is_delegated)
        self.assertIsNone(approval.delegation_id)
        self.assertIsNotNone(approval.decided_at)
        self.assertEqual(approval.stock_out.status, 'approved')

    def test_delegated_signing_records_chain(self):
        delegation = self.make_delegation()
        _, approval = self.make_stock_out()

        response = self.decide(self.deputy, approval)
        self.assertEqual(response.status_code, 200)

        approval.refresh_from_db()
        # 原授权人保持不变，实际操作人是代理人
        self.assertEqual(approval.approver_id, self.leader.id)
        self.assertEqual(approval.actual_operator_id, self.deputy.id)
        self.assertTrue(approval.is_delegated)
        self.assertEqual(approval.delegation_id, delegation.id)

        # 决定事件记录了授权链快照
        event = approval.events.get(event='decided')
        self.assertEqual(event.detail['actual_operator'], 'deputy')
        self.assertEqual(event.detail['original_grantor'], 'leader')
        chain = event.detail['delegation_chain']
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]['grantor'], 'leader')
        self.assertEqual(chain[0]['agent'], 'deputy')

    def test_multi_hop_chain_recorded(self):
        """多段代理签署时记录完整授权链"""
        first = self.make_delegation(allow_sub_delegation=True)
        create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
        )
        _, approval = self.make_stock_out()

        response = self.decide(self.third, approval)
        self.assertEqual(response.status_code, 200)

        approval.refresh_from_db()
        self.assertEqual(approval.approver_id, self.leader.id)
        self.assertEqual(approval.actual_operator_id, self.third.id)

        data = response.json()['data']
        chain = data['authorization_chain']
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]['grantor_name'], 'leader')
        self.assertEqual(chain[0]['agent_name'], 'deputy')
        self.assertEqual(chain[1]['grantor_name'], 'deputy')
        self.assertEqual(chain[1]['agent_name'], 'third')

    def test_outsider_cannot_decide(self):
        _, approval = self.make_stock_out()
        response = self.decide(self.third, approval)
        self.assertEqual(response.status_code, 403)

    def test_quantity_scope_enforced(self):
        self.make_delegation(max_quantity=Decimal('10'))
        _, big = self.make_stock_out(quantity=Decimal('50'))
        _, small = self.make_stock_out(quantity=Decimal('5'))

        self.assertEqual(self.decide(self.deputy, big).status_code, 403)
        self.assertEqual(self.decide(self.deputy, small).status_code, 200)

    def test_risk_scope_enforced(self):
        self.make_delegation(max_risk_level='medium')
        _, risky = self.make_stock_out(risk_level='high')
        _, normal = self.make_stock_out(risk_level='low')

        self.assertEqual(self.decide(self.deputy, risky).status_code, 403)
        self.assertEqual(self.decide(self.deputy, normal).status_code, 200)

    def test_expired_delegation_cannot_sign(self):
        delegation = self.make_delegation()
        _, approval = self.make_stock_out()
        # 强制到期
        Delegation.objects.filter(pk=delegation.pk).update(
            valid_to=timezone.now() - timedelta(seconds=1)
        )
        response = self.decide(self.deputy, approval)
        self.assertEqual(response.status_code, 403)

    def test_not_yet_effective_delegation_cannot_sign(self):
        self.make_delegation(
            valid_from=self.now + timedelta(days=1),
            valid_to=self.now + timedelta(days=7),
        )
        _, approval = self.make_stock_out()
        response = self.decide(self.deputy, approval)
        self.assertEqual(response.status_code, 403)

    def test_revoked_delegation_cannot_sign(self):
        delegation = self.make_delegation()
        _, approval = self.make_stock_out()
        revoke_delegation(delegation=delegation, revoked_by=self.leader)
        response = self.decide(self.deputy, approval)
        self.assertEqual(response.status_code, 403)

    def test_sod_operator_cannot_sign_via_delegation(self):
        """出库操作人即使持有代理授权，也不得审批自己的出库单"""
        self.make_delegation()
        _, approval = self.make_stock_out(operator=self.deputy)
        response = self.decide(self.deputy, approval)
        self.assertEqual(response.status_code, 403)
        self.assertIn('职责分离', response.json()['message'])

    def test_sod_chain_grantor_is_operator(self):
        """授权链中间环节的授权人是出库操作人时，末端代理人也不得签署"""
        first = self.make_delegation(allow_sub_delegation=True)
        create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
        )
        _, approval = self.make_stock_out(operator=self.deputy)
        response = self.decide(self.third, approval)
        self.assertEqual(response.status_code, 403)
        self.assertIn('职责分离', response.json()['message'])

    def test_approval_list_shows_operator_and_grantor(self):
        """查询结果同时展示实际操作者与原授权人"""
        self.make_delegation()
        _, approval = self.make_stock_out()
        self.decide(self.deputy, approval)

        self.login(self.leader)
        response = self.client.get('/api/approvals/')
        self.assertEqual(response.status_code, 200)
        items = response.json()['data']['list']
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['approver_name'], 'leader')
        self.assertEqual(item['actual_operator_name'], 'deputy')
        self.assertTrue(item['is_delegated'])
        self.assertEqual(len(item['authorization_chain']), 1)

    def test_approval_list_filter_is_delegated(self):
        self.make_delegation()
        _, delegated_approval = self.make_stock_out()
        _, direct_approval = self.make_stock_out()
        self.decide(self.deputy, delegated_approval)
        self.decide(self.leader, direct_approval)

        self.login(self.leader)
        response = self.client.get('/api/approvals/?is_delegated=true')
        items = response.json()['data']['list']
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['is_delegated'])

    def test_decide_rejects_non_pending(self):
        _, approval = self.make_stock_out()
        self.assertEqual(self.decide(self.leader, approval).status_code, 200)
        self.assertEqual(self.decide(self.leader, approval).status_code, 400)


class RevokeDelegationTest(DelegationFixture):
    """撤销授权后的级联与事项重检"""

    def test_revoke_reassigns_pending_to_fallback(self):
        delegation = self.make_delegation()
        _, approval_one = self.make_stock_out()
        _, approval_two = self.make_stock_out()

        self.login(self.leader)
        response = self.client.post(
            f'/api/delegations/{delegation.id}/revoke/',
            {'fallback_approver': self.third.id, 'reason': '提前返岗'},
            format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['data']['reassigned']), 2)

        approval_one.refresh_from_db()
        approval_two.refresh_from_db()
        self.assertEqual(approval_one.approver_id, self.third.id)
        self.assertEqual(approval_two.approver_id, self.third.id)

        # 改派事件可追溯
        event = approval_one.events.get(event='reassigned')
        self.assertEqual(event.detail['from_approver'], 'leader')
        self.assertEqual(event.detail['to_approver'], 'third')

        # 代理人失去签署能力，接替审批人可以审批
        self.assertEqual(self.decide(self.deputy, approval_one).status_code, 403)
        self.assertEqual(self.decide(self.third, approval_one).status_code, 200)

    def test_revoke_without_fallback_reverts_to_grantor(self):
        delegation = self.make_delegation()
        _, approval = self.make_stock_out()

        self.login(self.leader)
        response = self.client.post(f'/api/delegations/{delegation.id}/revoke/', {}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['data']['reverted']), 1)

        approval.refresh_from_db()
        self.assertEqual(approval.approver_id, self.leader.id)
        event = approval.events.get(event='reverted')
        self.assertIn('退回原审批人', event.detail['reason'])

        # 原授权人本人仍可审批
        self.assertEqual(self.decide(self.leader, approval).status_code, 200)

    def test_revoke_cascades_sub_delegations(self):
        first = self.make_delegation(allow_sub_delegation=True)
        second = create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
        )
        _, approval = self.make_stock_out()

        revoke_delegation(delegation=first, revoked_by=self.leader)

        second.refresh_from_db()
        self.assertEqual(second.status, 'revoked')
        self.assertEqual(second.revoke_reason, '上级授权已撤销')
        self.assertEqual(self.decide(self.third, approval).status_code, 403)

    def test_revoke_fallback_sod_skips_item(self):
        """接替审批人是某出库单操作人时，该单退回原审批人，其余正常改派"""
        delegation = self.make_delegation()
        _, conflicted = self.make_stock_out(operator=self.third)
        _, normal = self.make_stock_out()

        self.login(self.leader)
        response = self.client.post(
            f'/api/delegations/{delegation.id}/revoke/',
            {'fallback_approver': self.third.id},
            format='json'
        )
        self.assertEqual(response.status_code, 200)

        conflicted.refresh_from_db()
        normal.refresh_from_db()
        self.assertEqual(conflicted.approver_id, self.leader.id)
        self.assertEqual(normal.approver_id, self.third.id)
        event = conflicted.events.get(event='reverted')
        self.assertIn('fallback_rejected', event.detail)

    def test_revoke_permission_denied_for_outsider(self):
        delegation = self.make_delegation()
        self.login(self.operator)
        response = self.client.post(f'/api/delegations/{delegation.id}/revoke/', {}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_double_revoke_rejected(self):
        delegation = self.make_delegation()
        self.login(self.leader)
        first = self.client.post(f'/api/delegations/{delegation.id}/revoke/', {}, format='json')
        second = self.client.post(f'/api/delegations/{delegation.id}/revoke/', {}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_other_effective_delegation_prevents_reassign(self):
        """仍存在其他有效授权可处理时，撤销其一不触发事项改派"""
        first = self.make_delegation()
        self.make_delegation(agent=self.third)
        _, approval = self.make_stock_out()

        revoke_delegation(
            delegation=first, revoked_by=self.leader, fallback_approver=self.fourth
        )

        approval.refresh_from_db()
        self.assertEqual(approval.approver_id, self.leader.id)
        self.assertFalse(approval.events.filter(event='reassigned').exists())
        # 另一代理人仍可正常签署
        self.assertEqual(self.decide(self.third, approval).status_code, 200)


class ExpirySweepTest(DelegationFixture):
    """授权到期后的未完成事项处理"""

    def expire(self, delegation):
        Delegation.objects.filter(pk=delegation.pk).update(
            valid_to=timezone.now() - timedelta(seconds=1)
        )

    def test_sweep_reverts_pending_items(self):
        delegation = self.make_delegation()
        _, approval = self.make_stock_out()
        self.expire(delegation)

        processed = sweep_expired_delegations()
        self.assertEqual(processed, 1)

        approval.refresh_from_db()
        self.assertEqual(approval.approver_id, self.leader.id)
        event = approval.events.get(event='reverted')
        self.assertIn('已到期', event.detail['reason'])

        # 幂等：重复执行不再处理
        self.assertEqual(sweep_expired_delegations(), 0)

    def test_sweep_leaves_items_covered_by_other_delegation(self):
        first = self.make_delegation()
        self.make_delegation(agent=self.third)
        _, approval = self.make_stock_out()
        self.expire(first)

        processed = sweep_expired_delegations()
        self.assertEqual(processed, 1)
        self.assertFalse(
            approval.events.filter(event__in=['reverted', 'reassigned']).exists()
        )
        # 另一代理人仍可签署
        self.assertEqual(self.decide(self.third, approval).status_code, 200)

    def test_sweep_endpoint_requires_admin(self):
        self.login(self.operator)
        response = self.client.post('/api/delegations/sweep/', {}, format='json')
        self.assertEqual(response.status_code, 403)

        self.login(self.leader)
        response = self.client.post('/api/delegations/sweep/', {}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_expired_delegation_display_status(self):
        delegation = self.make_delegation()
        self.expire(delegation)

        self.login(self.leader)
        response = self.client.get(f'/api/delegations/{delegation.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['display_status'], '已到期')
        self.assertFalse(data['is_effective'])


class DelegationQueryTest(DelegationFixture):
    """代理授权查询"""

    def test_list_scoping(self):
        self.make_delegation()
        # 普通用户看不到任何授权
        self.login(self.operator)
        response = self.client.get('/api/delegations/')
        self.assertEqual(response.json()['data']['total'], 0)
        # 管理员可查看全部，也可按范围筛选
        self.login(self.third)
        response = self.client.get('/api/delegations/')
        self.assertEqual(response.json()['data']['total'], 1)
        response = self.client.get('/api/delegations/?scope=received')
        self.assertEqual(response.json()['data']['total'], 0)
        self.login(self.deputy)
        response = self.client.get('/api/delegations/?scope=received')
        self.assertEqual(response.json()['data']['total'], 1)

    def test_detail_contains_chain(self):
        first = self.make_delegation(allow_sub_delegation=True)
        second = create_delegation(
            grantor=self.deputy,
            agent=self.third,
            business_types=['stock_out'],
            valid_from=self.now,
            valid_to=self.now + timedelta(days=3),
            parent=first,
        )
        self.login(self.leader)
        response = self.client.get(f'/api/delegations/{second.id}/')
        self.assertEqual(response.status_code, 200)
        chain = response.json()['data']['chain']
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]['grantor_name'], 'leader')
        self.assertEqual(chain[1]['agent_name'], 'third')
