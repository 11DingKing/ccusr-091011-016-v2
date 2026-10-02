"""
代理授权测试：授权范围、防自授权/循环/多段代理、撤销到期失效、
授权链记录与合法审批人重新解析。
"""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, StockOut, Unit, Variety
from apps.warehouse.approval_service import ApprovalError, find_current_handler, perform_approval
from .models import (
    ApprovalAct,
    DelegationPolicy,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    expire_due_policies,
)


def hours_from_now(h):
    return timezone.now() + timedelta(hours=h)


class DelegationFixture(TestCase):
    def setUp(self):
        # 审批负责人（出差者）、代理人、另一名管理员、普通申请人
        self.boss = User.objects.create_user("boss", "p", role="admin", real_name="张负责")
        self.agent = User.objects.create_user("agent", "p", role="user", real_name="李代理")
        self.boss2 = User.objects.create_user("boss2", "p", role="admin", real_name="王主管")
        self.applicant = User.objects.create_user("applicant", "p", role="user", real_name="赵申请")

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.boss)}")

        unit = Unit.objects.create(name="件", created_by=self.boss)
        cat = Category.objects.create(name="受控器材", unit=unit, created_by=self.boss)
        variety = Variety.objects.create(name="终端", category=cat, created_by=self.boss)
        self.goods = Goods.objects.create(
            variety=variety, name="执法终端", code="D-1", quantity=Decimal("10"))

    def make_stock_out(self, amount=Decimal("100"), risk="normal", owner=None, operator=None):
        return StockOut.objects.create(
            goods=self.goods,
            operator=operator or self.applicant,
            receiver="使用人",
            quantity=Decimal("1"),
            amount=amount,
            risk_level=risk,
            approver_owner=owner or self.boss,
        )

    def grant(self, principal, agent, **kw):
        kw.setdefault("end_time", hours_from_now(8))
        return DelegationPolicy.objects.create(
            principal=principal, agent=agent, **kw)


class DelegationPolicyModelTest(DelegationFixture):
    def test_self_delegation_rejected(self):
        with self.assertRaises(ValidationError):
            self.grant(self.boss, self.boss)

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValidationError):
            DelegationPolicy.objects.create(
                principal=self.boss, agent=self.agent,
                start_time=hours_from_now(5), end_time=hours_from_now(2))

    def test_expired_policy_created_expired(self):
        p = DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent, end_time=hours_from_now(-1))
        self.assertEqual(p.status, STATUS_EXPIRED)
        self.assertFalse(p.is_effective())

    def test_scope_covers_amount_and_risk(self):
        p = self.grant(self.boss, self.agent,
                       max_amount=Decimal("500"), max_risk_level="important")
        self.assertTrue(p.covers("stock_out_approval", Decimal("100"), "normal"))
        self.assertTrue(p.covers("stock_out_approval", Decimal("500"), "important"))
        self.assertFalse(p.covers("stock_out_approval", Decimal("500.01"), "normal"))
        self.assertFalse(p.covers("stock_out_approval", Decimal("100"), "critical"))
        self.assertFalse(p.covers("other_business", Decimal("1"), "normal"))

    def test_circular_delegation_rejected(self):
        # boss -> agent 后，agent 不能反向授权给 boss（形成环）
        self.grant(self.boss, self.agent)
        with self.assertRaises(ValidationError):
            self.grant(self.agent, self.boss)

    def test_multi_hop_delegation_rejected(self):
        # boss -> agent 后，agent 不得再授权给第三人
        third = User.objects.create_user("third", "p", role="user")
        self.grant(self.boss, self.agent)
        with self.assertRaises(ValidationError):
            self.grant(self.agent, third)

    def test_multi_hop_reverse_creation_order_rejected(self):
        # 先建 agent -> third（agent 此时尚未代理他人，允许），
        # 再建 boss -> agent，时间窗重叠，必须拒绝
        third = User.objects.create_user("third2", "p", role="user")
        self.grant(self.agent, third)
        with self.assertRaises(ValidationError):
            self.grant(self.boss, self.agent)

    def test_non_overlapping_windows_allowed(self):
        # 时间窗不重叠时，同一用户可先后充当不同角色
        third = User.objects.create_user("third3", "p", role="user")
        DelegationPolicy.objects.create(
            principal=self.agent, agent=third,
            end_time=timezone.now() + timedelta(hours=1))
        # boss 对 agent 的授权从 2 小时后开始
        DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent,
            start_time=timezone.now() + timedelta(hours=2),
            end_time=timezone.now() + timedelta(hours=5))

    def test_revoke(self):
        p = self.grant(self.boss, self.agent)
        self.assertTrue(p.revoke(revoked_by=self.boss))
        self.assertEqual(p.status, STATUS_REVOKED)
        self.assertFalse(p.revoke())  # 不可重复撤销


class ApprovalWithDelegationTest(DelegationFixture):
    def test_owner_self_approval(self):
        so = self.make_stock_out()
        handler = find_current_handler(so)
        self.assertEqual(handler["user"], self.boss)
        self.assertEqual(handler["kind"], "self")

        approval = perform_approval(so, self.boss, "approved", "同意")
        self.assertFalse(approval.is_delegated)
        self.assertEqual(approval.actor, self.boss)
        self.assertEqual(approval.approver, self.boss)

    def test_future_delegation_does_not_activate(self):
        self.grant(self.boss, self.agent,
                   start_time=hours_from_now(2), end_time=hours_from_now(5))
        so = self.make_stock_out()
        # 授权尚未生效：负责人本人仍是处理人，代理人无权审批
        self.assertEqual(find_current_handler(so)["kind"], "self")
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

    def test_scope_gap_falls_back_to_owner(self):
        # 授权金额上限低于本单据：代理不成立，回到负责人本人
        self.grant(self.boss, self.agent, max_amount=Decimal("50"))
        so = self.make_stock_out(amount=Decimal("500"))
        handler = find_current_handler(so)
        self.assertEqual(handler["kind"], "self")
        self.assertEqual(handler["user"], self.boss)
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

    def test_agent_approval_records_chain(self):
        self.grant(self.boss, self.agent)
        so = self.make_stock_out()
        handler = find_current_handler(so)
        self.assertEqual(handler["kind"], "delegated")
        self.assertEqual(handler["user"], self.agent)

        approval = perform_approval(so, self.agent, "approved", "代批")
        self.assertTrue(approval.is_delegated)
        self.assertEqual(approval.actor, self.agent)       # 实际操作者
        self.assertEqual(approval.approver, self.boss)    # 原授权人
        chain = approval.authority_chain
        self.assertEqual(chain["length"], 1)
        self.assertEqual(chain["chain"][0]["role"], "principal")
        self.assertEqual(chain["chain"][1]["role"], "agent")

        act = ApprovalAct.objects.get(subject_id=so.id)
        self.assertIn("李代理", act.authority_summary())
        self.assertIn("张负责", act.authority_summary())
        self.assertTrue(act.is_delegated)

    def test_applicant_cannot_approve_even_as_agent(self):
        # 职责分离：代理人恰好是申请人时不得审批
        self.grant(self.boss, self.applicant)
        so = self.make_stock_out()
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.applicant, "approved")

    def test_amount_out_of_scope_rejected(self):
        self.grant(self.boss, self.agent, max_amount=Decimal("100"))
        so = self.make_stock_out(amount=Decimal("101"))
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

    def test_risk_out_of_scope_rejected(self):
        self.grant(self.boss, self.agent, max_risk_level="normal")
        so = self.make_stock_out(risk="critical")
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

    def test_revoked_delegation_blocks_agent_and_falls_back(self):
        policy = self.grant(self.boss, self.agent)
        so = self.make_stock_out()
        policy.revoke(revoked_by=self.boss)

        # 代理人即刻失去权力
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

        # 撤销后重新寻找：负责人本人仍是合法审批人
        handler = find_current_handler(so)
        self.assertEqual(handler["user"], self.boss)
        self.assertEqual(handler["kind"], "self")

    def test_expired_delegation_blocks_agent(self):
        DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent, end_time=hours_from_now(-1))
        so = self.make_stock_out()
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")

    def test_reassign_to_new_agent_after_revoke(self):
        # boss 撤销对 agent 的授权后，boss2 接手并授权 agent 代办
        self.grant(self.boss, self.agent).revoke()
        so = self.make_stock_out()
        # 尚未接手：负责人本人可处理
        self.assertEqual(find_current_handler(so)["user"], self.boss)
        # 负责人改为 boss2，并由其代理人处理
        self.grant(self.boss2, self.agent)
        so.approver_owner = self.boss2
        so.save()
        handler = find_current_handler(so)
        self.assertEqual(handler["user"], self.agent)
        self.assertEqual(handler["owner"], self.boss2)

    def test_no_handler_when_everyone_unavailable(self):
        # 负责人停用且无任何授权 -> 无人可处理
        so = self.make_stock_out()
        self.boss.is_active = False
        self.boss.save()
        handler = find_current_handler(so)
        self.assertIsNone(handler["user"])

    def test_double_approval_rejected(self):
        so = self.make_stock_out()
        perform_approval(so, self.boss, "approved")
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.boss, "approved")

    def test_expire_due_policies_job(self):
        p = DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent,
            end_time=timezone.now() + timedelta(seconds=1))
        p.status = STATUS_ACTIVE
        super(DelegationPolicy, p).save()
        # 手动把 end_time 改为过去而不触发 save 逻辑
        DelegationPolicy.objects.filter(pk=p.pk).update(
            end_time=timezone.now() - timedelta(seconds=1))
        self.assertEqual(expire_due_policies(), 1)
        p.refresh_from_db()
        self.assertEqual(p.status, STATUS_EXPIRED)


class DelegationAPITest(DelegationFixture):
    def test_create_delegation_api(self):
        resp = self.client.post("/api/delegations/", {
            "agent_id": self.agent.id,
            "business_type": "stock_out_approval",
            "max_amount": "1000",
            "max_risk_level": "important",
            "end_time": hours_from_now(8).isoformat(),
            "reason": "出差",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["agent"], self.agent.id)
        self.assertTrue(data["effective_now"])

    def test_self_delegation_api_rejected(self):
        resp = self.client.post("/api/delegations/", {
            "agent_id": self.boss.id,
            "end_time": hours_from_now(8).isoformat(),
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("自己", resp.json()["message"])

    def test_non_admin_cannot_grant(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.agent)}")
        resp = client.post("/api/delegations/", {
            "agent_id": self.boss2.id,
            "end_time": hours_from_now(8).isoformat(),
        }, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_revoke_api(self):
        p = self.grant(self.boss, self.agent)
        resp = self.client.post(f"/api/delegations/{p.id}/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "revoked")

    def test_other_user_cannot_revoke(self):
        p = self.grant(self.boss, self.agent)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.boss2)}")
        resp = client.post(f"/api/delegations/{p.id}/", {}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_approval_act_query_shows_operator_and_grantor(self):
        self.grant(self.boss, self.agent)
        so = self.make_stock_out()
        perform_approval(so, self.agent, "approved")

        resp = self.client.get("/api/approval-acts/?delegated=true")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()["data"]["list"][0]
        self.assertEqual(row["operator_name"], "李代理")
        self.assertEqual(row["original_grantor_name"], "张负责")
        self.assertTrue(row["is_delegated"])
        self.assertIn("原授权人", row["authority_description"])
