"""全局配置：从 .env 读取 API Key"""
import os
from dotenv import load_dotenv

# 加载 .env 文件里的环境变量
load_dotenv()

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"          # 可换成 deepseek-reasoner 试试
DEFAULT_TEMPERATURE = 0.8        # 越大越放飞，越小越稳定
MAX_TRANSCRIPT_LEN = 4000        # 塞给模型的对话历史最大字数，防止越聊越贵
MAX_TOKENS = 300                 # DeepSeek 单次回复 token 上限（deepseek-chat 不吐思考过程，够用）

# ---- 第二个模型：智谱 GLM ----
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL = "glm-4.7-flash"    # 智谱免费模型
# glm-4.7-flash 是"混合思考"模型：默认会先输出大量思考 token 再写正文。
# agent.py 已用 extra_body={'thinking': {'type': 'disabled'}} 显式关闭思考，
# 所以这里无需为推理预留预算，600 足够正常回复（早期实测：开启思考时
# max_tokens=300 会出现 finish_reason=length 且 content='' 的空回复，别再调回去）。
ZHIPU_MAX_TOKENS = 600


def check_config(providers=("deepseek", "zhipu")):
    """启动前检查：只校验这次实际要用到的厂商 Key（要用的都有效才放行）"""
    problems = []
    if "deepseek" in providers and (
        not API_KEY or API_KEY.startswith("sk-你的") or "你的" in API_KEY
    ):
        problems.append(
            "没有检测到有效的 DEEPSEEK_API_KEY。\n"
            "请打开项目目录下的 .env 文件，把 DEEPSEEK_API_KEY 改成你的真实 Key（sk-开头）。"
        )
    if "zhipu" in providers and (not ZHIPU_API_KEY or "你的" in ZHIPU_API_KEY):
        problems.append(
            "没有检测到有效的 ZHIPU_API_KEY。\n"
            "请打开项目目录下的 .env 文件，补上智谱的 Key（格式：id.secret，别用模板占位符）。"
        )
    if problems:
        raise SystemExit("\n".join(problems))
