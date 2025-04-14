import concurrent.futures
import random
import re
import threading
import time
import requests
from fake_useragent import UserAgent
from loguru import logger

# --- 配置区 ---
referrer = ""  # !!! 重要: 请替换为你的 Relingo 邀请码 !!!
MAX_SUCCESS = 100       # 程序自动停止前的最大成功注册次数
MAX_WORKERS = 3         # 并发注册线程数
TASK_DELAY = 1          # 批次内启动任务之间的延迟（秒）
BATCH_DELAY = 3         # 任务批次之间的延迟（秒） - 注意：当前主循环逻辑可能不严格使用此延迟
RELINGO_REQUESTS_PER_PROXY = 10 # 更换代理前，每个代理用于 Relingo API 调用的次数
PROXY_LIST_URL = "https://getproxy.bzpl.tech/get/" # !!! 修改: 新的代理获取 URL !!!
REQUEST_TIMEOUT = 10    # 通用请求超时时间（秒）
PROXY_REQUEST_TIMEOUT = 10 # 使用代理进行请求时的特定超时时间（秒）
MAX_REGISTRATION_ATTEMPTS = 5 # 每个注册任务的最大尝试次数（每次尝试使用新邮箱）
MAIL_WAIT_TIMEOUT = 180 # 等待验证邮件的最长时间（秒）
MAIL_CHECK_INTERVAL = 3 # 检查邮件的间隔时间（秒）
MAIL_MAX_CHECKS = 15    # 在判定邮箱可能有问题前，最大检查邮件次数

# --- 全局变量 ---
success_counter = [0]   # 成功计数器 (使用列表以便在线程间共享)
fail_counter = [0]      # 失败计数器 (使用列表以便在线程间共享)
# !!! 新增: 存储当前代理信息和剩余使用次数
current_proxy_info = [{'proxy_dict': None, 'uses_left': 0}] # 存储格式化后的代理字典和剩余次数

# --- 线程锁 ---
success_counter_lock = threading.Lock() # 成功计数器锁
fail_counter_lock = threading.Lock()    # 失败计数器锁
proxy_management_lock = threading.Lock() # !!! 修改: 现在管理 current_proxy_info

# --- 代理获取与解析 (替换旧函数) ---
def fetch_single_proxy(url):
    """从指定URL获取单个 SOCKS5 代理地址字符串"""
    logger.info(f"尝试从 {url} 获取单个新代理...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36"
        }
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
        response.raise_for_status()  # 对错误的响应 (4xx 或 5xx) 抛出 HTTPError

        data = response.json()
        proxy_address = data.get("proxy") # 安全地获取 'proxy' 字段

        if proxy_address:
            # 基本格式检查 (IP:Port)
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{1,5}$", proxy_address):
                logger.success(f"成功获取 SOCKS5 代理地址: {proxy_address}")
                # 检查是否为 https 代理 (虽然 API 文档说不是，但做个检查)
                if data.get("https", False):
                     logger.warning(f"代理 {proxy_address} 被标记为 HTTPS，但我们将按 SOCKS5 处理。")
                return proxy_address
            else:
                logger.error(f"获取的代理地址格式无效: {proxy_address}")
                return None
        else:
            logger.error(f"获取代理失败: 响应 JSON 中缺少 'proxy' 字段。响应: {data}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"获取代理超时 ({REQUEST_TIMEOUT} 秒)。")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"获取代理时发生请求错误: {e}")
        return None
    except ValueError: # 包括 JSONDecodeError
         logger.error(f"获取代理失败: 无法解析 JSON 响应。响应文本: {response.text[:200]}")
         return None
    except Exception as e:
         logger.error(f"获取代理时发生未知错误: {e}")
         return None

def get_current_proxy():
    """根据使用次数轮换逻辑获取当前 SOCKS5 代理。如果无可用代理则返回 None。"""
    global current_proxy_info # 需要访问全局变量来修改

    with proxy_management_lock: # 确保线程安全地访问和修改代理状态
        info = current_proxy_info[0] # 获取包含代理信息和剩余次数的字典

        # 检查当前代理是否还有剩余次数
        if info['uses_left'] > 0 and info['proxy_dict']:
            info['uses_left'] -= 1
            # logger.debug(f"复用现有代理: {info['proxy_dict']}, 剩余次数: {info['uses_left']}")
            return info['proxy_dict']
        else:
            # 如果没有剩余次数或代理无效，则获取新代理
            if info['proxy_dict']:
                 logger.info(f"当前代理 {list(info['proxy_dict'].values())[0]} 已用尽 ({RELINGO_REQUESTS_PER_PROXY} 次)，尝试获取新代理...")
            else:
                 logger.info("无可用代理或首次运行，尝试获取新代理...")

            new_proxy_address = fetch_single_proxy(PROXY_LIST_URL) # 获取原始地址 "ip:port"

            if new_proxy_address:
                # 格式化为 requests 需要的 SOCKS5 代理字典
                socks5_proxy_url = f"socks5://{new_proxy_address}"
                new_proxy_dict = {'http': socks5_proxy_url, 'https': socks5_proxy_url}

                # 更新全局代理信息
                info['proxy_dict'] = new_proxy_dict
                # 重置剩余次数 (减去当前这次使用)
                info['uses_left'] = RELINGO_REQUESTS_PER_PROXY - 1
                logger.success(f"已获取并切换到新代理: {new_proxy_dict}，剩余可用次数: {info['uses_left']}")
                return new_proxy_dict
            else:
                # 获取新代理失败
                logger.error("无法获取新代理。本次请求将不使用代理。")
                info['proxy_dict'] = None # 标记当前无有效代理
                info['uses_left'] = 0
                return None # 返回 None，表示此次请求不使用代理

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
    # !!! 重要: 请替换为你的实际 API 密钥 !!!
    api_key = ""

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
                logger.debug(f"MailTmClient 尝试 {attempt + 1}/3: 获取域名...")
                domain = self.get_domains()
                if not domain:
                    logger.warning(f"尝试 {attempt + 1}/3: 获取域名失败，重试中...")
                    time.sleep(2)
                    continue
                logger.info("获取到域名:" + domain)

                self.acount = user + "@" + domain
                logger.info("生成的邮箱账号:" + self.acount)

                logger.debug(f"尝试 {attempt + 1}/3: 创建账号 {self.acount}...")
                account_result = self.acounts(self.acount)
                if not account_result:
                    logger.warning(f"尝试 {attempt + 1}/3: 创建账户失败，重试中...")
                    time.sleep(2)
                    continue
                logger.info("创建/检查账户成功。")

                self.accountid = account_result["id"]
                logger.info("获取到 accountid:" + self.accountid)
                mailboxes = account_result["mailboxes"]
                for mailbox in mailboxes:
                    if mailbox["path"] == "INBOX":
                        self.mailboxid = mailbox["id"]
                        logger.info("获取到 mailboxid:" + self.mailboxid)
                        break
                if not self.mailboxid:
                    logger.error("未找到INBOX邮箱")
                    # 在重试循环中继续，而不是退出
                    time.sleep(2)
                    continue

                logger.debug(f"尝试 {attempt + 1}/3: 获取 {self.acount} 的令牌...")
                token_result = self.get_token()
                if not token_result:
                    logger.warning(f"尝试 {attempt + 1}/3: 获取令牌失败，重试中...")
                    time.sleep(2)
                    continue
                logger.info(f"成功获取 {self.acount} 的令牌。")

                # 成功初始化
                break
            except Exception as e:
                logger.error(f"初始化邮箱客户端出错 (尝试 {attempt + 1}/3): {str(e)}")
                if attempt < 2:  # 如果不是最后一次尝试，则等待后重试
                    time.sleep(3)
                else: # 最后一次尝试失败，抛出异常
                    raise Exception(f"MailTmClient 初始化失败，已重试3次: {str(e)}")

        if not self.token or not self.acount or not self.accountid or not self.mailboxid: # 检查是否成功初始化
            raise Exception("MailTmClient 初始化失败: 未能成功创建令牌或获取必要 ID。")
        logger.debug("MailTmClient 初始化成功。")


    def get_email(self):
        return self.acount

    def get_domains(self):
        # !!! 重要: 请替换为你的实际域名 !!!
        return "666.com"

    def acounts(self, acount):
        try:
            json_data = {
                "address": acount,
                "password": "thisispassword", # 固定密码
            }
            headers = {
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            # 注意：SMTP.dev API 请求不使用外部代理
            response = requests.post(
                f"{self.baseurl}/accounts", headers=headers, json=json_data, timeout=REQUEST_TIMEOUT
            )

            # 成功创建是 201，账户已存在是 200
            if response.status_code not in [201, 200]:
                logger.error(f"为 {acount} 创建/检查账户失败: 状态码 {response.status_code}, 响应: {response.text[:200]}")
                return None
            elif response.status_code == 200:
                 logger.warning(f"账户 {acount} 已存在 (状态码 200)。继续获取信息。")

            if not response.text:
                logger.error(f"为 {acount} 创建/检查账户失败: 空响应")
                return None

            # logger.info("acounts 响应:" + response.text[:200]) # 可能过于详细
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"为 {acount} 创建/检查账户时请求出错: {str(e)}")
            return None
        except ValueError as e: # JSONDecodeError
            logger.error(f"解析 {acount} 的账户创建响应时出错: {str(e)}")
            return None

    def get_token(self):
        try:
            # 注意：SMTP.dev API 请求不使用外部代理
            response = requests.get(
                f"{self.baseurl}/tokens", headers=self.headers, timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                logger.error(f"为 {self.acount} 获取令牌失败: 状态码 {response.status_code}")
                if response.text:
                    logger.debug("get_token 响应:" + response.text[:200])
                return None

            if not response.text:
                logger.error(f"为 {self.acount} 获取令牌失败: 空响应")
                return None

            # logger.info("get_token 响应:" + response.text[:200]) # 可能过于详细
            response_data = response.json()

            # 假设我们总是使用第一个令牌
            if response_data and len(response_data) > 0 and "id" in response_data[0]:
                self.token = response_data[0]["id"]
                # logger.info(f"获取到令牌 ID: {self.token}") # 可能过于详细
                return self.token
            else:
                 logger.error(f"为 {self.acount} 获取令牌失败: 响应中未找到有效的令牌 ID。 数据: {response_data}")
                 return None

        except requests.exceptions.RequestException as e:
            logger.error(f"为 {self.acount} 获取令牌请求出错: {str(e)}")
            return None
        except ValueError as e: # JSONDecodeError
            logger.error(f"为 {self.acount} 解析令牌响应出错: {str(e)}")
            return None

    def get_message(self):
        """获取邮箱中的第一条消息简介 (intro)"""
        try:
            # 先检查必要 ID 和 token 是否存在
            if not self.token or not self.accountid or not self.mailboxid:
                logger.error("获取消息失败: 客户端未完全初始化 (token/accountid/mailboxid 缺失)")
                return None

            # 注意：SMTP.dev API 请求不使用外部代理
            response = requests.get(
                f"{self.baseurl}/accounts/{self.accountid}/mailboxes/{self.mailboxid}/messages",
                headers=self.headers,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code != 200:
                logger.error(f"为 {self.acount} 获取消息失败: 状态码 {response.status_code}")
                # 检查是否是 401 (令牌可能失效)
                if response.status_code == 401:
                     logger.error("认证失败 (401)。令牌可能无效或已过期。")
                     # 可以考虑在这里设置 self.token = None 来强制重新获取？
                return None

            response_data = response.json()

            # 检查是否有消息并且结构正确
            if response_data and len(response_data) > 0 and "intro" in response_data[0]:
                logger.debug(f"为 {self.acount} 找到消息简介。")
                return response_data[0]["intro"]
            else:
                # logger.debug(f"尚未找到 {self.acount} 的消息。")
                return None # 没有消息或消息结构不对
        except requests.exceptions.RequestException as e:
            logger.error(f"为 {self.acount} 获取消息时请求出错: {str(e)}")
            return None
        except (ValueError, KeyError, IndexError) as e: # 处理 JSON 解析或键/索引错误
             logger.error(f"为 {self.acount} 解析消息响应时出错: {e}")
             return None
        except Exception as e: # 捕获其他意外错误
            logger.error(f"为 {self.acount} 获取消息时发生未知错误: {e}")
            return None


    def wait_getmessage(self, max_wait_time=MAIL_WAIT_TIMEOUT):
        """等待并获取验证邮件的消息简介，带有超时和重试逻辑"""
        # 先检查必要 ID 和 token 是否初始化
        if not self.token or not self.accountid or not self.mailboxid:
            logger.error("无法等待消息: 客户端未完全初始化。")
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
                    # 不在此处返回 None，继续等待直到超时

                # logger.debug(f"尚未找到 {self.acount} 的邮件。检查次数 {check_count}。等待 {MAIL_CHECK_INTERVAL} 秒...")
                time.sleep(MAIL_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"在为 {self.acount} 检查邮件的循环中出错: {e}")
                # 如果是token相关错误，直接返回None (基于 get_message 中的 401 处理)
                if "401" in str(e) or "token" in str(e).lower():
                    logger.error("邮件检查期间发生致命错误 (令牌/认证问题?)。中止等待。")
                    return None
                # 其他错误，稍微等待后继续尝试
                time.sleep(MAIL_CHECK_INTERVAL * 2)

    # --- 可选的清理方法 ---
    def get_all_accounts(self):
        response = requests.get(
            f"{self.baseurl}/accounts", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def delete_account(self, accountid):
        headers = {
            "X-API-KEY": self.api_key
        }
        response = requests.delete(
            f"{self.baseurl}/accounts/{accountid}", headers=headers, timeout=10
        )
        response.raise_for_status() # 确保删除成功

    def delete_all_accounts(self):
        try:
            accounts = self.get_all_accounts()
            logger.info(f"找到 {len(accounts)} 个账户需要清理。")
            for account in accounts:
                try:
                    acc_id = account.get("id")
                    acc_addr = account.get("address", "未知地址")
                    if acc_id:
                        logger.info(f"尝试删除账户 {acc_addr} (ID: {acc_id})...")
                        self.delete_account(acc_id)
                        logger.success(f"删除账户 {acc_addr} (ID: {acc_id}) 成功。")
                        time.sleep(1) # 稍微延迟避免 API 频率限制
                    else:
                        logger.warning("在账户列表中找到一个没有 ID 的条目，跳过。")
                except requests.exceptions.RequestException as del_e:
                     logger.error(f"删除账户 {acc_addr} (ID: {acc_id}) 时出错: {del_e}")
                except Exception as e:
                     logger.error(f"处理删除账户 {acc_addr} (ID: {acc_id}) 时发生未知错误: {e}")
            logger.info("所有账户清理尝试完成。")
        except requests.exceptions.RequestException as get_e:
             logger.error(f"获取账户列表以进行清理时出错: {get_e}")
        except Exception as e:
             logger.error(f"执行 delete_all_accounts 时发生未知错误: {e}")

# --- Relingo 注册客户端 (已修改以支持代理和兼容旧版 fake-useragent) ---
class RelingoReg:
    def __init__(self):
        # 首先初始化 MailTmClient - 这可能会抛出异常
        try:
            self.mm = MailTmClient()
            self.email = self.mm.get_email() # 获取生成的临时邮箱
            if not self.email:
                raise Exception("无法从 MailTmClient 获取邮箱地址。")
            logger.info(f"RelingoReg 使用邮箱: {self.email}")
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
        # logger.debug(f"使用代理进行 Relingo 请求: {current_proxy}")
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
            # 可选: 在此处添加逻辑来标记此代理为无效? 例如强制下次获取新代理
            # with proxy_management_lock:
            #     current_proxy_info[0]['uses_left'] = 0
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
            logger.success(f"已成功为 {self.email} 发送验证码请求。") # 移除响应体，可能包含敏感信息
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
            logger.success(f"{self.email} 注册/登录成功。") # 移除响应体
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
            # 改进正则，匹配常见的验证码格式（例如6位数字）
            match = re.search(r"\b(\d{4,8})\b", message_intro) # 查找边界内的4到8位数字
            if not match: # 如果没找到，尝试匹配任何数字串
                 match = re.search(r"\d+", message_intro)

            if match:
                code = match.group(0) # 如果是 group(1) 则取括号内的
                logger.info(f"为 {self.email} 提取到验证码 '{code}'。")
                # 4. 使用验证码进行注册
                return self.register(code)
            else:
                logger.error(f"无法从简介中提取验证码: '{message_intro}'")
                return False
        except Exception as e:
            # 捕获在 start() 内部与 MailTmClient 交互时可能出现的意外错误
            logger.error(f"{getattr(self, 'email', '未知邮箱')} 注册过程中发生意外错误: {e}", exc_info=False)
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
            relingo_reg = RelingoReg() # 初始化可能抛出异常

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
            # (包括 MailTmClient 初始化失败)
            logger.error(f"任务 {task_id}: 尝试 {attempt + 1} 期间出错: {e}", exc_info=False) # exc_info=False 避免在日志中重复打印完整的堆栈跟踪
            # 可选: 在出错后添加短暂延迟
            time.sleep(1)

        # 在每次失败的尝试后，可以添加短暂的延迟
        if attempt < MAX_REGISTRATION_ATTEMPTS - 1:
             time.sleep(2) # 重试前等待2秒

    # 如果循环完成所有尝试但仍未成功
    logger.error(f"任务 {task_id}: 尝试 {MAX_REGISTRATION_ATTEMPTS} 次后注册失败。")
    with fail_counter_lock: # 锁定以安全地增加失败计数器
        fail_counter[0] += 1
    return False # 任务最终失败


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
    logger.info(f"代理源 URL: {PROXY_LIST_URL} (每次获取单个 SOCKS5 代理)")
    logger.info(f"每个代理的最大请求数: {RELINGO_REQUESTS_PER_PROXY}")
    logger.info("请确保已在脚本中配置 smtp.dev 的 API Key 和域名！")
    logger.info("按 Ctrl+C 停止程序。")

    # --- 代理初始化 ---
    logger.info("代理管理已初始化，将在需要时从指定 URL 获取代理。")

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

                # 检查已完成任务的结果或异常 (可选，但有助于调试)
                for future in done:
                   try:
                      result = future.result() # 获取任务结果，如果任务中发生异常，这里会重新抛出
                      # logger.debug(f"任务完成，结果: {result}")
                   except Exception as e:
                      # 异常通常已在 register_task 中捕获并记录，这里可以再次记录或处理特定情况
                      logger.error(f"一个已完成的任务抛出了未捕获的异常: {e}", exc_info=False)


                # 记录当前状态 (可以根据需要调整记录频率)
                with proxy_management_lock: # 读取当前代理状态需要加锁
                    current_p = current_proxy_info[0]['proxy_dict']
                    uses_left = current_proxy_info[0]['uses_left']
                proxy_status = f"当前代理: {list(current_p.values())[0] if current_p else '无'}, 剩余: {uses_left}"
                logger.info(f"状态更新 -> 成功: {success_counter[0]} | 失败: {fail_counter[0]} | 活动任务: {len(executor._work_queue.qsize()) + len(futures)} | {proxy_status}")


                # 可选: 如果需要，可以在检查/添加任务之间添加延迟
                # time.sleep(BATCH_DELAY) # 这是旧的批处理延迟逻辑

    except KeyboardInterrupt: # 捕获 Ctrl+C 中断信号
        logger.info("检测到 Ctrl+C。正在关闭...")
        # 尝试关闭执行器，不再接受新任务，并等待现有任务完成 (或超时)
        # executor.shutdown(wait=True, cancel_futures=True) # cancel_futures 仅在 Python 3.9+ 可用
        executor.shutdown(wait=False) # 不等待任务完成，直接退出
        logger.info("执行器已关闭。")

    except Exception as e: # 捕获主循环中的其他意外错误
        logger.error(f"主循环中发生意外错误: {e}", exc_info=True) # exc_info=True 会记录完整的堆栈跟踪

    finally: # 无论如何都会执行的部分
        logger.info("--- 脚本执行完毕 ---")
        logger.info(f"总成功注册次数: {success_counter[0]}")
        logger.info(f"总失败注册任务数: {fail_counter[0]}")
        logger.info("日志已保存到 relingo_reg_*.log 文件。")

    # 可选：清理所有临时邮箱账户
    # try:
    #     logger.info("开始清理所有临时邮箱账户...")
    #     # 创建一个新的 MailTmClient 实例用于清理
    #     # 注意：如果 MailTmClient 初始化失败，这里也会失败
    #     clean_client = MailTmClient()
    #     clean_client.delete_all_accounts()
    #     logger.success("所有临时邮箱账户清理尝试完成。")
    # except Exception as e:
    #     logger.error(f"清理临时邮箱账户时出错: {e}")
