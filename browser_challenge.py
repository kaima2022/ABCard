"""
Playwright 浏览器处理 Stripe hCaptcha 挑战

两种方式:
  1. solve() — 用 Stripe.js handleNextAction() 自动处理
  2. solve_hcaptcha_direct() — 直接加载 hCaptcha SDK, 获取 token, 用 API 提交

方式2更灵活: 在真实浏览器中执行 hCaptcha invisible challenge,
然后把 token 返回给 Python 代码通过 verify_challenge API 提交。
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)


class BrowserChallengeSolver:
    """用真实浏览器处理 Stripe hCaptcha 挑战"""

    def __init__(self, stripe_pk: str, proxy: str = None, headless: bool = True):
        self.stripe_pk = stripe_pk
        self.proxy = proxy
        self.headless = headless

    def solve_hcaptcha_direct(self, site_key: str, site_url: str, rqdata: str = "", timeout: int = 60) -> dict:
        """
        在真实浏览器中执行 hCaptcha invisible 挑战。
        直接加载 hCaptcha SDK, 执行 invisible challenge, 返回 token。
        使用增强反检测: 鼠标模拟 + stealth + 持久化 profile。

        返回:
            {"success": True, "token": "...", "ekey": "..."} 或
            {"success": False, "error": "..."}
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"success": False, "error": "playwright not installed"}

        import os, random

        logger.info(f"[Browser] 直接 hCaptcha: site_key={site_key[:20]}...")
        logger.info(f"[Browser] headless={self.headless}, proxy={self.proxy or '直连'}")

        # hCaptcha 页面 HTML — 更真实的页面结构
        hcaptcha_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Secure Payment Verification</title>
    <script src="https://js.hcaptcha.com/1/api.js?render=explicit&onload=onHcaptchaLoad" async defer></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 40px; background: #f6f9fc; }
        .container { max-width: 480px; margin: 0 auto; background: white; border-radius: 8px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        h2 { color: #32325d; margin: 0 0 16px 0; font-size: 20px; }
        p { color: #6b7c93; margin: 0 0 24px 0; line-height: 1.5; }
        #hcaptcha-container { min-height: 60px; }
        .footer { margin-top: 24px; text-align: center; color: #aab7c4; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Verifying your identity</h2>
        <p>Please wait while we verify your payment. This process is automatic and should complete shortly.</p>
        <div id="hcaptcha-container"></div>
        <div class="footer">Secured by Stripe</div>
    </div>
    <script>
        window.__hcaptchaResult = null;
        window.__hcaptchaError = null;
        window.__hcaptchaReady = false;
        function onHcaptchaLoad() { window.__hcaptchaReady = true; }
    </script>
</body>
</html>"""

        # 持久化浏览器 profile 目录
        profile_dir = os.path.join(os.path.dirname(__file__), ".browser_profile")
        os.makedirs(profile_dir, exist_ok=True)

        with sync_playwright() as p:
            launch_args = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-web-security",
                    "--disable-infobars",
                    "--window-size=1280,800",
                    "--disable-dev-shm-usage",
                ],
            }
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                color_scheme="light",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )

            # 应用 stealth 补丁 (隐藏 webdriver/automation 特征)
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(context)
                logger.info("[Browser] stealth 补丁已应用")
            except ImportError:
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)

            # 额外的反检测注入
            context.add_init_script("""
                // 模拟真实 Chrome 的 plugin 列表
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin' },
                    ],
                });
                // 模拟 WebGL
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter.call(this, parameter);
                };
                // chrome.runtime 存在性
                if (!window.chrome) window.chrome = {};
                if (!window.chrome.runtime) window.chrome.runtime = { id: undefined };
            """)

            page = context.new_page()
            page.set_default_timeout(timeout * 1000 + 15000)

            try:
                # 虚拟 HTTPS 页面 — 用 js.stripe.com 域
                page.route("https://js.stripe.com/challenge/**", lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=hcaptcha_html,
                ))

                page.goto("https://js.stripe.com/challenge/verify", wait_until="domcontentloaded", timeout=15000)

                # 模拟用户行为 — 鼠标移动、滚动 (提升 hCaptcha 评分)
                logger.info("[Browser] 模拟用户行为...")
                time.sleep(random.uniform(0.5, 1.5))

                # 随机鼠标移动 (模拟人类不规则轨迹)
                for _ in range(random.randint(3, 6)):
                    x = random.randint(100, 800)
                    y = random.randint(100, 600)
                    page.mouse.move(x, y, steps=random.randint(5, 15))
                    time.sleep(random.uniform(0.1, 0.4))

                # 模拟页面滚动
                page.evaluate("window.scrollBy(0, 100)")
                time.sleep(random.uniform(0.3, 0.8))
                page.evaluate("window.scrollBy(0, -50)")
                time.sleep(random.uniform(0.3, 0.6))

                # 模拟点击页面空白处
                page.mouse.click(random.randint(200, 400), random.randint(300, 500))
                time.sleep(random.uniform(0.5, 1.0))

                # 等待 hCaptcha SDK 加载
                logger.info("[Browser] 等待 hCaptcha SDK 加载...")
                page.wait_for_function("window.__hcaptchaReady === true", timeout=15000)
                logger.info("[Browser] hCaptcha SDK 已加载")

                # 更多鼠标移动 (在 hCaptcha 容器附近)
                for _ in range(random.randint(2, 4)):
                    x = random.randint(200, 500)
                    y = random.randint(350, 550)
                    page.mouse.move(x, y, steps=random.randint(8, 20))
                    time.sleep(random.uniform(0.1, 0.3))

                time.sleep(random.uniform(0.5, 1.5))

                # 渲染 + 执行 invisible hCaptcha
                logger.info("[Browser] 执行 hCaptcha invisible challenge...")
                timeout_ms = timeout * 1000
                result = page.evaluate("""
                    (params) => {
                        return new Promise((resolve, reject) => {
                            const timer = setTimeout(() => {
                                reject(new Error('hCaptcha execute timeout'));
                            }, params.timeout);

                            try {
                                const widgetId = hcaptcha.render('hcaptcha-container', {
                                    sitekey: params.siteKey,
                                    size: 'invisible',
                                    callback: (token) => {
                                        clearTimeout(timer);
                                        const ekey = hcaptcha.getRespKey(widgetId) || '';
                                        resolve({ success: true, token: token, ekey: ekey });
                                    },
                                    'expired-callback': () => {
                                        clearTimeout(timer);
                                        resolve({success: false, error: 'hCaptcha token expired'});
                                    },
                                    'chalexpired-callback': () => {
                                        clearTimeout(timer);
                                        resolve({success: false, error: 'hCaptcha challenge expired'});
                                    },
                                    'error-callback': (err) => {
                                        clearTimeout(timer);
                                        resolve({success: false, error: 'hCaptcha error: ' + String(err)});
                                    },
                                });

                                const executeOpts = {};
                                if (params.rqdata) {
                                    executeOpts.rqdata = params.rqdata;
                                }
                                hcaptcha.execute(widgetId, executeOpts);
                            } catch (e) {
                                clearTimeout(timer);
                                resolve({success: false, error: e.message || String(e)});
                            }
                        });
                    }
                """, {"siteKey": site_key, "rqdata": rqdata, "timeout": timeout_ms})

                logger.info(f"[Browser] hCaptcha 结果: success={result.get('success')}, token_len={len(result.get('token', ''))}")
                return result

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[Browser] hCaptcha 异常: {error_msg}")
                try:
                    page.screenshot(path="test_outputs/browser_hcaptcha_error.png")
                except Exception:
                    pass
                return {"success": False, "error": f"Browser exception: {error_msg}"}
            finally:
                browser.close()

    def solve_hcaptcha_uc(self, site_key: str, site_url: str, rqdata: str = "", timeout: int = 60) -> dict:
        """
        使用 undetected-chromedriver (Selenium) 执行 hCaptcha invisible 挑战。
        UC 专门为绕过 hCaptcha/CloudFlare 等 bot 检测设计。
        """
        try:
            import undetected_chromedriver as uc
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            return {"success": False, "error": "undetected-chromedriver not installed"}

        import os, random, tempfile

        logger.info(f"[UC] 启动 undetected Chrome: site_key={site_key[:20]}...")
        logger.info(f"[UC] headless={self.headless}, proxy={self.proxy or '直连'}")

        # 持久化 profile
        profile_dir = os.path.join(os.path.dirname(__file__), ".uc_profile")
        os.makedirs(profile_dir, exist_ok=True)

        # hCaptcha 页面
        hcaptcha_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Secure Verification</title>
    <script src="https://js.hcaptcha.com/1/api.js?render=explicit&onload=onHcaptchaLoad" async defer></script>
    <style>body { font-family: sans-serif; padding: 40px; background: #f6f9fc; }</style>
</head>
<body>
    <h2>Verifying your identity</h2>
    <div id="hcaptcha-container"></div>
    <script>
        window.__hcaptchaReady = false;
        function onHcaptchaLoad() { window.__hcaptchaReady = true; }
    </script>
</body>
</html>"""

        # 保存 HTML 到临时文件 (UC 不支持 route interception)
        html_file = os.path.join(tempfile.gettempdir(), "hcaptcha_challenge.html")
        with open(html_file, "w") as f:
            f.write(hcaptcha_html)

        chrome_binary = self._find_chrome_binary()

        options = uc.ChromeOptions()
        if chrome_binary:
            options.binary_location = chrome_binary
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-extensions")
        options.add_argument(f"--user-data-dir={profile_dir}")
        if self.proxy:
            options.add_argument(f"--proxy-server={self.proxy}")

        driver = None
        try:
            uc_kwargs = {"options": options, "use_subprocess": True}
            if chrome_binary:
                uc_kwargs["browser_executable_path"] = chrome_binary
            driver = uc.Chrome(**uc_kwargs)
            driver.set_page_load_timeout(30)

            # 先访问 js.stripe.com 获得正确的域上下文
            logger.info("[UC] 导航到 js.stripe.com...")
            driver.get("https://js.stripe.com/v3/")
            time.sleep(random.uniform(1.0, 2.0))

            # 在 Stripe 域下注入 hCaptcha 页面
            logger.info("[UC] 注入 hCaptcha 环境...")
            driver.execute_script("""
                document.head.innerHTML = '';
                document.body.innerHTML = '<div id="hcaptcha-container"></div>';
                window.__hcaptchaReady = false;
                var script = document.createElement('script');
                script.src = 'https://js.hcaptcha.com/1/api.js?render=explicit&onload=onHcaptchaLoad';
                script.async = true;
                window.onHcaptchaLoad = function() { window.__hcaptchaReady = true; };
                document.head.appendChild(script);
            """)

            # 模拟用户行为
            time.sleep(random.uniform(1.0, 2.0))
            from selenium.webdriver.common.action_chains import ActionChains
            actions = ActionChains(driver)
            for _ in range(random.randint(3, 6)):
                x = random.randint(-300, 300)
                y = random.randint(-200, 200)
                actions.move_by_offset(x, y).perform()
                actions = ActionChains(driver)  # reset
                time.sleep(random.uniform(0.1, 0.3))

            # 等待 hCaptcha SDK 加载
            logger.info("[UC] 等待 hCaptcha SDK 加载...")
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return window.__hcaptchaReady === true")
            )
            logger.info("[UC] hCaptcha SDK 已加载")

            # 更多鼠标移动
            for _ in range(random.randint(2, 4)):
                actions = ActionChains(driver)
                actions.move_by_offset(random.randint(-100, 100), random.randint(-50, 50)).perform()
                time.sleep(random.uniform(0.1, 0.3))

            time.sleep(random.uniform(0.5, 1.0))

            # 执行 hCaptcha challenge
            logger.info("[UC] 执行 hCaptcha invisible challenge...")
            timeout_ms = timeout * 1000
            result = driver.execute_script("""
                return new Promise((resolve, reject) => {
                    const timer = setTimeout(() => {
                        resolve({success: false, error: 'hCaptcha execute timeout'});
                    }, arguments[0].timeout);

                    try {
                        const widgetId = hcaptcha.render('hcaptcha-container', {
                            sitekey: arguments[0].siteKey,
                            size: 'invisible',
                            callback: (token) => {
                                clearTimeout(timer);
                                const ekey = hcaptcha.getRespKey(widgetId) || '';
                                resolve({ success: true, token: token, ekey: ekey });
                            },
                            'error-callback': (err) => {
                                clearTimeout(timer);
                                resolve({success: false, error: 'hCaptcha error: ' + String(err)});
                            },
                        });

                        const executeOpts = {};
                        if (arguments[0].rqdata) {
                            executeOpts.rqdata = arguments[0].rqdata;
                        }
                        hcaptcha.execute(widgetId, executeOpts);
                    } catch (e) {
                        clearTimeout(timer);
                        resolve({success: false, error: e.message || String(e)});
                    }
                });
            """, {"siteKey": site_key, "rqdata": rqdata, "timeout": timeout_ms})

            logger.info(f"[UC] hCaptcha 结果: success={result.get('success')}, token_len={len(result.get('token', ''))}")
            return result

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[UC] 异常: {error_msg}")
            if driver:
                try:
                    driver.save_screenshot("test_outputs/uc_error.png")
                except Exception:
                    pass
            return {"success": False, "error": f"UC exception: {error_msg}"}
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    @staticmethod
    def _find_chrome_binary() -> str:
        """查找可用的 Chrome/Chromium 二进制文件"""
        import glob as gl
        # Playwright 自带 Chrome (优先)
        pw_chrome = os.path.expanduser("~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome")
        if os.path.isfile(pw_chrome):
            return pw_chrome
        # 系统 Chrome
        for path in [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]:
            if os.path.isfile(path):
                return path
        # Playwright 其他版本
        pw_chromes = gl.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        if pw_chromes:
            return sorted(pw_chromes)[-1]
        return None  # 让 undetected-chromedriver 自行查找

    def solve(self, pi_client_secret: str, timeout: int = 60) -> dict:
        """
        使用 Playwright 浏览器处理 Stripe hCaptcha 挑战。

        参数:
            pi_client_secret: payment_intent 的 client_secret (pi_xxx_secret_yyy)
            timeout: 最长等待时间 (秒)

        返回:
            {"success": True, "status": "succeeded|processing", "pi_data": {...}}
            {"success": False, "error": "..."}
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"success": False, "error": "playwright 未安装: pip install playwright && playwright install chromium"}

        logger.info(f"[Browser] 启动 Chromium 处理 hCaptcha 挑战...")
        logger.info(f"[Browser] headless={self.headless}, proxy={self.proxy or '直连'}, timeout={timeout}s")

        # 构建要注入浏览器的HTML (加载 Stripe.js + 执行 handleNextAction)
        stripe_html = self._build_stripe_page()

        with sync_playwright() as p:
            launch_args = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-web-security",  # 允许跨域 (虚拟HTTPS页面加载Stripe.js)
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
            }
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )

            # 应用 stealth 补丁 (隐藏 webdriver/automation 特征)
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(context)
                logger.info("[Browser] stealth 补丁已应用")
            except ImportError:
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                """)

            page = context.new_page()
            # 全局超时设置
            page.set_default_timeout(timeout * 1000 + 15000)  # JS timeout + 额外缓冲

            try:
                # 用 route 拦截创建虚拟 HTTPS 页面
                # 使用 js.stripe.com 域, 只拦截 /challenge/ 路径, v3/ 脚本正常加载
                logger.info("[Browser] 创建虚拟 HTTPS 页面 (js.stripe.com)...")
                page.route("https://js.stripe.com/challenge/**", lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=stripe_html,
                ))

                # 监听浏览器 console 输出
                page.on("console", lambda msg: logger.info(f"[Browser console] {msg.type}: {msg.text}"))
                page.on("pageerror", lambda err: logger.error(f"[Browser error] {err}"))
                # 监听网络请求: 特别关注 verify_challenge 请求
                def _log_response(resp):
                    url = resp.url
                    if resp.status >= 400:
                        logger.warning(f"[Browser {resp.status}] {url[:150]}")
                    if "verify_challenge" in url:
                        logger.info(f"[Browser] verify_challenge 请求: {url}")
                        logger.info(f"[Browser] verify_challenge 状态: {resp.status}")
                        try:
                            body = resp.text()
                            logger.info(f"[Browser] verify_challenge 响应: {body[:500]}")
                        except Exception:
                            pass
                def _log_request(req):
                    if "verify_challenge" in req.url:
                        logger.info(f"[Browser] verify_challenge POST: {req.url}")
                        logger.info(f"[Browser] verify_challenge headers: {dict(req.headers)}")
                        if req.post_data:
                            # 打印完整 body (解析字段名)
                            body = req.post_data
                            logger.info(f"[Browser] verify_challenge body (len={len(body)}): {body[:200]}")
                            # 解析并列出所有字段名
                            fields = [part.split('=')[0] for part in body.split('&') if '=' in part]
                            logger.info(f"[Browser] verify_challenge 字段: {fields}")
                page.on("response", _log_response)
                page.on("request", _log_request)

                page.goto("https://js.stripe.com/challenge/checkout", wait_until="domcontentloaded", timeout=15000)

                # 等待 Stripe.js v3 加载
                logger.info("[Browser] 等待 Stripe.js 加载...")
                page.wait_for_function("typeof Stripe !== 'undefined'", timeout=30000)
                logger.info("[Browser] Stripe.js 已加载")

                # 执行 handleNextAction (带超时)
                logger.info("[Browser] 调用 stripe.handleNextAction()...")
                timeout_ms = timeout * 1000
                result = page.evaluate("""
                    async (params) => {
                        const stripe = Stripe(params.pk);
                        try {
                            // 带超时的 handleNextAction
                            const timeoutPromise = new Promise((_, reject) =>
                                setTimeout(() => reject(new Error('handleNextAction timeout')), params.timeout)
                            );
                            const actionPromise = stripe.handleNextAction({
                                clientSecret: params.clientSecret,
                            });
                            const result = await Promise.race([actionPromise, timeoutPromise]);
                            if (result.error) {
                                return {
                                    success: false,
                                    error: result.error.message || result.error.type,
                                    error_code: result.error.code || '',
                                };
                            }
                            const pi = result.paymentIntent;
                            return {
                                success: pi.status === 'succeeded' || pi.status === 'processing',
                                status: pi.status,
                                pi_id: pi.id,
                                pi_data: {
                                    id: pi.id,
                                    status: pi.status,
                                    amount: pi.amount,
                                    currency: pi.currency,
                                },
                            };
                        } catch (e) {
                            return {
                                success: false,
                                error: e.message || String(e),
                            };
                        }
                    }
                """, {"pk": self.stripe_pk, "clientSecret": pi_client_secret, "timeout": timeout_ms})

                logger.info(f"[Browser] handleNextAction 结果: {json.dumps(result, ensure_ascii=False)[:500]}")
                return result

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[Browser] Playwright 异常: {error_msg}")
                # 尝试截图保存调试信息
                try:
                    page.screenshot(path="test_outputs/browser_challenge_error.png")
                    logger.info("[Browser] 错误截图已保存: test_outputs/browser_challenge_error.png")
                except Exception:
                    pass
                return {"success": False, "error": f"Browser exception: {error_msg}"}
            finally:
                browser.close()

    def _build_stripe_page(self) -> str:
        """构建注入浏览器的 HTML 页面"""
        return """<!DOCTYPE html>
<html>
<head>
    <title>Payment Processing</title>
    <script src="https://js.stripe.com/v3/"></script>
</head>
<body>
    <div id="status">Loading Stripe.js...</div>
    <div id="hcaptcha-container"></div>
    <script>
        document.getElementById('status').textContent = 'Stripe.js loaded, ready.';
    </script>
</body>
</html>"""
