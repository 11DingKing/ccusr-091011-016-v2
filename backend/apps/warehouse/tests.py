from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .models import (
    Approval, ApprovalEvent, Category, Goods, StockIn, StockOut, Unit, Variety, Warning
)


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


class StockOutFlowTest(WarehouseFixture):
    """出库申请与审批流程测试"""

    def setUp(self):
        super().setUp()
        self.approver = User.objects.create_user("approver", "testpass123", role="admin")

    def create_stock_out(self, **overrides):
        payload = {
            "goods": self.goods.id,
            "receiver": "领用人",
            "quantity": "2",
            "risk_level": "low",
            "approver": self.approver.id,
        }
        payload.update(overrides)
        return self.client.post("/api/stock-out/", payload, format="json")

    def test_create_stock_out_creates_pending_approval(self):
        response = self.create_stock_out()
        self.assertEqual(response.status_code, 200)

        stock_out = StockOut.objects.get()
        self.assertEqual(stock_out.status, "pending")
        self.assertEqual(stock_out.operator_id, self.user.id)

        approval = Approval.objects.get()
        self.assertEqual(approval.stock_out_id, stock_out.id)
        self.assertEqual(approval.approver_id, self.approver.id)
        self.assertEqual(approval.status, "pending")

        event = ApprovalEvent.objects.get()
        self.assertEqual(event.event, "created")
        self.assertEqual(event.actor_id, self.user.id)

    def test_create_stock_out_rejects_self_approval(self):
        response = self.create_stock_out(approver=self.user.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("职责分离", response.json()["message"])

    def test_create_stock_out_rejects_non_admin_approver(self):
        plain = User.objects.create_user("plain-user", "testpass123", role="user")
        response = self.create_stock_out(approver=plain.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("审批权限", response.json()["message"])

    def test_create_stock_out_rejects_exceeding_quantity(self):
        response = self.create_stock_out(quantity="999")
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能超过当前库存", response.json()["message"])

    def test_approve_and_reject_update_stock_out_status(self):
        self.create_stock_out()
        approval = Approval.objects.get()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.approver)}")
        rejected = self.client.post(
            f"/api/approvals/{approval.id}/decide/", {"action": "rejected"}, format="json"
        )
        self.assertEqual(rejected.status_code, 200)
        approval.refresh_from_db()
        self.assertEqual(approval.status, "rejected")
        self.assertEqual(approval.stock_out.status, "rejected")
        self.assertEqual(approval.actual_operator_id, self.approver.id)

    def test_decide_requires_pending_status(self):
        self.create_stock_out()
        approval = Approval.objects.get()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.approver)}")
        url = f"/api/approvals/{approval.id}/decide/"
        first = self.client.post(url, {"action": "approved"}, format="json")
        second = self.client.post(url, {"action": "approved"}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_approval_list_scoped_for_non_admin(self):
        self.create_stock_out()
        other = User.objects.create_user("other-user", "testpass123", role="user")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(other)}")
        response = self.client.get("/api/approvals/")
        self.assertEqual(response.json()["data"]["total"], 0)
