# Ragent 初始账号（PostgreSQL 初始化后）

> 本地开发环境（`127.0.0.1:5432`，库 `ragent`）从
> `resources/database/init_data_pg.sql` 导入的默认账号。
> 仅供本地/开发环境使用，**生产环境务必修改密码**。

## 管理员账号

| 字段 | 值 |
|---|---|
| 用户名 | `admin` |
| 密码 | `admin` |
| 角色 | `admin` |
| ID | `2001523723396308993` |
| 头像 | `https://static.deepseek.com/user-avatar/G_6cuD8GbD53VwGRwisvCsZ6` |

> 表：`t_user`（`username` / `password` 列当前为明文，建议尽快改造为加密存储）

## 登录入口

- 应用启动后，前端登录页 `/login`
- 用户名：`admin` / 密码：`admin`

## 速查 SQL

```sql
-- 查看所有初始用户
SELECT id, username, password, role, create_time FROM t_user WHERE deleted = 0;

-- 修改密码（建议立即改）
UPDATE t_user SET password = 'your_new_password', update_time = NOW()
WHERE username = 'admin' AND deleted = 0;
```

## 初始化时间

2026-08-23
