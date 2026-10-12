import os
import time
import json
import math
import random
import requests
import argparse
import sys
import re
from datetime import datetime, timedelta
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

# --- Selenium 相关库 (新增 Edge 支持) ---
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

# ==========================================
# 1. 配置区域 (Configuration)
# ==========================================
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent.parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
TARGET_SITE_URL = "https://stock.quicktiny.cn/"

# 导入项目配置
try:
    sys.path.insert(0, str(PROJECT_ROOT))
    from config.config import Config
    EXTERNAL_DATA_DIR = Path(Config.BASE_DIR) / "data" / "spider_data" / "dragon_eye" / "data_lake"
    TUSHARE_TOKEN = getattr(Config, 'TUSHARE_TOKEN', '')
except ImportError:
    EXTERNAL_DATA_DIR = PROJECT_ROOT / "data" / "spider_data" / "dragon_eye" / "data_lake"
    PROJECT_TOKEN, PROJECT_USER, PROJECT_PASS = "", "", ""
    TUSHARE_TOKEN = ""

TRADE_CAL_CACHE = {}

def is_trading_day(date_obj: datetime) -> bool:
    """
    判断是否为交易日（使用 Tushare 交易日历）

    Args:
        date_obj: 日期对象

    Returns:
        bool: 是否为交易日
    """
    date_str = date_obj.strftime("%Y%m%d")
    year = date_obj.year

    if year not in TRADE_CAL_CACHE:
        try:
            import tushare as ts
            pro = ts.pro_api(TUSHARE_TOKEN)
            start = f"{year}0101"
            end = f"{year}1231"
            cal_df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
            TRADE_CAL_CACHE[year] = set(cal_df[cal_df['is_open'] == 1]['cal_date'].tolist())
        except Exception as e:
            print(f"⚠️ 获取交易日历失败: {e}，使用简单周末判断")
            TRADE_CAL_CACHE[year] = None

    if TRADE_CAL_CACHE.get(year) is None:
        return date_obj.weekday() < 5

    return date_str in TRADE_CAL_CACHE.get(year, set())

# 获取集中配置的凭证
try:
    PROJECT_TOKEN = getattr(Config, 'DRAGON_TOKEN', '')
    PROJECT_USER = getattr(Config, 'DRAGON_USERNAME', '')
    PROJECT_PASS = getattr(Config, 'DRAGON_PASSWORD', '')
except NameError:
    PROJECT_TOKEN, PROJECT_USER, PROJECT_PASS = "", "", ""

# 默认配置
DEFAULT_CONFIG = {
    "TOKEN": PROJECT_TOKEN, 
    "USERNAME": PROJECT_USER,  # 从项目配置读取
    "PASSWORD": PROJECT_PASS,  # 从项目配置读取
    "HEADERS": {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "priority": "u=1, i",
        "referer": "https://stock.quicktiny.cn/",
        "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    },
    "DATA_DIR": str(EXTERNAL_DATA_DIR)
}

def load_config():
    """加载配置：优先使用本地 config.json（含最新 Token），其次集中配置"""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                ext_config = json.load(f)
                # 本地 config.json 优先（Token 会动态更新）
                for key in ["TOKEN", "USERNAME", "PASSWORD"]:
                    if ext_config.get(key):
                        config[key] = ext_config[key]
                if "HEADERS" in ext_config:
                    config["HEADERS"].update(ext_config["HEADERS"])
        except Exception as e:
            print(f"⚠️ 配置文件读取失败: {e}")
    return config

def save_token_to_config(new_token):
    """保存新 Token 到 config.json"""
    clean_token = new_token.replace("Bearer ", "").strip()
    current_conf = load_config()
    current_conf["TOKEN"] = clean_token
    
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(current_conf, f, ensure_ascii=False, indent=2)
    print(f"💾 新 Token 已保存到配置文件")
    return clean_token

# ==========================================
# 2. Token 自动获取模块 (双浏览器保险版)
# ==========================================
def get_browser_driver():
    """
    尝试启动浏览器：优先 Edge，失败则转 Chrome
    """
    # 方案 A: 尝试 Microsoft Edge (Windows 必有)
    try:
        print("   [1/2] 尝试启动 Microsoft Edge...")
        options = EdgeOptions()
        # 直接使用系统已安装的Edge，无需下载驱动
        driver = webdriver.Edge(options=options)
        return driver
    except Exception as e:
        print(f"   ⚠️ Edge 启动失败 (未安装或路径错误)")

    # 方案 B: 尝试 Chrome
    try:
        print("   [2/2] 尝试启动 Google Chrome...")
        options = ChromeOptions()
        # options.add_argument("--headless") # 调试阶段注释掉
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        print(f"❌ 浏览器启动全军覆没: {e}")
        return None

def launch_browser_for_token():
    """启动浏览器捕获 Token"""
    print("\n" + "="*50)
    print("🔓 Token 失效！正在启动浏览器...")
    print("👉 请在弹出的窗口中【扫码登录】或【等待自动登录】")
    print("="*50 + "\n")

    # 加载配置，获取账号密码
    config = load_config()
    username = config.get("USERNAME", "")
    password = config.get("PASSWORD", "")

    driver = get_browser_driver()
    if not driver:
        print("❌ 无法找到 Chrome 或 Edge 浏览器，请确保至少安装了一个。")
        sys.exit(1)
    
    captured_token = None
    
    try:
        driver.get(TARGET_SITE_URL)
        time.sleep(2)

        if username and password:
            print(f"\n🔑 检测到账号密码，尝试自动登录...")
            _try_auto_login(driver, username, password)

        # 循环检测 Token (180秒)
        for i in range(180):
            time.sleep(1)

            token_candidate = driver.execute_script("""
                let keys = ['token', 'Token', 'Authorization', 'user_token', 'access_token'];
                for (let key of keys) {
                    let val = localStorage.getItem(key);
                    if (val && val.includes('eyJ')) return val;
                }
                for (let i = 0; i < localStorage.length; i++) {
                    let val = localStorage.getItem(localStorage.key(i));
                    if (val && val.includes('eyJ')) return val;
                }
                return null;
            """)

            if token_candidate:
                match = re.search(r'(eyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+)', token_candidate)
                if match:
                    captured_token = match.group(1)
                    print(f"\n🎉 成功捕获 Token!")
                    break

            print(f"⏳ 等待登录... ({i}/180s)", end="\r")

    except Exception as e:
        print(f"❌ 浏览器操作异常: {e}")
    finally:
        try:
            driver.quit()
        except:
            pass

    if captured_token:
        return save_token_to_config(captured_token)
    else:
        print("\n❌ 获取 Token 失败（超时）。")
        sys.exit(1)

def _try_auto_login(driver, username: str, password: str):
    """
    自动登录：先尝试点击'账号密码登录'tab，再填账号密码提交
    """
    tried = False

    # 策略1: 查找并点击"账号密码登录" tab/链接（仅匹配明确的 tab，避免误点"忘记密码"链接）
    tab_selectors = [
        "//*[contains(text(),'账号密码登录')]",
        "//button[contains(@class,'tab') and contains(text(),'密码')]",
        "//div[contains(@class,'login-tab') and contains(text(),'密码')]",
    ]
    for xpath in tab_selectors:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    el.click()
                    print(f"   [Tab] 点击: {xpath}")
                    tried = True
                    time.sleep(2)
                    break
        except Exception:
            pass

    # 策略2: 依次尝试多种输入框组合
    input_selectors = [
        ("//input[@id='login_form_email']", "//input[@id='login_form_password']"),
        ("//input[@type='text']", "//input[@type='password']"),
        ("//input[contains(@placeholder,'邮箱')]", "//input[contains(@placeholder,'密码')]"),
        ("//input[contains(@placeholder,'手机')]", "//input[contains(@placeholder,'密码')]"),
        ("//input[contains(@placeholder,'账号')]", "//input[contains(@placeholder,'密码')]"),
        ("//input[@name='username']", "//input[@name='password']"),
    ]

    login_btn_selectors = [
        "//button[contains(text(),'登录')]",
        "//button[contains(text(),'登 录')]",
        "//button[contains(@type,'submit')]",
        "//div[contains(@class,'login')]//button",
    ]

    username_el, password_el, login_btn = None, None, None

    for user_xp, pwd_xp in input_selectors:
        try:
            user_el = driver.find_element(By.XPATH, user_xp)
            pwd_el = driver.find_element(By.XPATH, pwd_xp)
            if user_el.is_displayed() and pwd_el.is_displayed():
                username_el = user_el
                password_el = pwd_el
                print(f"   [Input] 找到输入框: {user_xp}")
                break
        except Exception:
            continue

    for btn_xp in login_btn_selectors:
        try:
            btn = driver.find_element(By.XPATH, btn_xp)
            if btn.is_displayed():
                login_btn = btn
                print(f"   [Button] 找到登录按钮: {btn_xp}")
                break
        except Exception:
            continue

    if username_el and password_el and login_btn:
        try:
            username_el.clear()
            username_el.send_keys(username)
            time.sleep(0.5)
            password_el.clear()
            password_el.send_keys(password)
            time.sleep(0.5)
            login_btn.click()
            print("   ✅ 已提交账号密码登录")
            return
        except Exception as e:
            print(f"   ⚠️ 提交失败: {e}")

    # 兜底：用 JS 直接设置 React/Ant Design 受控组件的值
    try:
        driver.execute_script("""
            const emailInput = document.getElementById('login_form_email');
            const pwdInput = document.getElementById('login_form_password');
            if (emailInput && pwdInput) {
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputValueSetter.call(emailInput, arguments[0]);
                emailInput.dispatchEvent(new Event('input', { bubbles: true }));
                emailInput.dispatchEvent(new Event('change', { bubbles: true }));
                nativeInputValueSetter.call(pwdInput, arguments[1]);
                pwdInput.dispatchEvent(new Event('input', { bubbles: true }));
                pwdInput.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            return false;
        """, username, password)
        time.sleep(1)
        # 点击登录按钮
        if not login_btn:
            for btn_xp in login_btn_selectors:
                try:
                    btn = driver.find_element(By.XPATH, btn_xp)
                    if btn.is_displayed():
                        login_btn = btn
                        break
                except Exception:
                    continue
        if login_btn:
            login_btn.click()
            print("   ✅ 已通过 JS 兜底提交账号密码登录")
            return
    except Exception as e:
        print(f"   ⚠️ JS 兜底登录失败: {e}")

    if not tried:
        print("   ⚠️ 自动登录失败（页面结构变化），请手动登录")

# ==========================================
# 3. API 接口配置 (不变)
# ==========================================
API_MAP = {
    "ladder_trend_summary": { "url": "https://stock.quicktiny.cn/api/ladder?startDate={date_dash}&endDate={date_dash}", "type": "simple" },
    "ladder_hierarchy_detail": { "url": "https://stock.quicktiny.cn/api/ladder/day/{date_nodash}", "type": "simple" },
    "limit_up_filter": { "url": "https://stock.quicktiny.cn/api/limit-up/filter?date={date_nodash}&reasonTypeInput=&page={page}&limit=100", "type": "pagination", "data_key": "stocks" },
    "sector_heat_stats": { "url": "https://stock.quicktiny.cn/api/limit-up/filter/concept-tags-stats?date={date_nodash}", "type": "simple" },
    "market_sentiment_cycle": { "url": "https://stock.quicktiny.cn/api/ladder/cycle-analysis?startDate={date_dash}&endDate={date_dash}", "type": "simple" },
    "dragon_tiger_list": { "url": "https://stock.quicktiny.cn/api/admin/dragon-tiger-board-user?startDate={date_dash}&endDate={date_dash}&pageSize=100&stockCode=&stockName=&infoClassCode=&branchSearch=", "type": "simple" },
    "risk_monitor_list": { "url": "https://stock.quicktiny.cn/api/ladder/exchange-monitor/list?type=all", "type": "simple" }
}

# ==========================================
# 4. 智能爬虫类 (集成自愈逻辑)
# ==========================================
class StealthCrawler:
    def __init__(self):
        self.config = load_config()
        self.session = requests.Session()
        self.refresh_session_headers()

    def refresh_session_headers(self):
        headers = self.config["HEADERS"].copy()
        token = self.config.get("TOKEN", "")
        if token:
            headers["authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def random_sleep(self):
        sleep_time = random.uniform(2.5, 5.5) 
        print(f"   ☕ 休息 {sleep_time:.1f}s...", end="\r")
        time.sleep(sleep_time)

    def fetch_simple(self, url, retry_count=0):
        try:
            resp = self.session.get(url)
            
            # Token 过期处理
            if resp.status_code in (401, 403):
                if retry_count >= 1:
                    print("\n❌ Token 获取后依然无效，退出。")
                    sys.exit(1)
                    
                print(f"\n⚠️ 接口返回 401 (未授权) -> 触发自动登录流程")
                
                # 启动浏览器获取新 Token
                new_token = launch_browser_for_token()
                
                # 更新当前内存中的配置
                self.config["TOKEN"] = new_token
                self.refresh_session_headers()
                
                print("🔄 正在重试请求...")
                return self.fetch_simple(url, retry_count=1)
            
            if resp.status_code == 200:
                return resp.json()
            return None
            
        except Exception as e:
            print(f"\n❌ 网络请求异常: {e}")
            return None

    def fetch_pagination(self, url_template, data_key):
        all_items = []
        page = 1
        
        while True:
            current_url = url_template.replace("{page}", str(page))
            print(f"   [📄 第{page}页] ... ", end="")
            
            resp_json = self.fetch_simple(current_url)
            
            if not resp_json or "data" not in resp_json:
                print("停止(无数据)")
                break
                
            current_data = resp_json.get("data", {})
            if isinstance(current_data, dict):
                items = current_data.get(data_key, [])
                pagination_info = current_data.get("pagination", {})
                total_count = pagination_info.get("total", 0)
                page_size = pagination_info.get("pageSize", 100)
            else:
                items = []
                total_count = 0
                page_size = 100

            if not items:
                print("停止(空列表)")
                break
                
            all_items.extend(items)
            print(f"获取 {len(items)} 条 (累计 {len(all_items)})")
            
            total_pages = math.ceil(total_count / page_size)
            if page >= total_pages:
                break
            
            page += 1
            self.random_sleep()
            
        return {
            "success": True,
            "data": {
                data_key: all_items,
                "note": "Auto-merged by crawler"
            }
        }

    def _fallback_sector_heat_stats(self, date_nodash: str) -> dict | None:
        """当 quicktiny.cn sector_heat_stats 返回空数据时，用 AKShare 东方财富涨停池兜底。

        调用 ak.stock_zt_pool_em(date) 获取涨停池，按涨停原因分组统计，
        输出格式与 sector_heat_stats API 一致: {"success": True, "data": [{"name": ..., "count": ...}]}
        """
        try:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=date_nodash)
            if df is None or df.empty:
                return None
            # 按涨停原因/所属板块分组统计
            # akshare 返回的列名可能为 "涨停统计" 或 "所属行业"
            group_col = None
            for col in ("涨停统计", "所属行业", "概念", "limit_up_reason"):
                if col in df.columns:
                    group_col = col
                    break
            if group_col is None:
                # 兜底：用第一列作为分组依据
                return None
            counts = df.groupby(group_col).size().reset_index(name="count")
            counts = counts.sort_values("count", ascending=False)
            data = [
                {"name": str(row[group_col]), "count": int(row["count"])}
                for _, row in counts.iterrows()
            ]
            print(f"\n   [🔄 AKShare 兜底] sector_heat_stats 从东方财富涨停池获取 {len(data)} 个题材", end="")
            return {"success": True, "data": data, "source": "akshare_em_zt_pool_fallback"}
        except Exception as e:
            print(f"\n   [⚠️ AKShare 兜底失败] {e}", end="")
            return None

    @staticmethod
    def cached_payload_is_usable(api_key, payload):
        if not isinstance(payload, dict):
            return False
        if payload.get("note") and not payload.get("data"):
            return False

        if api_key == "limit_up_filter":
            data = payload.get("data") or {}
            return isinstance(data, dict) and bool(data.get("stocks"))
        if api_key in ("ladder_hierarchy_detail", "ladder_trend_summary"):
            return bool(payload.get("dates"))
        if api_key == "market_sentiment_cycle":
            return bool(payload.get("data"))
        return bool(payload.get("data")) or payload.get("success") is True

    def run_task(self, target_date):
        date_nodash = target_date.strftime("%Y%m%d")
        date_dash = target_date.strftime("%Y-%m-%d")

        day_dir = os.path.join(self.config["DATA_DIR"], date_dash)
        if not os.path.exists(day_dir):
            os.makedirs(day_dir)

        print(f"\n📅 处理日期: {date_dash}")

        if not is_trading_day(target_date):
            print(f"   [⏭️ 非交易日] 跳过，写入空标记")
            for api_key in API_MAP.keys():
                file_path = os.path.join(day_dir, f"{api_key}.json")
                if not os.path.exists(file_path):
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump({"success": True, "data": {}, "note": "非交易日"}, f, ensure_ascii=False)
            return

        for api_key, api_conf in API_MAP.items():
            file_path = os.path.join(day_dir, f"{api_key}.json")
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        cached_payload = json.load(f)
                    if self.cached_payload_is_usable(api_key, cached_payload):
                        print(f"   [✅ 已存在] {api_key}")
                        continue
                    print(f"   [♻️ 重新抓取] {api_key} 本地缓存为空或不完整")
                except Exception:
                    print(f"   [♻️ 重新抓取] {api_key} 本地缓存损坏")

            url_final = api_conf["url"].format(
                date_nodash=date_nodash,
                date_dash=date_dash,
                page="{page}"
            )

            try:
                print(f"   [📡 请求] {api_key} ... ", end="")

                final_data = None
                if api_conf["type"] == "pagination":
                    print("")
                    final_data = self.fetch_pagination(url_final, api_conf["data_key"])
                else:
                    final_data = self.fetch_simple(url_final)

                if final_data:
                    # sector_heat_stats 兜底：API 返回空数据时用 AKShare 东方财富涨停池替代
                    if api_key == "sector_heat_stats":
                        data_field = final_data.get("data")
                        if not data_field or (isinstance(data_field, list) and len(data_field) == 0):
                            fallback = self._fallback_sector_heat_stats(date_nodash)
                            if fallback:
                                final_data = fallback
                    print("成功")
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(final_data, f, ensure_ascii=False, indent=2)
                    self.random_sleep()
                else:
                    print("无数据，写入空标记")
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump({"success": True, "data": {}, "note": "无数据"}, f, ensure_ascii=False)

            except Exception as e:
                print(f"❌ 异常: {e}")

# ==========================================
# 5. 主入口
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantData 自动爬虫 (含自动登录)")
    parser.add_argument("--date", type=str, help="指定爬取日期 (格式 YYYY-MM-DD)")
    parser.add_argument("--backfill", type=int, help="向前回溯补录几天的数据", default=0)
    args = parser.parse_args()
    
    crawler = StealthCrawler()
    
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d")
        crawler.run_task(target)
    elif args.backfill > 0:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=args.backfill)
        curr = start_date
        while curr <= end_date:
            crawler.run_task(curr)
            curr += timedelta(days=1)
    else:
        target = datetime.now()
        print(f"🚀 默认抓取今天 ({target.strftime('%Y-%m-%d')})")
        crawler.run_task(target)
        
    print("\n🎉 任务结束。")
