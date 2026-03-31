from dataclasses import dataclass


@dataclass
class UserContext:
    """用户上下文"""
    user_id: str  # 用户ID
    username: str  # 用户名
    staff_id: str  # 员工ID
    staff_name: str  # 员工姓名
    user_code: str  # 用户编码
    token: str  # 认证token

    @classmethod
    def from_auth_response(cls, data: dict, token: str) -> 'UserContext':
        """从认证接口响应数据创建UserContext"""
        user_session = data.get('userSessionInfo', {})
        return cls(
            user_id=str(user_session.get('userId', '')),
            username=user_session.get('username', ''),
            staff_id=str(user_session.get('staffId', '')),
            staff_name=user_session.get('staffName', ''),
            user_code=data.get('userCode', ''),
            token=token
        )


def get_current_user_context():
    """获取当前请求的用户上下文"""
    from flask import g
    return getattr(g, 'user_context', None)
