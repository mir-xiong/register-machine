import concurrent.futures
import random
import re
import threading
import time
import requests
from fake_useragent import UserAgent
from loguru import logger

# --- 配置区 ---
referrer = "67fb4ba3f3b4c6199007c82b"  # !!! 重要: 替换为实的 Relingo 邀请码 !!!
MAX_SUCCESS = 100       # 程序自动停止前的最大成功注册次数
MAX_WORKERS = 3         # 并发注册线程数
TASK_DELAY = 1          # 批次内启动任务之间的延迟（秒）
BATCH_DELAY = 3         # 任务批次之间的延迟（秒） - 注意：当前主循环逻辑可能不严格使用此延迟
RELINGO_REQUESTS_PER_PROXY = 10 # 更换代理前，每个代理用于 Relingo API 调用的次数
PROXY_LIST_URL = "http://77.93.157.21:3030/fetch_all" # 获取代理列表的URL
REQUEST_TIMEOUT = 10    # 通用请求超时时间（秒）
PROXY_REQUEST_TIMEOUT = 10 # 使用代理进行请求时的特定超时时间（秒）
MAX_REGISTRATION_ATTEMPTS = 5 # 每个注册任务的最大尝试次数（每次尝试使用新邮箱）
MAIL_WAIT_TIMEOUT = 180 # 等待验证邮件的最长时间（秒）
MAIL_CHECK_INTERVAL = 3 # 检查邮件的间隔时间（秒）
MAIL_MAX_CHECKS = 15    # 在判定邮箱可能有问题前，最大检查邮件次数

# --- 全局变量 ---
success_counter = [0]   # 成功计数器 (使用列表以便在线程间共享)
fail_counter = [0]      # 失败计数器 (使用列表以便在线程间共享)
relingo_request_counter = [0] # Relingo API 调用计数器，用于代理轮换
current_proxy_index = [0]     # 当前使用的代理在列表中的索引
fetched_proxies = []          # 存储解析后的代理列表

# --- 线程锁 ---
success_counter_lock = threading.Lock() # 成功计数器锁
fail_counter_lock = threading.Lock()    # 失败计数器锁
proxy_management_lock = threading.Lock() # 管理代理索引和计数器的锁

# --- 代理获取与解析 ---
def fetch_proxies(url):
    """从指定URL获取代理列表字符串"""
    logger.info(f"尝试从 {url} 获取代理列表...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36"
        }
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        response.raise_for_status()  # 对错误的响应 (4xx 或 5xx) 抛出 HTTPError
        logger.success(f"成功获取代理列表 ({len(response.text)} 字节)。")
        return response.text
    except requests.exceptions.Timeout:
        logger.error(f"获取代理列表超时 ({REQUEST_TIMEOUT} 秒)。")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"获取代理列表失败: {e}")
        return None

def parse_proxies(proxy_string):
    """将逗号分隔的代理字符串解析为 requests 库可用的字典列表格式"""
    global fetched_proxies
    proxies_list = []
    if not proxy_string:
        logger.warning("代理字符串为空。将不使用代理。")
        fetched_proxies = []
        return

    raw_proxies = proxy_string.strip().split(',')
    logger.info(f"正在解析 {len(raw_proxies)} 个原始代理...")
    for proxy_address in raw_proxies:
        proxy_address = proxy_address.strip()
        if not proxy_address:
            continue

        # 对 IP:Port 格式进行基本检查 (可以改进)
        # 同时检查是否以已知协议 (http, https, socks) 开头
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{1,5}$", proxy_address.split('@')[-1]) and \
           not re.match(r"^(http|https|socks\d?)://", proxy_address):
                logger.warning(f"跳过无效的代理格式: {proxy_address}")
                continue

        proxy_dict = {}
        if proxy_address.startswith("socks"):
            proxy_dict = {'http': proxy_address, 'https': proxy_address}
        elif proxy_address.startswith("http"): # 包括 http 和 https
            proxy_dict = {'http': proxy_address, 'https': proxy_address}
        else:
            # 如果未提供协议，则假定为 http
            logger.debug(f"假设代理 {proxy_address} 使用 http:// 协议")
            proxy_full_address = f"http://{proxy_address}"
            proxy_dict = {'http': proxy_full_address, 'https': proxy_full_address}

        if proxy_dict:
            proxies_list.append(proxy_dict)

    if not proxies_list:
         logger.warning("未能从获取的列表中解析出任何有效的代理。")
    else:
        logger.success(f"成功解析了 {len(proxies_list)} 个有效代理。")

    fetched_proxies = proxies_list # 更新全局代理列表

def get_current_proxy():
    """根据轮换逻辑获取当前代理。如果没有代理则返回 None。"""
    global current_proxy_index, relingo_request_counter, fetched_proxies

    with proxy_management_lock: # 确保线程安全地访问和修改代理状态
        if not fetched_proxies:
            # logger.debug("无可用代理。")
            return None # 没有获取或解析到代理

        # 增加请求计数器
        relingo_request_counter[0] += 1
        request_count = relingo_request_counter[0]

        # 检查是否到了更换代理的时候
        # 在触发计数的请求 *之前* 进行切换 (当 request_count - 1 是 RELINGO_REQUESTS_PER_PROXY 的倍数时)
        if request_count > 1 and (request_count - 1) % RELINGO_REQUESTS_PER_PROXY == 0 :
             old_index = current_proxy_index[0]
             current_proxy_index[0] = (current_proxy_index[0] + 1) % len(fetched_proxies) # 循环使用代理
             logger.info(f"Relingo 请求计数: {request_count-1}。切换代理，从索引 {old_index} 到 {current_proxy_index[0]}")

        proxy_to_use = fetched_proxies[current_proxy_index[0]]
        # logger.debug(f"使用代理: {proxy_to_use} (索引: {current_proxy_index[0]}, 请求计数: {request_count})")
        return proxy_to_use


# --- 单词生成器 (与原版一致，未作修改) ---
class WordGenerator:
    def __init__(self):
        self.consonants = "bcdfghjklmnpqrstvwxyz" # 常用辅音字母
        self.vowels = "aeiou"                     # 元音字母
        self.common_pairs = ["th", "ch", "sh", "ph", "wh", "br", "cr", "dr", "fr", "gr", "pr", "tr"] # 常用字母组合
        self.common_endings = ["ing", "ed", "er", "est", "ly", "tion", "ment"] # 常用词尾
        self.username_suffixes = ["153", "862", "east", "le", "us", "apple", "dev", "lucky", "best"] # 常用用户名后缀

    def generate_syllable(self):
        """生成一个音节"""
        if random.random() < 0.3 and self.common_pairs:  # 30% 概率使用常用字母组合
            return random.choice(self.common_pairs) + random.choice(self.vowels)
        else:
            return random.choice(self.consonants) + random.choice(self.vowels)

    def generate_word(self, min_length=4, max_length=8):
        """生成一个随机单词"""
        word = ""
        target_length = random.randint(min_length, max_length)
        while len(word) < target_length - 2: # 添加音节直到达到目标长度附近
            word += self.generate_syllable()
        if random.random() < 0.3 and len(word) < max_length - 2 and self.common_endings: # 可能添加常用词尾
             word += random.choice(self.common_endings)
        elif len(word) < target_length: # 如果长度还不够，补一个辅音
            word += random.choice(self.consonants)
        return word.lower()

    def generate_random_username(self, min_length=3, max_length=8):
        """生成随机用户名"""
        username = self.generate_word(min_length, max_length)
        if random.random() < 0.5: # 50% 的概率添加数字或特殊后缀
            if random.random() < 0.7: # 70% 概率添加数字
                username += str(random.randint(0, 999)).zfill(random.randint(2, 3))
            else: # 30% 概率添加特殊后缀
                 if self.username_suffixes:
                    username += random.choice(self.username_suffixes)
        return username

    def generate_combined_username(self, num_words=1, separator="_"):
        """生成完整的组合用户名"""
        base_username = self.generate_random_username() # 首先生成基础用户名
        words = [self.generate_word() for _ in range(num_words)] # 生成额外的随机单词
        if random.random() < 0.5: # 随机决定用户名放在前面还是后面
            words.append(base_username)
        else:
            words.insert(0, base_username)
        return separator.join(words) # 使用分隔符连接


# --- 使用SMTP.dev替代Mail.tm的邮箱客户端 ---
class MailTmClient:
    baseurl = "https://api.smtp.dev"
    api_key = ""  # 请替换为你的实际API密钥

    def __init__(self, user=None):
        # 初始化token为None，防止未设置时引发属性错误
        self.token = None
        self.acount = None
        self.headers = {
            "X-API-KEY": self.api_key,
            "Accept": "application/json"
        }
        self.mailboxid = None
        self.accountid = None

        if user is None:
            generator = WordGenerator()
            user = generator.generate_combined_username(1)

        # 重试机制
        for attempt in range(3):
            try:
                domain = self.get_domains()
                if not domain:
                    logger.warning(f"尝试 {attempt + 1}/3: 获取域名失败，重试中...")
                    time.sleep(2)
                    continue

                logger.info("Get domain:" + domain)
                self.acount = user + "@" + domain
                logger.info("Get acount:" + self.acount)

                account_result = self.acounts(self.acount)
                if not account_result:
                    logger.warning(f"尝试 {attempt + 1}/3: 创建账户失败，重试中...")
                    time.sleep(2)
                    continue
                
                self.accountid = account_result["id"]
                logger.info("Get accountid:" + self.accountid)
                mailboxes = account_result["mailboxes"]
                for mailbox in mailboxes:
                    if mailbox["path"] == "INBOX":
                        self.mailboxid = mailbox["id"]
                        logger.info("Get mailboxid:" + self.mailboxid)
                        break
                if not self.mailboxid:
                    logger.error("未找到INBOX邮箱")
                    continue
            
                token_result = self.get_token()
                if not token_result:
                    logger.warning(f"尝试 {attempt + 1}/3: 获取令牌失败，重试中...")
                    time.sleep(2)
                    continue

                # 成功初始化
                break
            except Exception as e:
                logger.error(f"初始化邮箱客户端出错 (尝试 {attempt + 1}/3): {str(e)}")
                if attempt < 2:  # 如果不是最后一次尝试，则等待后重试
                    time.sleep(3)
                else:
                    raise Exception(f"初始化邮箱客户端失败，已重试3次: {str(e)}")

    def get_email(self):
        return self.acount

    def get_domains(self):
        return ""  # 请替换为你的实际域名

    def acounts(self, acount):
        try:
            json_data = {
                "address": acount,
                "password": "thisispassword",
            }
            headers = {
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            response = requests.post(
                f"{self.baseurl}/accounts", headers=headers, json=json_data, timeout=10
            )

            if response.status_code != 201 and response.status_code != 200:
                logger.error(f"创建账户失败: 状态码 {response.status_code}")
                if response.text:
                    logger.info("acounts:" + response.text)
                return None

            if not response.text:
                logger.error("创建账户失败: 空响应")
                return None

            logger.info("acounts:" + response.text)
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"创建账户请求出错: {str(e)}")
            return None
        except ValueError as e:
            logger.error(f"解析账户响应出错: {str(e)}")
            return None

    def get_token(self):
        try:
            response = requests.get(
                f"{self.baseurl}/tokens", headers=self.headers, timeout=10
            )

            if response.status_code != 200:
                logger.error(f"获取令牌失败: 状态码 {response.status_code}")
                if response.text:
                    logger.info("get_token:" + response.text)
                return None

            if not response.text:
                logger.error("获取令牌失败: 空响应")
                return None

            logger.info("get_token:" + response.text)
            response_data = response.json()

            self.token = response_data[0]["id"]
            logger.info(f"token: {self.token}")
            return self.token
        except requests.exceptions.RequestException as e:
            logger.error(f"获取令牌请求出错: {str(e)}")
            return None
        except ValueError as e:
            logger.error(f"解析令牌响应出错: {str(e)}")
            return None

    def get_message(self):  # 重命名为与原代码匹配
        try:
            # 先检查token是否存在
            if not hasattr(self, "token") or self.token is None:
                logger.error("获取消息失败: token未初始化")
                return None

            response = requests.get(
                f"{self.baseurl}/accounts/{self.accountid}/mailboxes/{self.mailboxid}/messages", 
                headers=self.headers, 
                timeout=10
            )

            if response.status_code != 200:
                logger.error(f"获取消息失败: 状态码 {response.status_code}")
                return None

            response_data = response.json()

            if response_data:  
                logger.info("Get message:" + response_data[0]["intro"])
                return response_data[0]["intro"]
            else:
                return None
        except Exception as e:
            logger.error(f"获取消息时出错: {str(e)}")
            return None

    def wait_getmessage(self, max_wait_time=MAIL_WAIT_TIMEOUT):
        """等待并获取验证邮件的消息简介，带有超时和重试逻辑"""
        # 先检查token是否初始化
        if not hasattr(self, "token") or self.token is None:
            logger.error("等待消息失败: token未初始化")
            return None

        start_time = time.time()
        check_count = 0
        logger.info(f"正在等待 {self.acount} 的验证邮件 (最多 {max_wait_time} 秒)...")

        while True:
            try:
                message = self.get_message()
                if message is not None:
                    logger.success(f"在 {time.time() - start_time:.1f} 秒后收到 {self.acount} 的邮件简介。")
                    return message

                # 检查是否超时
                elapsed_time = time.time() - start_time
                if elapsed_time > max_wait_time:
                    logger.error(f"超时: 为 {self.acount} 等待邮件 {max_wait_time:.0f} 秒，但未收到。")
                    return None

                check_count += 1
                if check_count > MAIL_MAX_CHECKS:
                    logger.warning(f"已为 {self.acount} 检查邮件 {check_count} 次但未成功。邮件发送可能延迟或失败。")

                logger.info(f"等待邮件中... ({check_count}/{MAIL_MAX_CHECKS})")
                time.sleep(MAIL_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"在为 {self.acount} 检查邮件的循环中出错: {e}")
                if "token" in str(e).lower() or "401" in str(e):
                    logger.error("邮件检查期间发生致命错误 (令牌/认证问题?)。中止等待。")
                    return None
                time.sleep(MAIL_CHECK_INTERVAL * 2)

    def get_all_accounts(self):
        response = requests.get(
            f"{self.baseurl}/accounts", headers=self.headers, timeout=10
        )
        return response.json()

    def delete_account(self, accountid):
        headers = {
            "X-API-KEY": self.api_key
        }
        response = requests.delete(
            f"{self.baseurl}/accounts/{accountid}", headers=headers, timeout=10
        )

    def delete_all_accounts(self):
        accounts = self.get_all_accounts()
        for account in accounts:
            time.sleep(3)
            self.delete_account(account["id"])
            logger.info(f"删除账户 {account['id']} 成功") 


# --- Relingo 注册客户端 (已修改以支持代理) ---
# --- Relingo 注册客户端 (已修改以支持代理和兼容旧版 fake-useragent) ---
class RelingoReg:
    def __init__(self):
        # 首先初始化 MailTmClient - 这可能会抛出异常
        try:
            self.mm = MailTmClient()
            self.email = self.mm.get_email() # 获取生成的临时邮箱
            if not self.email:
                raise Exception("无法从 MailTmClient 获取邮箱地址。")
        except Exception as e:
             logger.error(f"初始化 MailTmClient 失败: {e}")
             # 抛出异常，以便 register_task 知道这次尝试失败了
             raise Exception(f"初始化 MailTmClient 失败: {e}")

        # 创建 UserAgent 对象，移除了旧版本不支持的参数
        try:
            # 尝试用简单的方式初始化，适用于大多数版本
            ua = UserAgent(platforms="desktop")
            random_ua = ua.random # 获取一个随机 User-Agent
        except Exception as ua_error:
            logger.warning(f"初始化 UserAgent 时出错: {ua_error}. 将使用默认 UA。")
            # 提供一个备用的 User-Agent
            random_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"


        # 设置请求头
        self.headers = {
            "authority": "api.relingo.net",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7", # 示例 accept-language
            "content-type": "application/json",
            "origin": "chrome-extension://dpphkcfmnbkdpmgneljgdhfnccnhmfig", # 如果 API 要求，保持不变
            "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="90", "Google Chrome";v="90"', # 示例
            "sec-ch-ua-mobile": "?0", # 表示非移动设备
            "sec-ch-ua-platform": '"Windows"', # 示例平台
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors", # 跨域请求模式
            "sec-fetch-site": "none", # 从非同源站点发起请求
            "user-agent": random_ua, # 使用随机生成的 User-Agent
            # 如果需要，添加其他观察到的或必要的请求头
            "x-relingo-dest-lang": "en", # Relingo 特定头
            "x-relingo-lang": "zh",      # Relingo 特定头
            "x-relingo-platform": "extension", # Relingo 特定头
            "x-relingo-referrer": "https://relingo.net/en/try?relingo-drawer=account", # Relingo 特定头
            "x-relingo-version": "3.16.6", # Relingo 特定头 (考虑是否需要更新)
        }
        # Cookie 可能由 session 管理，或在需要时为每个请求单独设置
        self.cookies = {"referrer": referrer} if referrer else {} # 如果设置了邀请码，则添加到 Cookie

    def _make_relingo_request(self, method, url, **kwargs):
        """辅助方法，用于向 Relingo API 发送请求，处理代理逻辑"""
        current_proxy = get_current_proxy() # 获取当前轮换到的代理
        try:
            response = requests.request(
                method, # 请求方法 (GET, POST, etc.)
                url,    # 请求 URL
                proxies=current_proxy, # 设置代理 (如果 current_proxy 为 None 则不使用代理)
                timeout=PROXY_REQUEST_TIMEOUT if current_proxy else REQUEST_TIMEOUT, # 如果使用代理，则应用代理超时时间
                **kwargs # 传递其他参数，如 headers, json, cookies
            )
            response.raise_for_status() # 检查 HTTP 错误 (4xx, 5xx)
            return response # 返回成功的响应对象
        except requests.exceptions.Timeout:
             logger.error(f"Relingo API 请求 {method} {url} 超时 ({PROXY_REQUEST_TIMEOUT if current_proxy else REQUEST_TIMEOUT}秒)。代理: {current_proxy}")
             return None
        except requests.exceptions.ProxyError as e:
            logger.error(f"Relingo API 请求 {method} {url} 因代理错误失败。代理: {current_proxy}。错误: {e}")
            # 可选: 在此处添加逻辑来标记此代理为无效?
            return None
        except requests.exceptions.HTTPError as e: # 处理 HTTP 错误
             logger.error(f"Relingo API 请求 {method} {url} 失败: 状态码 {e.response.status_code}。代理: {current_proxy}。响应: {e.response.text[:200]}")
             return None
        except requests.exceptions.RequestException as e: # 处理其他请求相关的异常
            logger.error(f"Relingo API 请求 {method} {url} 失败。代理: {current_proxy}。错误: {e}")
            return None

    def send_code(self):
        """向 Relingo API 发送发送验证码的请求"""
        logger.info(f"正在向 {self.email} 发送验证码...")
        json_data = {"email": self.email, "type": "LOGIN"} # 假设 LOGIN 是注册验证的正确类型

        # 使用辅助方法发送请求
        response = self._make_relingo_request(
            method="post",
            url="https://api.relingo.net/api/sendPasscode",
            cookies=self.cookies,
            headers=self.headers,
            json=json_data
        )

        if response:
            logger.success(f"已成功为 {self.email} 发送验证码请求。响应: {response.text[:100]}")
            return True
        else:
            # 错误已在 _make_relingo_request 中记录
            return False

    def register(self, code):
        """使用邮箱和验证码向 Relingo API 发送注册/登录请求"""
        logger.info(f"尝试使用验证码 {code} 为 {self.email} 进行注册...")
        json_data = {
            "type": "PASSCODE", # 使用验证码类型
            "email": self.email,
            "code": code,
            # 仅当 referrer 有值时才包含它
            **({"referrer": referrer} if referrer else {})
        }

        # 使用辅助方法发送请求
        response = self._make_relingo_request(
            method="post",
            url="https://api.relingo.net/api/login", # 端点似乎是 login，通过 PASSCODE 类型处理注册
            cookies=self.cookies,
            headers=self.headers,
            json=json_data
        )

        if response:
            # 可以添加更具体的成功检查 (例如检查响应体内容)
            logger.success(f"{self.email} 注册/登录成功。响应: {response.text[:100]}")
            return True
        else:
            # 错误已在 _make_relingo_request 中记录
            return False

    def start(self):
        """运行单个邮箱的完整注册流程"""
        # 确保 email 属性存在
        if not hasattr(self, 'email') or not self.email:
            logger.error("RelingoReg 对象未成功初始化邮箱地址，无法启动注册流程。")
            return False
        try:
            # 1. 发送验证码
            if not self.send_code():
                logger.error(f"为 {self.email} 发送验证码失败。")
                return False

            # 2. 等待并获取邮件简介
            message_intro = self.mm.wait_getmessage(max_wait_time=MAIL_WAIT_TIMEOUT)
            if not message_intro:
                logger.error(f"在超时时间内未收到 {self.email} 的验证邮件。")
                return False

            logger.debug(f"收到 {self.email} 的邮件简介: '{message_intro}'")

            # 3. 从邮件简介中提取验证码 (第一串数字)
            match = re.search(r"\d+", message_intro)
            if match:
                code = match.group(0)
                logger.info(f"为 {self.email} 提取到验证码 '{code}'。")
                # 4. 使用验证码进行注册
                return self.register(code)
            else:
                logger.error(f"无法从简介中提取验证码: '{message_intro}'")
                return False
        except Exception as e:
            # 捕获在 start() 内部与 MailTmClient 交互时可能出现的意外错误
            logger.error(f"{getattr(self, 'email', '未知邮箱')} 注册过程中发生意外错误: {e}")
            return False



# --- 注册任务函数 ---
def register_task(task_id):
    """执行一次注册尝试，包括使用新邮箱进行重试"""
    global success_counter, fail_counter # 访问全局计数器

    logger.info(f"任务 {task_id}: 开始注册尝试...")

    for attempt in range(MAX_REGISTRATION_ATTEMPTS): # 循环尝试最多 MAX_ATTEMPTS 次
        logger.debug(f"任务 {task_id}: 尝试 {attempt + 1}/{MAX_REGISTRATION_ATTEMPTS}")
        try:
            # 每次尝试都创建一个新的 RelingoReg 实例 (这会内部初始化 MailTmClient 并获取新邮箱)
            relingo_reg = RelingoReg()

            # 运行完整的注册流程
            result = relingo_reg.start()

            if result: # 如果 start() 返回 True，表示成功
                with success_counter_lock: # 锁定以安全地增加计数器
                    success_counter[0] += 1
                    current_success = success_counter[0]
                logger.success(f"任务 {task_id}: 注册成功! (邮箱: {relingo_reg.email})。总成功数: {current_success}")
                return True # 任务成功完成
            else: # 如果 start() 返回 False，表示失败
                logger.warning(f"任务 {task_id}: 第 {attempt + 1} 次注册尝试失败 (邮箱: {getattr(relingo_reg, 'email', 'N/A')})。如果可能将重试...")
                # 此处不添加额外睡眠，让循环快速处理重试

        except Exception as e:
            # 捕获在实例化 RelingoReg 或调用其方法期间发生的错误
            logger.error(f"任务 {task_id}: 尝试 {attempt + 1} 期间出错: {e}", exc_info=False) # exc_info=False 避免在日志中重复打印完整的堆栈跟踪
            # 可选: 在出错后添加短暂延迟
            time.sleep(1)

        # 如果循环完成所有尝试但仍未成功
        if attempt == MAX_REGISTRATION_ATTEMPTS - 1:
            logger.error(f"任务 {task_id}: 尝试 {MAX_REGISTRATION_ATTEMPTS} 次后注册失败。")
            with fail_counter_lock: # 锁定以安全地增加失败计数器
                fail_counter[0] += 1
            return False # 任务最终失败

    return False # 理论上不应到达这里，但为清晰起见添加


# --- 主执行入口 ---
if __name__ == "__main__":
    # --- 日志记录器设置 ---
    logger.remove() # 移除默认处理器
    # 定义日志格式
    log_format = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    # 添加控制台输出处理器 (INFO 级别及以上)
    logger.add(lambda msg: print(msg, end=""), level="INFO", format=log_format, colorize=True)
    # 添加文件输出处理器 (DEBUG 级别及以上)
    logger.add("relingo_reg_{time}.log", rotation="50 MB", level="DEBUG", format=log_format, encoding="utf-8")

    logger.info("--- Relingo 自动注册脚本 ---")
    logger.info(f"邀请码: {'已设置' if referrer else '未设置'}")
    logger.info(f"最大成功目标: {MAX_SUCCESS}")
    logger.info(f"最大工作线程数: {MAX_WORKERS}")
    logger.info(f"代理列表 URL: {PROXY_LIST_URL}")
    logger.info(f"每个代理的请求数: {RELINGO_REQUESTS_PER_PROXY}")
    logger.info("按 Ctrl+C 停止程序。")

    # --- 获取初始代理列表 ---
    proxy_string = fetch_proxies(PROXY_LIST_URL) # 从 URL 获取代理字符串
    parse_proxies(proxy_string) # 解析代理字符串
    if not fetched_proxies:
        logger.warning("未能加载任何代理，将不使用代理继续运行。")
    else:
         logger.info(f"已加载 {len(fetched_proxies)} 个代理。代理轮换已启用。")


    # --- 主循环 ---
    task_id_counter = 0 # 任务 ID 计数器
    try:
        # 创建线程池执行器
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = set() # 使用集合存储 Future 对象，方便管理
            while True:
                # 在安排新任务前检查是否已达到成功上限
                with success_counter_lock:
                    if success_counter[0] >= MAX_SUCCESS:
                        logger.success(f"已达到 {MAX_SUCCESS} 次成功注册的目标。正在停止...")
                        break # 跳出主循环

                # 如果线程池有空闲，提交新任务，直到达到 MAX_WORKERS
                while len(futures) < MAX_WORKERS:
                     task_id_counter += 1
                     logger.debug(f"向执行器提交任务 {task_id_counter}...")
                     future = executor.submit(register_task, task_id_counter) # 提交任务
                     futures.add(future) # 将 Future 对象添加到集合中
                     time.sleep(TASK_DELAY) # 在启动下一个任务前稍作延迟


                # 等待至少一个任务完成，然后处理已完成的任务
                # concurrent.futures.wait 会阻塞，直到满足 return_when 条件
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)

                # 可选: 检查已完成任务的结果或异常
                # for future in done:
                #    try:
                #       future.result() # 获取任务结果，如果任务中发生异常，这里会重新抛出
                #    except Exception as e:
                #       logger.error(f"任务抛出了未处理的异常: {e}")


                # 记录当前状态 (可以根据需要调整记录频率)
                logger.info(f"状态更新 -> 成功: {success_counter[0]} | 失败: {fail_counter[0]} | 活动任务: {len(futures)} | Relingo请求数: {relingo_request_counter[0]}")


                # 可选: 如果需要，可以在检查/添加任务之间添加延迟
                # time.sleep(BATCH_DELAY) # 这是旧的批处理延迟逻辑

    except KeyboardInterrupt: # 捕获 Ctrl+C 中断信号
        logger.info("检测到 Ctrl+C。正在关闭...")
        # 可选: 向正在运行的任务发送停止信号 (这比较复杂)
        # executor.shutdown(wait=True) # 等待当前正在运行的任务完成

    except Exception as e: # 捕获主循环中的其他意外错误
        logger.error(f"主循环中发生意外错误: {e}", exc_info=True) # exc_info=True 会记录完整的堆栈跟踪

    finally: # 无论如何都会执行的部分
        logger.info("--- 脚本执行完毕 ---")
        logger.info(f"总成功注册次数: {success_counter[0]}")
        logger.info(f"总失败注册任务数: {fail_counter[0]}")
        logger.info(f"总 Relingo API 请求次数 (大约): {relingo_request_counter[0]}")
        logger.info("日志已保存到 relingo_reg_*.log 文件。")

    # 可选：清理所有临时邮箱账户
    # try:
    #     logger.info("开始清理所有临时邮箱账户...")
    #     clean_client = MailTmClient()
    #     clean_client.delete_all_accounts()
    #     logger.success("所有临时邮箱账户清理完成。")
    # except Exception as e:
    #     logger.error(f"清理临时邮箱账户时出错: {e}")
