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

## 代理授权

审批负责人出差时可将审批权授予同事代办，系统区分本人决定与代理决定：

- `POST /api/delegations/` 创建授权，限定代理人（`agent`）、业务种类（`business_types`）、
  数量上限（`max_quantity`）、风险等级上限（`max_risk_level`）与有效时间（`valid_from`/`valid_to`）；
  传入 `parent` 可转委托，但范围只能收窄、禁止循环代理与超长授权链（最长 3 段）。
- `GET /api/delegations/`、`GET /api/delegations/<id>/` 查询授权及完整授权链。
- `POST /api/delegations/<id>/revoke/` 撤销授权并级联撤销下级转委托；
  可指定 `fallback_approver` 改派未完成事项，否则退回原授权人本人。
- `POST /api/delegations/sweep/`（或 `python manage.py expire_delegations`）处理到期授权，
  未完成事项重新寻找合法审批人。
- `POST /api/stock-out/` 创建出库申请时指定审批人（不得指定自己，职责分离）。
- `POST /api/approvals/<id>/decide/` 审批决定；代理人签署时校验授权链有效性与业务范围，
  出库操作人不得参与本单决定。`GET /api/approvals/` 返回每次决定的原授权人（`approver`）、
  实际操作人（`actual_operator`）与授权链（`authorization_chain`）。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
