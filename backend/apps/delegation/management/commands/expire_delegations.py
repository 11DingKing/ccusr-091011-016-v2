"""
处理到期代理授权的管理命令。
"""
from django.core.management.base import BaseCommand
from apps.delegation.services import sweep_expired_delegations


class Command(BaseCommand):
    help = '处理已到期的代理授权：未完成事项重新寻找合法审批人（退回原授权人）'

    def handle(self, *args, **options):
        processed = sweep_expired_delegations()
        self.stdout.write(self.style.SUCCESS(f'已处理 {processed} 条到期授权'))
