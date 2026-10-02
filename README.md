# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 代理审批授权

审批负责人出差时，可将审批权在限定范围内授予代理人；系统对每张单据实时校验授权，事后可清晰追溯每次决定的实际操作者与原授权人。

- `POST /api/delegations/`：授予授权（代理人、业务种类、单笔金额上限、最高风险等级、生效/失效时间、事由）
- `GET  /api/delegations/`：授权列表（`direction=granted|received`、`status`、`business_type` 过滤）
- `GET  /api/delegations/mine/`：我当前持有的有效授权
- `POST /api/delegations/<id>/`：撤销授权（仅原授权人/超管）
- `POST /api/stock-out/`：提交出库申请（可带 `amount`、`risk_level`、`approver_owner`）
- `POST /api/stock-out/<id>/approval/`：作出审批决定（本人或代理人均调用此接口）
- `GET  /api/stock-out/?status=pending`：待办列表，每行含 `current_handler`（当前合法处理人）
- `GET  /api/approval-acts/`：审批授权链台账，`operator` 为实际操作者、`original_grantor` 为原授权人，`authority_description` 为可读说明，`chain` 为授权链快照

规则：

1. 授权限定代理人、业务种类、金额/风险范围和有效时间；
2. 禁止自己给自己授权、循环代理与多段转授（保存授权时双向时间窗校验）；
3. 申请人不得审批自己的单据，代理人亦同（职责分离）；
4. 授权撤销或到期后实时失效，待办单据自动重新解析合法审批人（负责人本人或新代理人），无人可处理时 `current_handler` 为空，需上报重新指派；
5. 每次代理审批同时写入 `wh_approval` 与 `dlg_approval_act`，固化授权链快照，授权事后撤销/删除不影响凭证。

