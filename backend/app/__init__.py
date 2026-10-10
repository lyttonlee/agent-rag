"""企业公文知识库 RAG 引擎服务。

定位：被第三方公文系统集成的标准化 RAG 引擎。
我方制定接口 / 字段 / 密级 / 权限 / 模型接入标准，第三方适配。

模块划分（模块化单体）：
- core/      : 配置、安全（密级 / 权限下推）
- db/        : 数据模型、会话、迁移
- schemas/   : 请求 / 响应契约（与《接口文档》对齐）
- services/  : 解析、切片、入库、检索、生成、模型 Provider
- api/       : REST 路由（/v1/documents、/retrieve、/generate、/templates、/export、/config）
"""

__version__ = "1.2.0"
