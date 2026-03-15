"""
兑换码管理 - 生成、验证、计次
"""
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional
from database import get_db, init_db


def generate_code(length: int = 12) -> str:
    """生成随机兑换码 (大写字母+数字, 易读)"""
    alphabet = string.ascii_uppercase + string.digits
    # 排除易混淆字符
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "").replace("L", "")
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4))
        for _ in range(length // 4)
    )


def create_codes(count: int = 1, total_uses: int = 1,
                 expires_days: Optional[int] = None, note: str = "") -> list[str]:
    """批量创建兑换码"""
    now = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days else None
    codes = []
    with get_db() as conn:
        for _ in range(count):
            code = generate_code()
            conn.execute(
                "INSERT INTO codes (code, total_uses, used_count, created_at, expires_at, note) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (code, total_uses, now, expires_at, note),
            )
            codes.append(code)
    return codes


def validate_code(code: str) -> tuple[bool, str]:
    """
    验证兑换码是否可用。
    返回 (valid, message)
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code.strip(),)).fetchone()

    if not row:
        return False, "兑换码不存在"

    if row["expires_at"]:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return False, "兑换码已过期"

    if row["used_count"] >= row["total_uses"]:
        return False, f"兑换码已用完 ({row['used_count']}/{row['total_uses']})"

    remaining = row["total_uses"] - row["used_count"]
    return True, f"有效 (剩余 {remaining}/{row['total_uses']} 次)"


def reserve_use(code: str, plan_type: str = "", amount: int = 1) -> Optional[int]:
    """
    预留使用额度，创建 pending 执行记录。
    立即增加 used_count（先扣），失败时在 complete_use 中部分回退。
    amount: 扣减数量（新注册=2, 其他=1）
    返回 execution_id，如果码无效返回 None。
    """
    code = code.strip()
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return None
        if row["used_count"] + amount > row["total_uses"]:
            return None

        conn.execute(
            "UPDATE codes SET used_count = used_count + ? WHERE code = ?",
            (amount, code),
        )
        cursor = conn.execute(
            "INSERT INTO executions (code, plan_type, status, reserved_amount, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (code, plan_type, amount, now, now),
        )
        return cursor.lastrowid


def update_execution(execution_id: int, *, status: str = None,
                     email: str = None, error_msg: str = None,
                     result_json: str = None):
    """更新执行记录的部分字段"""
    updates = []
    params = []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if email is not None:
        updates.append("email = ?")
        params.append(email)
    if error_msg is not None:
        updates.append("error_msg = ?")
        params.append(error_msg)
    if result_json is not None:
        updates.append("result_json = ?")
        params.append(result_json)
    if not updates:
        return

    updates.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(execution_id)

    with get_db() as conn:
        conn.execute(
            f"UPDATE executions SET {', '.join(updates)} WHERE id = ?",
            params,
        )


def complete_use(execution_id: int, success: bool, email: str = "",
                 error_msg: str = "", result_json: str = ""):
    """
    完成执行：
    - 成功: 保持已扣减的额度, status='success'
    - 失败: 回退部分额度 (保留 1 次扣减, 返还多余的), status='failed'
      例: 新注册扣了2 → 失败返还1 → 净消耗1
    """
    now = datetime.now().isoformat()
    status = "success" if success else "failed"

    with get_db() as conn:
        row = conn.execute(
            "SELECT code, reserved_amount FROM executions WHERE id=?", (execution_id,)
        ).fetchone()

        # 更新执行记录
        conn.execute(
            "UPDATE executions SET status=?, email=?, error_msg=?, result_json=?, updated_at=? "
            "WHERE id=?",
            (status, email, error_msg, result_json, now, execution_id),
        )
        # 失败时回退额度
        # 新注册(reserved>=2): 保留1次消耗(邮箱已创建), 退还其余
        # 已有账号(reserved=1): 全额退还, 净消耗0
        if not success and row:
            reserved_amount = row["reserved_amount"] or 1
            failure_cost = 1 if reserved_amount >= 2 else 0
            refund = reserved_amount - failure_cost
            if refund > 0:
                conn.execute(
                    "UPDATE codes SET used_count = MAX(used_count - ?, 0) WHERE code=?",
                    (refund, row["code"]),
                )


def get_code_history(code: str) -> list[dict]:
    """获取兑换码的执行历史"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE code=? ORDER BY created_at DESC",
            (code.strip(),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_code_info(code: str) -> Optional[dict]:
    """获取兑换码信息"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM codes WHERE code=?", (code.strip(),)).fetchone()
    return dict(row) if row else None


def list_all_codes() -> list[dict]:
    """列出所有兑换码 (管理用)"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM executions e WHERE e.code=c.code AND e.status='success') as success_count, "
            "(SELECT COUNT(*) FROM executions e WHERE e.code=c.code AND e.status='failed') as fail_count "
            "FROM codes c ORDER BY c.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
