"""
自动化绑卡支付 - Streamlit UI
运行: streamlit run ui.py --server.address 0.0.0.0 --server.port 8503
"""
import json
import logging
import os
import sys
import traceback
import threading
from collections import deque

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, CardInfo, BillingInfo, CaptchaConfig
from mail_provider import MailProvider
from auth_flow import AuthFlow, AuthResult
from payment_flow import PaymentFlow
from logger import ResultStore
from database import init_db
from code_manager import validate_code, reserve_use, complete_use, update_execution, get_code_history, get_code_info

init_db()

OUTPUT_DIR = "test_outputs"

import re as _re

# 国家名/后缀 → (country_code, currency) 映射
_COUNTRY_ALIAS = {
    "UK": ("GB", "GBP"), "GB": ("GB", "GBP"), "England": ("GB", "GBP"), "United Kingdom": ("GB", "GBP"), "英国": ("GB", "GBP"),
    "US": ("US", "USD"), "USA": ("US", "USD"), "United States": ("US", "USD"), "美国": ("US", "USD"),
    "DE": ("DE", "EUR"), "Germany": ("DE", "EUR"), "德国": ("DE", "EUR"),
    "JP": ("JP", "JPY"), "Japan": ("JP", "JPY"), "日本": ("JP", "JPY"),
    "FR": ("FR", "EUR"), "France": ("FR", "EUR"), "法国": ("FR", "EUR"),
    "SG": ("SG", "SGD"), "Singapore": ("SG", "SGD"), "新加坡": ("SG", "SGD"),
    "HK": ("HK", "HKD"), "Hong Kong": ("HK", "HKD"), "香港": ("HK", "HKD"),
    "KR": ("KR", "KRW"), "Korea": ("KR", "KRW"), "韩国": ("KR", "KRW"),
    "AU": ("AU", "AUD"), "Australia": ("AU", "AUD"), "澳大利亚": ("AU", "AUD"),
    "CA": ("CA", "CAD"), "Canada": ("CA", "CAD"), "加拿大": ("CA", "CAD"),
    "NL": ("NL", "EUR"), "Netherlands": ("NL", "EUR"), "荷兰": ("NL", "EUR"),
    "IT": ("IT", "EUR"), "Italy": ("IT", "EUR"), "意大利": ("IT", "EUR"),
    "ES": ("ES", "EUR"), "Spain": ("ES", "EUR"), "西班牙": ("ES", "EUR"),
    "CH": ("CH", "CHF"), "Switzerland": ("CH", "CHF"), "瑞士": ("CH", "CHF"),
}


def _parse_card_text(text: str) -> dict:
    """从粘贴文本中解析卡号、有效期、CVV、账单地址。
    支持两种格式:
    1) 纯文本: 卡号一行、MM/YY一行、CVV一行、账单地址一行
    2) 键值对: 卡号: xxx / 有效期: MMYY / CVV: xxx / 地址: xxx / 城市: xxx / 邮编: xxx / 国家: xxx
    """
    result = {}
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # 构建键值映射 (支持 "键: 值" 和 "键：值")
    kv = {}
    for line in lines:
        m = _re.match(r'^(.+?)\s*[:：]\s*(.+)$', line)
        if m:
            kv[m.group(1).strip().lower()] = m.group(2).strip()

    # ── 卡号 ──
    # 从键值对获取
    for k in ("卡号", "card number", "card", "card_number"):
        if k in kv:
            digits = kv[k].replace(" ", "").replace("-", "")
            if digits.isdigit() and 13 <= len(digits) <= 19:
                result["card_number"] = digits
                break
    # 回退: 纯数字行
    if "card_number" not in result:
        for line in lines:
            digits_only = line.replace(" ", "").replace("-", "")
            if digits_only.isdigit() and 13 <= len(digits_only) <= 19:
                result["card_number"] = digits_only
                break

    # ── 有效期 ──
    # 从键值对获取 (支持 MMYY, MM/YY, MM/YYYY)
    for k in ("有效期", "exp", "expiry", "expiration", "exp_date"):
        if k in kv:
            val = kv[k]
            # MM/YY 或 MM/YYYY
            m = _re.search(r'(0[1-9]|1[0-2])\s*/\s*(\d{2,4})', val)
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break
            # MMYY 或 MMYYYY (无分隔符)
            m = _re.search(r'^(0[1-9]|1[0-2])(\d{2,4})$', val.strip())
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break
    # 回退: 逐行寻找 MM/YY
    if "exp_month" not in result:
        for line in lines:
            m = _re.search(r'\b(0[1-9]|1[0-2])\s*/\s*(\d{2,4})\b', line)
            if m:
                result["exp_month"] = m.group(1)
                yr = m.group(2)
                if len(yr) == 2:
                    yr = "20" + yr
                result["exp_year"] = yr
                break

    # ── CVV ──
    for k in ("cvv", "cvc", "安全码"):
        if k in kv:
            m = _re.search(r'\b(\d{3,4})\b', kv[k])
            if m:
                result["cvv"] = m.group(1)
                break
    if "cvv" not in result:
        for i, line in enumerate(lines):
            if _re.search(r'(?i)\b(?:cvv|cvc|安全码)\b', line):
                m = _re.search(r'\b(\d{3,4})\b', line)
                if m:
                    result["cvv"] = m.group(1)
                elif i + 1 < len(lines):
                    m2 = _re.search(r'\b(\d{3,4})\b', lines[i + 1])
                    if m2:
                        result["cvv"] = m2.group(1)
                break

    # ── 地址: 键值对模式 (地址/城市/州/邮编/国家 分字段) ──
    kv_addr = None
    for k in ("地址", "address", "address_line1"):
        if k in kv:
            kv_addr = kv[k]
            break
    kv_city = None
    for k in ("城市", "city"):
        if k in kv:
            kv_city = kv[k]
            break
    kv_state = None
    for k in ("州", "state", "省"):
        if k in kv:
            kv_state = kv[k]
            break
    kv_zip = None
    for k in ("邮编", "postal_code", "zip", "zipcode", "zip_code"):
        if k in kv:
            kv_zip = kv[k]
            break
    kv_country = None
    for k in ("国家", "country", "地区"):
        if k in kv:
            kv_country = kv[k]
            break

    if kv_addr:
        result["address_line1"] = kv_addr
        if kv_city:
            result["address_city"] = kv_city
            result["address_state"] = kv_state or kv_city
        elif kv_state:
            result["address_state"] = kv_state
        if kv_zip:
            result["postal_code"] = kv_zip
        if kv_country:
            ci = _COUNTRY_ALIAS.get(kv_country)
            if ci:
                result["country_code"] = ci[0]
                result["currency"] = ci[1]
        # 构建 raw_address
        parts = [kv_addr]
        if kv_city:
            parts.append(kv_city)
        if kv_state:
            parts.append(kv_state)
        if kv_zip:
            parts.append(kv_zip)
        if kv_country:
            parts.append(kv_country)
        result["raw_address"] = ", ".join(parts)

    # ── 地址: 回退 "账单地址" / "billing address" 单行模式 ──
    if "address_line1" not in result:
        addr_text = ""
        for i, line in enumerate(lines):
            if _re.search(r'(?i)账单地址|billing\s*address', line):
                after = _re.sub(r'(?i)^.*?(账单地址|billing\s*address)\s*[:：]?\s*', '', line).strip()
                if after and len(after) > 3:
                    addr_text = after
                else:
                    for j in range(i + 1, min(i + 5, len(lines))):
                        candidate = lines[j]
                        if candidate and candidate not in ("复制", "copy", ""):
                            addr_text = candidate
                            break
                break

        if addr_text:
            result["raw_address"] = addr_text
            parts = [p.strip() for p in addr_text.split(",")]
            if len(parts) >= 2:
                last = parts[-1].strip()
                country_info = _COUNTRY_ALIAS.get(last)
                if country_info:
                    result["country_code"] = country_info[0]
                    result["currency"] = country_info[1]
                    parts = parts[:-1]

                for idx, p in enumerate(parts):
                    if _re.search(r'\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b', p.strip(), _re.IGNORECASE):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break
                    elif _re.search(r'\b\d{5}(-\d{4})?\b', p.strip()):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break
                    elif _re.search(r'\b\d{3}-\d{4}\b', p.strip()):
                        result["postal_code"] = p.strip()
                        parts.pop(idx)
                        break

                if len(parts) == 1:
                    result["address_line1"] = parts[0]
                elif len(parts) == 2:
                    result["address_line1"] = parts[0]
                    result["address_state"] = parts[1]
                elif len(parts) >= 3:
                    result["address_line1"] = parts[0]
                    result["address_state"] = parts[1]

    # ── 姓名 ──
    for k in ("姓名", "name", "cardholder", "持卡人"):
        if k in kv:
            result["billing_name"] = kv[k]
            break

    return result


# 国家 → (code, currency, state, address, postal_code)
COUNTRY_MAP = {
    "US - 美国": ("US", "USD", "California", "123 Main St", "90001"),
    "DE - 德国": ("DE", "EUR", "Berlin", "Hauptstraße 1", "10115"),
    "JP - 日本": ("JP", "JPY", "Tokyo", "1-1-1 Shibuya", "150-0002"),
    "GB - 英国": ("GB", "GBP", "London", "10 Downing St", "SW1A 2AA"),
    "FR - 法国": ("FR", "EUR", "Paris", "1 Rue de Rivoli", "75001"),
    "SG - 新加坡": ("SG", "SGD", "Singapore", "1 Raffles Place", "048616"),
    "HK - 香港": ("HK", "HKD", "Hong Kong", "1 Queen's Road", "000000"),
    "KR - 韩国": ("KR", "KRW", "Seoul", "1 Gangnam-daero", "06000"),
    "AU - 澳大利亚": ("AU", "AUD", "NSW", "1 George St", "2000"),
    "CA - 加拿大": ("CA", "CAD", "Ontario", "123 King St", "M5H 1A1"),
    "NL - 荷兰": ("NL", "EUR", "Amsterdam", "Damrak 1", "1012 LG"),
    "IT - 意大利": ("IT", "EUR", "Rome", "Via Roma 1", "00100"),
    "ES - 西班牙": ("ES", "EUR", "Madrid", "Calle Mayor 1", "28013"),
    "CH - 瑞士": ("CH", "CHF", "Zurich", "Bahnhofstrasse 1", "8001"),
}

st.set_page_config(page_title="Let's ABC", page_icon="A", layout="wide")

# ── CSS ──
st.markdown("""
<style>
    .block-container { max-width: 1100px; padding-top: 1.5rem; }
    /* 更精细的排版 */
    .stRadio > label { font-weight: 500; letter-spacing: 0.02em; }
    .stRadio [data-baseweb="radio"] { gap: 0.3rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 0; border-bottom: 1px solid rgba(255,255,255,0.08); }
    .stTabs [data-baseweb="tab"] {
        padding: 0.6rem 1.5rem; font-weight: 500; letter-spacing: 0.05em;
        border-bottom: 2px solid transparent; transition: all 0.2s;
    }
    .stTabs [aria-selected="true"] { border-bottom-color: #7c3aed; }
    /* 进度条渐变 */
    .stProgress > div > div > div { background: linear-gradient(90deg, #7c3aed, #3b82f6); }
    /* 按键圆角 */
    .stButton > button { border-radius: 8px; font-weight: 500; letter-spacing: 0.03em; transition: all 0.15s; }
    .stButton > button[kind="primary"] { background: linear-gradient(135deg, #7c3aed, #6d28d9); border: none; }
    .stButton > button[kind="primary"]:hover { background: linear-gradient(135deg, #6d28d9, #5b21b6); }
    /* 输入框 */
    .stTextInput > div > div > input { border-radius: 6px; }
    /* Expander 样式 */
    .streamlit-expanderHeader { font-weight: 500; letter-spacing: 0.02em; }
    /* 分割线淡化 */
    hr { opacity: 0.15; }
</style>
""", unsafe_allow_html=True)

# 后台日志缓存 — 使用 cache_resource 确保跨 rerun 同一对象
@st.cache_resource
def _get_log_shared():
    return {"cache": deque(maxlen=5000), "lock": threading.Lock()}

_log_shared = _get_log_shared()


# ── 日志 ──
class LogCapture(logging.Handler):
    def __init__(self, shared):
        super().__init__()
        self._cache = shared["cache"]
        self._lock = shared["lock"]
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        with self._lock:
            self._cache.append(msg)


def pull_captured_logs():
    """将后台日志搬运到 session_state，需在主线程调用。"""
    if "log_buffer" not in st.session_state:
        st.session_state.log_buffer = []
    cache = _log_shared["cache"]
    lock = _log_shared["lock"]
    with lock:
        if not cache:
            return
        st.session_state.log_buffer.extend(list(cache))
        cache.clear()


def clear_captured_logs():
    cache = _log_shared["cache"]
    lock = _log_shared["lock"]
    with lock:
        cache.clear()


def init_logging():
    handler = LogCapture(_log_shared)
    handler.setLevel(logging.INFO)
    handler._is_log_capture = True
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [h for h in root.handlers if not getattr(h, '_is_log_capture', False)]
    root.addHandler(handler)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


for k, v in {"log_buffer": [], "running": False, "result": None}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# 每次 rerun 先同步一次日志缓存
pull_captured_logs()

# ── widget 默认值初始化 (只在首次运行时设置) ──
_widget_defaults = {
    "w_exp_month": "12",
    "w_exp_year": "2030",
    "w_proxy": "",
    "w_billing_name": "",
}
for _dk, _dv in _widget_defaults.items():
    if _dk not in st.session_state:
        st.session_state[_dk] = _dv

# ── 延迟的解析结果应用 (必须在 widget 渲染之前) ──
_parse_just_applied = False
if "_pending_parse" in st.session_state:
    _pp = st.session_state.pop("_pending_parse")
    for _pk, _pv in _pp.items():
        st.session_state[_pk] = _pv
    _parse_just_applied = True


# ════════════════════════════════════════
# 顶部
# ════════════════════════════════════════
st.markdown(
    '<h1 style="text-align:center;letter-spacing:3px;">'
    "Let's "
    '<span style="font-family:\'Courier New\',monospace;font-weight:900;'
    'background:linear-gradient(135deg,#7c3aed,#3b82f6);-webkit-background-clip:text;'
    '-webkit-text-fill-color:transparent;font-size:1.15em;">ABC</span>'
    ' <span style="font-size:0.5em;opacity:0.6;vertical-align:middle;">(Auto BindCard)</span>'
    '</h1>',
    unsafe_allow_html=True,
)

# ── 开发者模式: 启动时通过 -- --dev 参数开启 ──
# 用法: streamlit run ui.py -- --dev
dev_mode = "--dev" in sys.argv

# ═══════════════════════════════════════
# 兑换码验证门禁
# ═══════════════════════════════════════
if "verified_code" not in st.session_state:
    st.session_state.verified_code = ""

if not st.session_state.verified_code:
    st.markdown(
        '<div style="text-align:center;margin:40px 0 20px;opacity:0.7">输入兑换码开始使用</div>',
        unsafe_allow_html=True,
    )
    _code_col1, _code_col2 = st.columns([3, 1])
    with _code_col1:
        _input_code = st.text_input("兑换码", placeholder="XXXX-XXXX-XXXX", label_visibility="collapsed")
    with _code_col2:
        _verify_btn = st.button("验证", type="primary", use_container_width=True)
    if _verify_btn and _input_code:
        _valid, _msg = validate_code(_input_code.strip())
        if _valid:
            st.session_state.verified_code = _input_code.strip()
            st.rerun()
        else:
            st.error(_msg)
    st.stop()

# ── 已验证: 显示兑换码状态 ──
_code_info = get_code_info(st.session_state.verified_code)
if _code_info:
    _remaining = _code_info["total_uses"] - _code_info["used_count"]
    _status_col1, _status_col2 = st.columns([5, 1])
    with _status_col1:
        st.caption(f"兑换码: `{st.session_state.verified_code}` — 剩余 {_remaining}/{_code_info['total_uses']} 次")
    with _status_col2:
        if st.button("退出", key="logout_code"):
            st.session_state.verified_code = ""
            st.rerun()

# ── 账号来源选择 ──
# 从数据库获取当前兑换码的有 token 的执行记录 (用于「选择已有账号」)
_code_history = get_code_history(st.session_state.verified_code)
_code_success_creds = []
for _h in _code_history:
    if _h.get("result_json"):
        try:
            _rd = json.loads(_h["result_json"])
            if _rd.get("email") and _rd.get("access_token"):
                _code_success_creds.append(_rd)
        except Exception:
            pass

acct_col, proxy_col = st.columns([3, 2])
with acct_col:
    account_source = st.radio(
        "账号来源",
        ["新注册", "选择已有账号", "手动输入 Token"],
        index=1 if _code_success_creds else 0,
        horizontal=True,
    )
    do_register = account_source == "新注册"

do_checkout = True
do_payment = True

if dev_mode:
    with proxy_col:
        sc1, sc2 = st.columns(2)
        do_checkout = sc1.checkbox("创建 Checkout", value=True)
        do_payment = sc2.checkbox("提交支付", value=True)

with proxy_col:
    proxy = st.text_input("代理", placeholder="http://127.0.0.1:7897", key="w_proxy")

# ── 已有账号选择 / Token 输入 ──
cred_email = ""
cred_session_token = ""
cred_access_token = ""
cred_device_id = ""
use_existing_creds = not do_register

if account_source == "选择已有账号":
    if _code_success_creds:
        _cred_options = {}
        for _cd in _code_success_creds:
            _label = f"{_cd.get('email', '未知')}"
            _cred_options[_label] = _cd
        if _cred_options:
            sel_label = st.selectbox("选择账号", list(_cred_options.keys()), key="w_acct_select")
            _sel_data = _cred_options[sel_label]
            cred_email = _sel_data.get("email", "")
            cred_session_token = _sel_data.get("session_token", "")
            cred_access_token = _sel_data.get("access_token", "")
            cred_device_id = _sel_data.get("device_id", "")
            with st.expander("查看凭证详情", expanded=False):
                st.json({k: (v[:40] + "..." if isinstance(v, str) and len(v) > 50 else v) for k, v in _sel_data.items()})
        else:
            st.warning("未找到有效的凭证")
    else:
        st.warning("暂无已注册的账号，请先选择「新注册」")

elif account_source == "手动输入 Token":
    cred_access_token = st.text_input("access_token", placeholder="eyJhbGciOi...", type="password", key="w_manual_at")
    cred_email = st.text_input("邮箱 (可选)", placeholder="user@example.com", key="w_manual_email")

# ── 注册模式下显示邮箱配置 ──
if do_register:
    with st.expander("邮箱配置", expanded=True):
        _mc1, _mc2, _mc3 = st.columns(3)
        mail_worker = _mc1.text_input("Worker API", placeholder="https://mail-api.example.com", key="w_mail_worker_reg")
        mail_domain = _mc2.text_input("邮箱域名", placeholder="example.com", key="w_mail_domain_reg")
        mail_token = _mc3.text_input("密码", placeholder="your-mail-token", type="password", key="w_mail_token_reg")


# 默认值 (非开发者模式下不显示这些设置)
use_browser_mode = True
captcha_key = ""
captcha_api_url = ""
if not do_register:
    # 非注册模式时，邮箱配置使用空默认值 (开发者模式下有单独的输入框)
    mail_worker = ""
    mail_domain = ""
    mail_token = ""
# 计划类型选择 (始终可见)
plan_type_label = st.radio(
    "选择计划",
    ["Business · 团队版免费试用 1 个月", "Plus · 个人版免费试用 1 个月"],
    index=0,
    horizontal=True,
)
plan_type = "plus" if "Plus" in plan_type_label else "team"
if plan_type == "plus":
    workspace_name = ""
    seat_quantity = 0
    promo_campaign = "plus-1-month-free"
else:
    workspace_name = "MyWorkspace"
    seat_quantity = 5
    promo_campaign = "team-1-month-free"

if dev_mode:
    with st.expander("高级设置", expanded=False):
        adv_col1, adv_col2 = st.columns(2)
        with adv_col1:
            payment_mode = st.radio(
                "支付模式",
                ["浏览器模式 (推荐)", "API 模式"],
                index=0,
                horizontal=True,
            )
            use_browser_mode = payment_mode.startswith("浏览")
        with adv_col2:
            if use_browser_mode:
                import subprocess as _sp
                _xvfb_running = False
                try:
                    _xvfb_pids = _sp.check_output(["pgrep", "-f", "Xvfb :99"], stderr=_sp.DEVNULL).decode().strip()
                    _xvfb_running = bool(_xvfb_pids)
                except Exception:
                    pass
                if _xvfb_running:
                    st.success("Xvfb 运行中 (:99)")
                else:
                    st.info("将自动启动 Xvfb :99")
            else:
                st.info("API 模式")

        if not use_browser_mode:
            captcha_col1, captcha_col2 = st.columns([3, 1])
            with captcha_col1:
                captcha_key = st.text_input("YesCaptcha API Key", placeholder="your-yescaptcha-key", type="password")
            with captcha_col2:
                captcha_api_url = st.text_input("打码 API", value="https://api.yescaptcha.com")

        st.markdown("---")
        st.markdown("**邮箱 & 计划设置**")
        if not do_register:
            mail_worker = st.text_input("邮箱 Worker", placeholder="https://mail-api.example.com", key="w_mail_worker_dev")
            adv_mc1, adv_mc2 = st.columns(2)
            mail_domain = adv_mc1.text_input("邮箱域名", placeholder="example.com", key="w_mail_domain_dev")
            mail_token = adv_mc2.text_input("密码", placeholder="your-mail-token", type="password", key="w_mail_token_dev")
        if plan_type == "team":
            adv_tc1, adv_tc2, adv_tc3 = st.columns(3)
            workspace_name = adv_tc1.text_input("Workspace", value="MyWorkspace")
            seat_quantity = adv_tc2.number_input("席位数", min_value=2, max_value=50, value=5)
            promo_campaign = adv_tc3.text_input("活动 ID", value="team-1-month-free")
        else:
            promo_campaign = st.text_input("活动 ID", value="plus-1-month-free")

st.divider()

# ════════════════════════════════════════
# 配置区: 卡片信息优先
# ════════════════════════════════════════

if do_payment:
    with st.expander("粘贴卡片信息", expanded=True):
        paste_text = st.text_area(
            "粘贴卡片/账单文本",
            height=120,
            placeholder="支持两种格式:\n\n格式1 (键值对):\n卡号: 4242424242424242\n有效期: 1230\nCVV: 123\n姓名: John Smith\n地址: 123 Main Street\n城市: San Francisco\n州: CA\n邮编: 94102\n国家: United States\n\n格式2 (纯文本):\n4242 4242 4242 4242\n12/30\nCVV 123",
            key="paste_card_text",
        )
        if st.button("识别并填充", key="parse_btn", disabled=not paste_text):
            parsed = _parse_card_text(paste_text)
            pending = {}
            if parsed.get("card_number"):
                pending["w_card_number"] = parsed["card_number"]
            if parsed.get("exp_month"):
                pending["w_exp_month"] = parsed["exp_month"]
            if parsed.get("exp_year"):
                pending["w_exp_year"] = parsed["exp_year"]
            if parsed.get("cvv"):
                pending["w_card_cvc"] = parsed["cvv"]
            if parsed.get("address_line1"):
                pending["w_address_line1"] = parsed["address_line1"]
            if parsed.get("address_city"):
                pending["w_address_city"] = parsed["address_city"]
            if parsed.get("address_state"):
                pending["w_address_state"] = parsed["address_state"]
            if parsed.get("postal_code"):
                pending["w_postal_code"] = parsed["postal_code"]
            if parsed.get("country_code"):
                cc = parsed["country_code"]
                for i, label in enumerate(COUNTRY_MAP.keys()):
                    if label.startswith(cc):
                        pending["w_country"] = label
                        break
            if parsed.get("currency"):
                pending["w_currency"] = parsed["currency"]
            if parsed.get("billing_name"):
                pending["w_billing_name"] = parsed["billing_name"]
            st.session_state["_pending_parse"] = pending
            filled = []
            if parsed.get("card_number"):
                filled.append(f"卡号: {parsed['card_number'][:4]}****{parsed['card_number'][-4:]}")
            if parsed.get("exp_month"):
                filled.append(f"有效期: {parsed['exp_month']}/{parsed['exp_year']}")
            if parsed.get("cvv"):
                filled.append(f"CVV: ***")
            if parsed.get("raw_address"):
                filled.append(f"地址: {parsed['raw_address']}")
            if parsed.get("billing_name"):
                filled.append(f"姓名: {parsed['billing_name']}")
            if filled:
                st.success("已识别: " + " | ".join(filled))
            else:
                st.warning("未能识别卡片信息，请检查文本格式")
            st.rerun()

cfg_col1, cfg_col2 = st.columns(2)

with cfg_col1:
    if do_payment:
        with st.expander("信用卡", expanded=True):
            TEST_CARDS = {
                "4242 4242 4242 4242 (Visa 标准)": ("4242424242424242", "123"),
                "4000 0000 0000 0002 (Visa 被拒)": ("4000000000000002", "123"),
                "4000 0000 0000 0069 (Visa 过期)": ("4000000000000069", "123"),
                "4000 0000 0000 9995 (Visa 余额不足)": ("4000000000009995", "123"),
                "5555 5555 5555 4444 (Mastercard)": ("5555555555554444", "123"),
                "5200 8282 8282 8210 (MC Debit)": ("5200828282828210", "123"),
                "2223 0031 2200 3222 (MC 2系列)": ("2223003122003222", "123"),
                "3782 822463 10005 (Amex)": ("378282246310005", "1234"),
            }
            tc_sel = st.selectbox("快速填充测试卡", ["不填充"] + list(TEST_CARDS.keys()), key="tc_sel")
            if tc_sel != "不填充":
                tc_num, tc_cvc = TEST_CARDS[tc_sel]
                st.session_state["w_card_number"] = tc_num
                st.session_state["w_card_cvc"] = tc_cvc

            cc1, cc2, cc3, cc4 = st.columns([5, 2, 2, 2])
            card_number = cc1.text_input("卡号", placeholder="真实卡号", key="w_card_number")
            exp_month = cc2.text_input("月", key="w_exp_month")
            exp_year = cc3.text_input("年", key="w_exp_year")
            card_cvc = cc4.text_input("CVC", key="w_card_cvc")

            if card_number and card_number.startswith("4"):
                st.caption("Live 模式下所有测试卡都会被拒绝，仅用于验证流程")
    else:
        card_number = exp_month = exp_year = card_cvc = ""

with cfg_col2:
    with st.expander("账单地址", expanded=True):
        # 如果有解析出的国家，自动选择对应国家
        country_label = st.selectbox("国家", list(COUNTRY_MAP.keys()), key="w_country")
        country_code, default_currency, default_state, default_addr, default_zip = COUNTRY_MAP[country_label]
        # 当国家变更时，更新地址默认值 (但不覆盖刚解析的值)
        _prev_country = st.session_state.get("_prev_country", "")
        if _prev_country and _prev_country != country_label and not _parse_just_applied:
            st.session_state["w_currency"] = default_currency
            st.session_state["w_address_line1"] = default_addr
            st.session_state["w_address_state"] = default_state
            st.session_state["w_postal_code"] = default_zip
        st.session_state["_prev_country"] = country_label
        bc1, bc2 = st.columns(2)
        billing_name = bc1.text_input("姓名", key="w_billing_name")
        if "w_currency" not in st.session_state:
            st.session_state["w_currency"] = default_currency
        currency = bc2.text_input("货币", key="w_currency")
        bc3, bc4, bc5, bc6 = st.columns(4)
        if "w_address_line1" not in st.session_state:
            st.session_state["w_address_line1"] = default_addr
        if "w_address_city" not in st.session_state:
            st.session_state["w_address_city"] = ""
        if "w_address_state" not in st.session_state:
            st.session_state["w_address_state"] = default_state
        if "w_postal_code" not in st.session_state:
            st.session_state["w_postal_code"] = default_zip
        address_line1 = bc3.text_input("地址", key="w_address_line1")
        address_city = bc4.text_input("城市", key="w_address_city")
        address_state = bc5.text_input("州/省", key="w_address_state")
        postal_code = bc6.text_input("邮编", key="w_postal_code")

st.divider()

# ════════════════════════════════════════
# Tab
# ════════════════════════════════════════
steps_list = []
if do_register: steps_list.append("注册")
if do_checkout: steps_list.append("Checkout")
if do_payment: steps_list.append("支付")

tab_run, tab_accounts, tab_history = st.tabs(["执行", "账号", "历史"])

# 日志关键词 → 进度百分比映射
_PROGRESS_KEYWORDS = [
    ("使用已有凭证", 5),
    ("邮箱创建成功", 3),
    ("注册完成", 10),
    ("创建 Checkout Session", 12),
    ("Checkout 创建成功", 18),
    ("启动 Chrome", 22),
    ("Chrome ready", 28),
    ("通过 Cloudflare", 32),
    ("Cloudflare 已通过", 38),
    ("加载 checkout 页面", 42),
    ("Stripe Payment Element", 48),
    ("Stripe Element 已加载", 55),
    ("填写卡片信息", 60),
    ("已输入卡号", 65),
    ("已输入 CVC", 70),
    ("填写账单地址", 73),
    ("地址-邮编", 78),
    ("已点击提交按钮", 82),
    ("等待支付处理", 85),
    ("hCaptcha", 88),
    ("checkbox 已点击", 92),
    ("支付成功", 98),
    ("支付被拒", 98),
    ("支付失败", 98),
]

def _calc_progress_pct():
    """根据 session_state.log_buffer (累积) 计算当前进度百分比"""
    pull_captured_logs()  # 先把 _LOG_CACHE 搬运到 log_buffer
    logs = st.session_state.get("log_buffer", [])
    if not logs:
        return 1
    text = "\n".join(logs[-30:])
    best = 1
    for keyword, pct in _PROGRESS_KEYWORDS:
        if keyword in text and pct > best:
            best = pct
    return best


def _run_flow_thread(rd, cs):
    """在后台线程中执行完整流程 (cs = config_snapshot)"""
    try:
        cfg = Config()
        cfg.proxy = cs["proxy"]
        cfg.mail.email_domain = cs["mail_domain"]
        cfg.mail.worker_domain = cs["mail_worker"]
        cfg.mail.admin_token = cs["mail_token"]
        cfg.team_plan.workspace_name = cs["workspace_name"]
        cfg.team_plan.seat_quantity = cs["seat_quantity"]
        cfg.team_plan.promo_campaign_id = cs["promo_campaign"]
        cfg.captcha = CaptchaConfig(api_url=cs["captcha_api_url"], client_key=cs["captcha_key"])
        cfg.billing = BillingInfo(
            name=cs["billing_name"], email="",
            country=cs["country_code"], currency=cs["currency"],
            address_line1=cs["address_line1"], address_state=cs["address_state"],
            postal_code=cs["postal_code"])
        if cs["do_payment"]:
            cfg.card = CardInfo(number=cs["card_number"], cvc=cs["card_cvc"],
                                exp_month=cs["exp_month"], exp_year=cs["exp_year"])

        store = ResultStore(output_dir=OUTPUT_DIR)
        auth_result = None
        af = None

        if cs["do_register"]:
            mp = MailProvider(worker_domain=cfg.mail.worker_domain, admin_token=cfg.mail.admin_token, email_domain=cfg.mail.email_domain)
            af = AuthFlow(cfg)
            auth_result = af.run_register(mp)
            rd["email"] = auth_result.email
            rd["session_token"] = auth_result.session_token
            rd["access_token"] = auth_result.access_token
            rd["device_id"] = auth_result.device_id
            store.save_credentials(auth_result.to_dict())
            store.append_credentials_csv(auth_result.to_dict())
        elif cs["use_existing_creds"] and cs["do_checkout"]:
            if not cs["cred_access_token"]:
                raise RuntimeError("必须提供 access_token")
            if not cs["cred_session_token"]:
                raise RuntimeError("必须提供 session_token")
            af = AuthFlow(cfg)
            auth_result = af.from_existing_credentials(
                session_token=cs["cred_session_token"],
                access_token=cs["cred_access_token"],
                device_id=cs["cred_device_id"],
            )
            auth_result.email = cs["cred_email"] or "unknown@example.com"
            rd["email"] = auth_result.email

        if cs["do_checkout"]:
            if not auth_result:
                raise RuntimeError("需先注册或提供凭证")

            if cs["use_browser_mode"] and cs["do_payment"]:
                import subprocess as _sp
                _xvfb_ok = False
                try:
                    _sp.check_output(["pgrep", "-f", "Xvfb :99"], stderr=_sp.DEVNULL)
                    _xvfb_ok = True
                except Exception:
                    pass
                if not _xvfb_ok:
                    _sp.Popen(["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac"],
                              stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    import time as _t; _t.sleep(1)
                os.environ["DISPLAY"] = ":99"

                from browser_payment import BrowserPayment
                bp = BrowserPayment(proxy=cfg.proxy, headless=False, slow_mo=80)
                br = bp.run_full_flow(
                    session_token=auth_result.session_token,
                    access_token=auth_result.access_token,
                    device_id=auth_result.device_id,
                    card_number=cs["card_number"], card_exp_month=cs["exp_month"],
                    card_exp_year=cs["exp_year"], card_cvc=cs["card_cvc"],
                    billing_name=cs["billing_name"], billing_country=cs["country_code"],
                    billing_zip=cs["postal_code"], billing_line1=cs["address_line1"],
                    billing_city=cs["address_city"], billing_state=cs["address_state"],
                    billing_email=auth_result.email, billing_currency=cs["currency"],
                    chatgpt_proxy=cfg.proxy, timeout=120,
                    plan_type=cs["plan_type"],
                )
                rd["checkout_data"] = br.get("checkout_data")
                rd["checkout_session_id"] = br.get("checkout_data", {}).get("checkout_session_id", "")
                rd["success"] = br.get("success", False)
                rd["error"] = br.get("error", "")
                rd["confirm_response"] = br

            else:
                cfg.billing.email = auth_result.email
                pf = PaymentFlow(cfg, auth_result)
                if af: pf.session = af.session
                cs_id = pf.create_checkout_session()
                rd["checkout_session_id"] = cs_id
                rd["checkout_data"] = pf.checkout_data
                pf.fetch_stripe_fingerprint()
                pf.extract_stripe_pk(pf.checkout_url)
                if cs["do_payment"]:
                    pf.payment_method_id = pf.create_payment_method()
                    pf.fetch_payment_page_details(cs_id)
                    pay = pf.confirm_payment(cs_id)
                    rd["confirm_status"] = pay.confirm_status
                    rd["confirm_response"] = pay.confirm_response
                    rd["success"] = pay.success
                    rd["error"] = pay.error
                else:
                    rd["success"] = True
        elif cs["do_register"]:
            rd["success"] = True

    except Exception as e:
        rd["error"] = str(e)
        logging.getLogger("ui").error(f"EXCEPTION: {traceback.format_exc()}")
    finally:
        rd["_done"] = True

    try:
        store = ResultStore(output_dir=OUTPUT_DIR)
        store.save_result(rd, "ui_run")
        if rd.get("email"):
            store.append_history(email=rd["email"], status="ui_run",
                                 checkout_session_id=rd.get("checkout_session_id", ""),
                                 payment_status=rd.get("confirm_status", ""),
                                 error=rd.get("error", ""))
    except Exception:
        pass


with tab_run:
    # 额度提示
    if do_register:
        st.info("新注册模式: 成功消耗 **2** 次额度，失败消耗 **1** 次")
    else:
        st.info("已有账号模式: 消耗 **1** 次额度")
    btn_col1, btn_col2 = st.columns([4, 1])
    with btn_col1:
        run_btn = st.button("开始执行", disabled=st.session_state.running or not steps_list,
                            type="primary", use_container_width=True)
    with btn_col2:
        stop_btn = st.button("终止", disabled=not st.session_state.running, use_container_width=True)

    # ── 点击开始: 表单验证 → 验证兑换码 → 预留额度 → 启动线程 ──
    if run_btn and not st.session_state.running:
        # 表单验证
        _errors = []
        if do_register:
            if not mail_worker or not mail_worker.startswith("http"):
                _errors.append("请填写邮箱 Worker API 地址")
            if not mail_domain:
                _errors.append("请填写邮箱域名")
            if not mail_token:
                _errors.append("请填写密码")
        elif use_existing_creds and do_checkout:
            if not cred_access_token:
                _errors.append("请提供 access_token")
        if do_payment:
            if not card_number:
                _errors.append("请填写卡号")
            if not card_cvc:
                _errors.append("请填写 CVC")
        if _errors:
            for _e in _errors:
                st.error(_e)
            st.stop()

        # 再次验证兑换码
        _v, _vm = validate_code(st.session_state.verified_code)
        if not _v:
            st.error(f"兑换码不可用: {_vm}")
            st.stop()

        # 预留使用额度 (新注册=2, 其他=1)
        _reserve_amount = 2 if do_register else 1
        _exec_id = reserve_use(st.session_state.verified_code, plan_type=plan_type, amount=_reserve_amount)
        if _exec_id is None:
            st.error("兑换码额度不足")
            st.stop()

        st.session_state._execution_id = _exec_id
        update_execution(_exec_id, status="running")

        st.session_state._flow_config = {
            "proxy": proxy or None,
            "mail_domain": mail_domain, "mail_worker": mail_worker, "mail_token": mail_token,
            "workspace_name": workspace_name, "seat_quantity": seat_quantity, "promo_campaign": promo_campaign,
            "plan_type": plan_type,
            "captcha_api_url": captcha_api_url, "captcha_key": captcha_key,
            "billing_name": billing_name, "country_code": country_code, "currency": currency,
            "address_line1": address_line1, "address_city": address_city,
            "address_state": address_state, "postal_code": postal_code,
            "card_number": card_number if do_payment else "",
            "card_cvc": card_cvc if do_payment else "",
            "exp_month": exp_month if do_payment else "",
            "exp_year": exp_year if do_payment else "",
            "do_register": do_register, "do_checkout": do_checkout, "do_payment": do_payment,
            "use_existing_creds": use_existing_creds, "use_browser_mode": use_browser_mode,
            "cred_session_token": cred_session_token, "cred_access_token": cred_access_token,
            "cred_device_id": cred_device_id, "cred_email": cred_email,
        }
        st.session_state._flow_result = {"success": False, "error": "", "email": "", "steps": {}}
        st.session_state.running = True
        st.session_state.log_buffer = []
        st.session_state.result = None
        clear_captured_logs()
        init_logging()
        _t = threading.Thread(
            target=_run_flow_thread,
            args=(st.session_state._flow_result, st.session_state._flow_config),
            daemon=True,
        )
        _t.start()
        st.rerun()

    # ── 点击终止 ──
    if stop_btn and st.session_state.running:
        import subprocess as _sp
        try:
            _sp.run(["pkill", "-f", "remote-debugging-port"], capture_output=True)
        except Exception:
            pass
        st.session_state.running = False
        st.session_state.result = {"success": False, "error": "用户手动终止", "email": ""}
        # 终止不扣额度
        _eid = st.session_state.get("_execution_id")
        if _eid:
            complete_use(_eid, success=False, error_msg="用户手动终止")
            st.session_state._execution_id = None
        st.warning("已终止执行")
        st.rerun()

    # ── 运行中: 显示进度 ──
    if st.session_state.running:
        pct = _calc_progress_pct()
        st.progress(pct / 100.0)
        st.markdown(
            f'<div style="text-align:center;font-size:28px;font-weight:bold;margin:-15px 0 10px">{pct}%</div>',
            unsafe_allow_html=True,
        )
        rd = st.session_state.get("_flow_result", {})
        if rd.get("_done"):
            st.session_state.running = False
            st.session_state.result = rd
            # ── 完成兑换码计次 ──
            _eid = st.session_state.get("_execution_id")
            if _eid:
                complete_use(
                    _eid,
                    success=rd.get("success", False),
                    email=rd.get("email", ""),
                    error_msg=rd.get("error", ""),
                    result_json=json.dumps(rd, ensure_ascii=False, default=str),
                )
                st.session_state._execution_id = None
            st.rerun()
        else:
            import time as _time
            _time.sleep(1)
            st.rerun()

    # ── 显示结果 ──
    if st.session_state.result and not st.session_state.running:
        r = st.session_state.result
        if r.get("success"):
            st.progress(1.0)
            st.success(f"全部完成 — {r.get('email', '')}")
        elif r.get("error"):
            st.error(f"{r.get('error', '')}")

        if dev_mode:
            st.divider()
            cols = st.columns(4)
            cols[0].metric("邮箱", r.get("email") or "-")
            cols[1].metric("Checkout", (r.get("checkout_session_id", "")[:20] + "...") if r.get("checkout_session_id") else "-")
            cols[2].metric("Confirm", r.get("confirm_status") or "-")
            cols[3].metric("状态", "成功" if r.get("success") else "失败")
            if r.get("confirm_response"):
                with st.expander("Stripe 原始响应", expanded=False):
                    st.json(r["confirm_response"])
            pull_captured_logs()
            if st.session_state.log_buffer:
                with st.expander("日志", expanded=False):
                    st.code("\n".join(st.session_state.log_buffer[-200:]), language="log")


# Tab: 账号
# ════════════════════════════════════════
with tab_accounts:
    _history = get_code_history(st.session_state.verified_code)
    # 显示所有有邮箱的账号 (注册成功的, 不管支付是否成功)
    _acct_rows = []
    for r in _history:
        if r.get("result_json"):
            try:
                rd = json.loads(r["result_json"])
                if rd.get("email"):
                    _acct_rows.append({
                        "exec_id": r["id"],
                        "email": rd["email"],
                        "plan_type": r.get("plan_type") or "-",
                        "status": r["status"],
                        "created_at": r["created_at"][:19],
                        "has_token": bool(rd.get("access_token")),
                        "_data": rd,
                    })
            except Exception:
                pass

    if _acct_rows:
        import pandas as pd
        _disp_rows = []
        for a in _acct_rows:
            _disp_rows.append({
                "邮箱": a["email"],
                "计划": a["plan_type"],
                "支付": "✅ 成功" if a["status"] == "success" else "❌ 失败",
                "时间": a["created_at"],
            })
        st.dataframe(pd.DataFrame(_disp_rows), hide_index=True, use_container_width=True)
        st.caption(f"共 {len(_acct_rows)} 个账号")

        st.divider()
        for idx, acct in enumerate(_acct_rows):
            _data = acct["_data"]
            with st.expander(f"{acct['email']}  {'✅' if acct['status'] == 'success' else '❌'}", expanded=False):
                if _data.get("access_token"):
                    st.code(
                        f"access_token: {_data.get('access_token', 'N/A')}\n"
                        f"session_token: {_data.get('session_token', 'N/A')}\n"
                        f"device_id: {_data.get('device_id', 'N/A')}",
                        language="yaml",
                    )
                else:
                    st.caption("无 Token 信息")
    else:
        st.info("暂无已注册的账号。执行完成后自动显示。")

    if st.button("刷新", key="ref_acc"):
        st.rerun()


# ════════════════════════════════════════
# Tab: 历史
# ════════════════════════════════════════
with tab_history:
    _history = get_code_history(st.session_state.verified_code)
    if _history:
        import pandas as pd
        _disp = []
        for r in _history:
            _disp.append({
                "状态": {"success": "✅ 成功", "failed": "❌ 失败", "running": "🔄 运行中", "pending": "⏳ 等待"}.get(r["status"], r["status"]),
                "邮箱": r.get("email") or "-",
                "计划": r.get("plan_type") or "-",
                "错误": r.get("error_msg") or "",
                "时间": r["created_at"][:19],
            })
        st.dataframe(pd.DataFrame(_disp), hide_index=True, use_container_width=True)
        st.caption(f"共 {len(_history)} 条记录")
    else:
        st.info("暂无执行历史")

    if st.button("刷新", key="ref_hist"):
        st.rerun()
