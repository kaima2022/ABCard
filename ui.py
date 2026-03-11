"""
自动化绑卡支付 - Streamlit UI
运行: streamlit run ui.py --server.address 0.0.0.0 --server.port 8503
"""
import json
import logging
import os
import sys
import traceback

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config, CardInfo, BillingInfo
from mail_provider import MailProvider
from auth_flow import AuthFlow, AuthResult
from payment_flow import PaymentFlow
from logger import ResultStore

OUTPUT_DIR = "test_outputs"

# 国家 → (code, currency, state, address)
COUNTRY_MAP = {
    "US - 美国": ("US", "USD", "California", "123 Main St"),
    "DE - 德国": ("DE", "EUR", "Berlin", "Hauptstraße 1"),
    "JP - 日本": ("JP", "JPY", "Tokyo", "1-1-1 Shibuya"),
    "GB - 英国": ("GB", "GBP", "London", "10 Downing St"),
    "FR - 法国": ("FR", "EUR", "Paris", "1 Rue de Rivoli"),
    "SG - 新加坡": ("SG", "SGD", "Singapore", "1 Raffles Place"),
    "HK - 香港": ("HK", "HKD", "Hong Kong", "1 Queen's Road"),
    "KR - 韩国": ("KR", "KRW", "Seoul", "1 Gangnam-daero"),
    "AU - 澳大利亚": ("AU", "AUD", "NSW", "1 George St"),
    "CA - 加拿大": ("CA", "CAD", "Ontario", "123 King St"),
    "NL - 荷兰": ("NL", "EUR", "Amsterdam", "Damrak 1"),
    "IT - 意大利": ("IT", "EUR", "Rome", "Via Roma 1"),
    "ES - 西班牙": ("ES", "EUR", "Madrid", "Calle Mayor 1"),
    "CH - 瑞士": ("CH", "CHF", "Zurich", "Bahnhofstrasse 1"),
}

st.set_page_config(page_title="Auto BindCard", page_icon="💳", layout="wide")

# ── CSS: 流水线 + 闪烁动画 ──
st.markdown("""
<style>
    .block-container { max-width: 1100px; padding-top: 1.5rem; }

    @keyframes blink {
        0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(243,156,18,0.6); }
        50% { opacity: 0.5; box-shadow: 0 0 20px rgba(243,156,18,0.9); }
    }

    .pipeline-wrap { display: flex; align-items: center; gap: 0; margin: 16px 0; }

    .pipeline-node {
        text-align: center; padding: 10px 14px; border-radius: 10px; min-width: 90px;
        border: 2px solid #555; background: rgba(30,30,30,0.6); flex-shrink: 0;
    }
    .pipeline-node .icon { font-size: 22px; }
    .pipeline-node .label { font-size: 12px; font-weight: 600; margin-top: 3px; }

    .pipeline-node.done   { border-color: #2ecc71; }
    .pipeline-node.done .label { color: #2ecc71; }
    .pipeline-node.running { border-color: #f39c12; animation: blink 1s ease-in-out infinite; }
    .pipeline-node.running .label { color: #f39c12; }
    .pipeline-node.error  { border-color: #e74c3c; }
    .pipeline-node.error .label { color: #e74c3c; }
    .pipeline-node.pending { border-color: #555; }
    .pipeline-node.pending .label { color: #888; }

    .pipeline-line {
        height: 2px; flex: 1; min-width: 20px;
        background: linear-gradient(90deg, #555, #555);
    }
    .pipeline-line.done { background: linear-gradient(90deg, #2ecc71, #2ecc71); }
    .pipeline-line.active { background: linear-gradient(90deg, #2ecc71, #f39c12); }
</style>
""", unsafe_allow_html=True)

ICONS = {"done": "✅", "running": "⏳", "error": "❌", "pending": "⬜"}


def render_pipeline_html(nodes):
    """渲染连线式流水线"""
    parts = []
    prev_done = False
    for i, (name, status) in enumerate(nodes):
        if i > 0:
            if prev_done and status == "running":
                line_cls = "active"
            elif prev_done:
                line_cls = "done"
            else:
                line_cls = ""
            parts.append(f'<div class="pipeline-line {line_cls}"></div>')
        parts.append(
            f'<div class="pipeline-node {status}">'
            f'<div class="icon">{ICONS.get(status, "⬜")}</div>'
            f'<div class="label">{name}</div>'
            f'</div>'
        )
        prev_done = status == "done"
    return f'<div class="pipeline-wrap">{"".join(parts)}</div>'


# ── 日志 ──
class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        if "log_buffer" in st.session_state:
            st.session_state.log_buffer.append(self.format(record))


def init_logging():
    handler = LogCapture()
    handler.setLevel(logging.DEBUG)
    handler._is_log_capture = True  # 标记
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 按标记移除旧的 LogCapture (class 名/属性)
    root.handlers = [h for h in root.handlers if not getattr(h, '_is_log_capture', False)]
    root.addHandler(handler)


for k, v in {"log_buffer": [], "running": False, "result": None}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ════════════════════════════════════════
# 顶部
# ════════════════════════════════════════
st.title("💳 Auto BindCard")

col_step1, col_step2, col_step3, col_proxy = st.columns([1, 1, 1, 2])
with col_step1:
    do_register = st.checkbox("注册账号", value=True)
with col_step2:
    do_checkout = st.checkbox("创建 Checkout", value=True)
with col_step3:
    do_payment = st.checkbox("提交支付", value=True)
with col_proxy:
    proxy = st.text_input("代理 (可选)", placeholder="socks5://127.0.0.1:1080", label_visibility="collapsed")

st.divider()

# ════════════════════════════════════════
# 配置区
# ════════════════════════════════════════
cfg_col1, cfg_col2 = st.columns(2)

with cfg_col1:
    with st.expander("📧 邮箱 & Team Plan", expanded=True):
        mail_worker = st.text_input("邮箱 Worker", value="https://apimail.mkai.de5.net")
        mc1, mc2 = st.columns(2)
        mail_domain = mc1.text_input("邮箱域名", value="mkai.de5.net")
        mail_token = mc2.text_input("邮箱 Token", value="ma123999", type="password")
        st.markdown("---")
        tc1, tc2, tc3 = st.columns(3)
        workspace_name = tc1.text_input("Workspace", value="Artizancloud")
        seat_quantity = tc2.number_input("席位数", min_value=2, max_value=50, value=5)
        promo_campaign = tc3.text_input("活动 ID", value="team0dollar")

with cfg_col2:
    with st.expander("💰 账单地址", expanded=True):
        country_label = st.selectbox("国家", list(COUNTRY_MAP.keys()), index=0)
        country_code, default_currency, default_state, default_addr = COUNTRY_MAP[country_label]
        bc1, bc2 = st.columns(2)
        billing_name = bc1.text_input("姓名", value="Test User")
        currency = bc2.text_input("货币", value=default_currency)
        bc3, bc4 = st.columns(2)
        address_line1 = bc3.text_input("地址", value=default_addr)
        address_state = bc4.text_input("州/省", value=default_state)

if do_payment:
    with st.expander("💳 信用卡 ⚠️ Live 模式 - 真实扣款", expanded=True):
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
        tc_sel = st.selectbox("🧪 快速填充测试卡", ["不填充"] + list(TEST_CARDS.keys()), key="tc_sel")
        if tc_sel != "不填充":
            tc_num, tc_cvc = TEST_CARDS[tc_sel]
            st.session_state["_test_card_number"] = tc_num
            st.session_state["_test_cvc"] = tc_cvc

        _tn = st.session_state.get("_test_card_number", "")
        _tm = st.session_state.get("_test_exp_month", "12")
        _ty = st.session_state.get("_test_exp_year", "2030")
        _tc = st.session_state.get("_test_cvc", "")

        cc1, cc2, cc3, cc4 = st.columns([3, 1, 1, 1])
        card_number = cc1.text_input("卡号", value=_tn, placeholder="真实卡号")
        exp_month = cc2.text_input("月", value=_tm)
        exp_year = cc3.text_input("年", value=_ty)
        card_cvc = cc4.text_input("CVC", value=_tc, type="password")

        if _tn and _tn.startswith("4"):
            st.caption("⚠️ Live 模式下所有测试卡都会被拒绝，仅用于验证流程")

st.divider()

# ════════════════════════════════════════
# Tab
# ════════════════════════════════════════
steps_list = []
if do_register: steps_list.append("注册")
if do_checkout: steps_list.append("Checkout")
if do_payment: steps_list.append("支付")

tab_run, tab_accounts, tab_history = st.tabs(["▶ 执行", "📋 账号", "📊 历史"])

# ── 构建节点 ──
ALL_NODES = [
    ("邮箱创建", "mail"),
    ("账号注册", "register"),
    ("Checkout", "checkout"),
    ("指纹获取", "fingerprint"),
    ("卡片Token", "tokenize"),
    ("确认支付", "confirm"),
]

def get_active_nodes():
    active = []
    if do_register:
        active += [("邮箱创建", "mail"), ("账号注册", "register")]
    if do_checkout:
        active += [("Checkout", "checkout"), ("指纹获取", "fingerprint")]
    if do_payment:
        active += [("卡片Token", "tokenize"), ("确认支付", "confirm")]
    return active


with tab_run:
    bc1, bc2 = st.columns([3, 1])
    with bc1:
        run_btn = st.button("🚀 开始执行", disabled=st.session_state.running or not steps_list, use_container_width=True, type="primary")
    with bc2:
        if st.button("🗑️ 清空", use_container_width=True):
            st.session_state.log_buffer = []
            st.session_state.result = None
            st.rerun()

    active_nodes = get_active_nodes()

    if run_btn:
        st.session_state.running = True
        st.session_state.log_buffer = []
        st.session_state.result = None
        init_logging()

        pipeline_area = st.empty()
        log_area = st.empty()

        store = ResultStore(output_dir=OUTPUT_DIR)
        rd = {"success": False, "error": "", "email": "", "steps": {}}
        node_status = {key: "pending" for _, key in active_nodes}

        def _refresh():
            pipeline_area.markdown(
                render_pipeline_html([(name, node_status.get(key, "pending")) for name, key in active_nodes]),
                unsafe_allow_html=True,
            )

        _refresh()

        try:
            cfg = Config()
            cfg.proxy = proxy or None
            cfg.mail.email_domain = mail_domain
            cfg.mail.worker_domain = mail_worker
            cfg.mail.admin_token = mail_token
            cfg.team_plan.workspace_name = workspace_name
            cfg.team_plan.seat_quantity = seat_quantity
            cfg.team_plan.promo_campaign_id = promo_campaign
            cfg.billing = BillingInfo(name=billing_name, email="", country=country_code, currency=currency,
                                      address_line1=address_line1, address_state=address_state)
            if do_payment:
                cfg.card = CardInfo(number=card_number, cvc=card_cvc, exp_month=exp_month, exp_year=exp_year)

            auth_result = None
            af = None

            # ── 注册 ──
            if do_register:
                node_status["mail"] = "running"; _refresh()
                mp = MailProvider(worker_domain=cfg.mail.worker_domain, admin_token=cfg.mail.admin_token, email_domain=cfg.mail.email_domain)
                node_status["mail"] = "done"
                node_status["register"] = "running"; _refresh()
                af = AuthFlow(cfg)
                auth_result = af.run_register(mp)
                rd["email"] = auth_result.email
                rd["steps"]["mail"] = "✅"
                rd["steps"]["register"] = "✅"
                node_status["register"] = "done"; _refresh()
                store.save_credentials(auth_result.to_dict())
                store.append_credentials_csv(auth_result.to_dict())
                log_area.code("\n".join(st.session_state.log_buffer[-60:]), language="log")

            # ── Checkout ──
            if do_checkout:
                if not auth_result:
                    raise RuntimeError("需先注册或提供凭证")
                node_status["checkout"] = "running"; _refresh()
                cfg.billing.email = auth_result.email
                pf = PaymentFlow(cfg, auth_result)
                if af:
                    pf.session = af.session
                cs_id = pf.create_checkout_session()
                rd["checkout_session_id"] = cs_id
                rd["steps"]["checkout"] = "✅"
                node_status["checkout"] = "done"; _refresh()
                log_area.code("\n".join(st.session_state.log_buffer[-60:]), language="log")

                node_status["fingerprint"] = "running"; _refresh()
                pf.fetch_stripe_fingerprint()
                pf.extract_stripe_pk(pf.checkout_url)
                rd["stripe_pk"] = (pf.stripe_pk[:30] + "...") if pf.stripe_pk else ""
                rd["steps"]["fingerprint"] = "✅"
                node_status["fingerprint"] = "done"; _refresh()
                log_area.code("\n".join(st.session_state.log_buffer[-60:]), language="log")

                # ── 支付 ──
                if do_payment:
                    node_status["tokenize"] = "running"; _refresh()
                    pf.payment_method_id = pf.create_payment_method()
                    rd["steps"]["tokenize"] = "✅"
                    node_status["tokenize"] = "done"; _refresh()
                    log_area.code("\n".join(st.session_state.log_buffer[-60:]), language="log")

                    node_status["confirm"] = "running"; _refresh()
                    pay = pf.confirm_payment(cs_id)
                    rd["confirm_status"] = pay.confirm_status
                    rd["confirm_response"] = pay.confirm_response
                    rd["success"] = pay.success
                    rd["error"] = pay.error
                    rd["steps"]["confirm"] = "✅" if pay.success else "❌"
                    node_status["confirm"] = "done" if pay.success else "error"; _refresh()
                else:
                    rd["success"] = True
            elif do_register:
                rd["success"] = True

        except Exception as e:
            rd["error"] = str(e)
            st.session_state.log_buffer.append(f"EXCEPTION:\n{traceback.format_exc()}")
            # 尝试提取 Stripe 原始响应
            try:
                if 'pf' in dir() and pf and pf.result.confirm_response:
                    rd["confirm_response"] = pf.result.confirm_response
                    rd["confirm_status"] = pf.result.confirm_status
            except Exception:
                pass
            for k in node_status:
                if node_status[k] == "running":
                    node_status[k] = "error"
            _refresh()

        st.session_state.result = rd
        st.session_state.running = False

        try:
            store.save_result(rd, "ui_run")
            if rd.get("email"):
                store.append_history(email=rd["email"], status="ui_run",
                                     checkout_session_id=rd.get("checkout_session_id", ""),
                                     payment_status=rd.get("confirm_status", ""),
                                     error=rd.get("error", ""))
        except Exception:
            pass

        # 最终结果
        if rd["success"]:
            st.success(f"✅ 全部完成! {rd.get('email', '')}")
        else:
            st.error(f"❌ {rd.get('error', '')}")

        # 如果有 Stripe 原始返回，展示
        if rd.get("confirm_response"):
            with st.expander("Stripe 原始响应", expanded=True):
                st.json(rd["confirm_response"])

        log_area.code("\n".join(st.session_state.log_buffer[-200:]), language="log")

    elif st.session_state.log_buffer:
        st.code("\n".join(st.session_state.log_buffer[-200:]), language="log")

    # ── 结果 ──
    if st.session_state.result and not run_btn:
        r = st.session_state.result

        if r.get("steps"):
            node_status_saved = {}
            for _, key in active_nodes:
                node_status_saved[key] = "done" if r["steps"].get(key, "").startswith("✅") else ("error" if key in r["steps"] else "pending")
            st.markdown(
                render_pipeline_html([(name, node_status_saved.get(key, "pending")) for name, key in active_nodes]),
                unsafe_allow_html=True,
            )

        st.divider()
        cols = st.columns(4)
        cols[0].metric("邮箱", r.get("email") or "-")
        cols[1].metric("Checkout", (r.get("checkout_session_id", "")[:20] + "...") if r.get("checkout_session_id") else "-")
        cols[2].metric("Confirm", r.get("confirm_status") or "-")
        cols[3].metric("状态", "成功" if r.get("success") else "失败")

        if r.get("confirm_response"):
            with st.expander("Stripe 原始响应", expanded=False):
                st.json(r["confirm_response"])

        with st.expander("完整 JSON 结果", expanded=False):
            st.json(r)


# ════════════════════════════════════════
# Tab: 账号
# ════════════════════════════════════════
with tab_accounts:
    csv_path = os.path.join(OUTPUT_DIR, "accounts.csv")
    if os.path.exists(csv_path):
        try:
            import pandas as pd
            df = pd.read_csv(csv_path)
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"共 {len(df)} 条记录")
                if st.button("🔄 刷新", key="ref_acc"):
                    st.rerun()
            else:
                st.info("暂无账号记录")
        except Exception as e:
            st.error(str(e))
    else:
        st.info("暂无账号。注册后自动保存到此处。")

    st.divider()
    with st.expander("📁 凭证文件", expanded=False):
        if os.path.exists(OUTPUT_DIR):
            cred_files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith("credentials_") and f.endswith(".json")], reverse=True)
            if cred_files:
                sel = st.selectbox("选择凭证文件", cred_files, key="cred_sel")
                if sel:
                    with open(os.path.join(OUTPUT_DIR, sel)) as f:
                        data = json.load(f)
                    st.json({k: (v[:50] + "..." + v[-20:] if isinstance(v, str) and len(v) > 80 else v) for k, v in data.items()})
            else:
                st.caption("暂无凭证文件")


# ════════════════════════════════════════
# Tab: 历史
# ════════════════════════════════════════
with tab_history:
    hist_path = os.path.join(OUTPUT_DIR, "history.csv")
    if os.path.exists(hist_path):
        try:
            import pandas as pd
            df = pd.read_csv(hist_path)
            if not df.empty:
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"共 {len(df)} 条")
                if st.button("🔄 刷新", key="ref_hist"):
                    st.rerun()
            else:
                st.info("暂无历史")
        except Exception as e:
            st.error(str(e))
    else:
        st.info("暂无执行历史")

    st.divider()
    with st.expander("📁 结果文件", expanded=False):
        if os.path.exists(OUTPUT_DIR):
            rf = sorted([f for f in os.listdir(OUTPUT_DIR) if f.endswith(".json") and not f.startswith("credentials_") and not f.startswith("debug_")], reverse=True)
            if rf:
                sel = st.selectbox("选择结果文件", rf, key="res_sel")
                if sel:
                    with open(os.path.join(OUTPUT_DIR, sel)) as f:
                        st.json(json.load(f))
            else:
                st.caption("暂无结果文件")
