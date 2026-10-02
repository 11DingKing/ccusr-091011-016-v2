"""
代理授权 URL 配置
"""
from django.urls import path

from .views import (
    DelegationListCreateView,
    DelegationDetailView,
    MyEffectiveDelegationsView,
    ApprovalActQueryView,
)

urlpatterns = [
    # 代理授权：授予 / 列表 / 详情 / 撤销
    path("delegations/", DelegationListCreateView.as_view(), name="delegation-list"),
    path("delegations/mine/", MyEffectiveDelegationsView.as_view(), name="delegation-mine"),
    path("delegations/<int:pk>/", DelegationDetailView.as_view(), name="delegation-detail"),

    # 审批授权链台账：每次决定的实际操作者与原授权人
    path("approval-acts/", ApprovalActQueryView.as_view(), name="approval-act-list"),
]
