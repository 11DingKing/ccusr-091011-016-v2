"""
代理授权URL配置
"""
from django.urls import path
from .views import (
    DelegationListView, DelegationDetailView,
    DelegationRevokeView, DelegationSweepView
)

urlpatterns = [
    path('delegations/', DelegationListView.as_view(), name='delegation-list'),
    path('delegations/sweep/', DelegationSweepView.as_view(), name='delegation-sweep'),
    path('delegations/<int:pk>/', DelegationDetailView.as_view(), name='delegation-detail'),
    path('delegations/<int:pk>/revoke/', DelegationRevokeView.as_view(), name='delegation-revoke'),
]
