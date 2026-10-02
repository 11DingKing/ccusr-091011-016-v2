from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.delegation.models import DelegationPolicy
from .models import Approval, Category, Goods, StockIn, StockOut, Unit, Variety, Warning
from .approval_service import ApprovalError, perform_approval
from django.utils import timezone
from datetime import timedelta


class WarehouseFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("warehouse-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="受控器材", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name="执法记录终端",
            code="DEV-001",
            quantity=Decimal("12"),
            warning_threshold=Decimal("5"),
        )


class WarehouseModelTest(WarehouseFixture):
    def test_relationship_flags(self):
        self.assertTrue(self.unit.is_linked)
        self.assertTrue(self.category.is_linked)
        self.assertTrue(self.variety.is_in_stock)
        self.assertFalse(self.goods.is_warning)

    def test_unique_unit_name(self):
        with self.assertRaises(IntegrityError):
            Unit.objects.create(name="件", created_by=self.user)

    def test_stock_records_and_approval(self):
        inbound = StockIn.objects.create(goods=self.goods, operator=self.user, quantity=Decimal("3"))
        outbound = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="保管员", quantity=Decimal("2")
        )
        approval = Approval.objects.create(stock_out=outbound, approver=self.user)
        self.assertEqual(inbound.goods_id, self.goods.id)
        self.assertEqual(approval.status, "pending")

    def test_warning_record(self):
        warning = Warning.objects.create(goods=self.goods, type="low_stock", message="库存不足")
        self.assertFalse(warning.is_read)
        self.assertIn("执法记录终端", str(warning))


class WarehouseAPITest(WarehouseFixture):
    def test_list_units(self):
        response = self.client.get("/api/units/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_create_unit_and_reject_duplicate(self):
        created = self.client.post("/api/units/", {"name": "箱"}, format="json")
        duplicate = self.client.post("/api/units/", {"name": "箱"}, format="json")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_update_linked_unit(self):
        response = self.client.put(f"/api/units/{self.unit.id}/", {"name": "台"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.name, "台")

    def test_refuse_delete_linked_unit(self):
        response = self.client.delete(f"/api/units/{self.unit.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Unit.objects.filter(pk=self.unit.id).exists())

    def test_create_category_validates_unit(self):
        ok = self.client.post("/api/categories/", {"name": "封存介质", "unit": self.unit.id}, format="json")
        bad = self.client.post("/api/categories/", {"name": "无效分类", "unit": 99999}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)

    def test_create_variety_and_duplicate_boundary(self):
        ok = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        duplicate = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_requires_authentication(self):
        anonymous = APIClient().get("/api/units/")
        self.assertEqual(anonymous.status_code, 401)


class StockOutApprovalTest(WarehouseFixture):
    """出库申请-审批（含代理授权）端到端测试"""

    def setUp(self):
        super().setUp()
        self.boss = User.objects.create_user("boss-x", "p", role="admin", real_name="周审批")
        self.agent = User.objects.create_user("agent-x", "p", role="user", real_name="吴代办")

    def _create_stock_out(self, client=None, owner=None):
        client = client or self.client
        return client.post("/api/stock-out/", {
            "goods": self.goods.id,
            "receiver": "使用人",
            "quantity": "1",
            "amount": "100",
            "risk_level": "normal",
            "approver_owner": (owner or self.boss).id,
        }, format="json")

    def test_create_stock_out(self):
        resp = self._create_stock_out()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["approver_owner"], self.boss.id)

    def test_owner_approve_api(self):
        so_id = self._create_stock_out().json()["data"]["id"]
        boss_client = APIClient()
        boss_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.boss)}")
        resp = boss_client.post(f"/api/stock-out/{so_id}/approval/",
                               {"decision": "approved"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertFalse(data["is_delegated"])
        self.assertEqual(data["actor"], self.boss.id)
        self.assertEqual(data["approver"], self.boss.id)
        self.assertIn("本人审批", data["authority_description"])

    def test_delegated_approval_api(self):
        DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent,
            end_time=timezone.now() + timedelta(hours=8))
        so_id = self._create_stock_out().json()["data"]["id"]

        agent_client = APIClient()
        agent_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.agent)}")
        resp = agent_client.post(f"/api/stock-out/{so_id}/approval/",
                                {"decision": "approved", "remark": "出差代办"},
                                format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertTrue(data["is_delegated"])
        self.assertEqual(data["actor"], self.agent.id)       # 实际操作者
        self.assertEqual(data["approver"], self.boss.id)    # 原授权人
        self.assertIn("代理人", data["authority_description"])
        self.assertIn("原授权人", data["authority_description"])

    def test_list_shows_current_handler(self):
        DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent,
            end_time=timezone.now() + timedelta(hours=8))
        self._create_stock_out()
        resp = self.client.get("/api/stock-out/?status=pending")
        row = resp.json()["data"]["list"][0]
        self.assertEqual(row["current_handler"]["user_id"], self.agent.id)
        self.assertEqual(row["current_handler"]["kind"], "delegated")

    def test_agent_without_delegation_forbidden(self):
        so_id = self._create_stock_out().json()["data"]["id"]
        agent_client = APIClient()
        agent_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.agent)}")
        resp = agent_client.post(f"/api/stock-out/{so_id}/approval/",
                                {"decision": "approved"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_revoked_delegation_forbidden(self):
        policy = DelegationPolicy.objects.create(
            principal=self.boss, agent=self.agent,
            end_time=timezone.now() + timedelta(hours=8))
        so = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="r",
            quantity=Decimal("1"), approver_owner=self.boss)
        policy.revoke()
        with self.assertRaises(ApprovalError):
            perform_approval(so, self.agent, "approved")
