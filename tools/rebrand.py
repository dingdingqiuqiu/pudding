#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
布丁智服 项目改造脚本
====================

把工作区里三套 EchoMind 代码（Python / Java / Vue）复制成独立的「布丁智服」项目，
完成品牌、包名、配置项、主题色和人设的整体改名。

用法（在仓库根目录执行）：

    python 布丁智服/tools/rebrand.py

改名映射（按顺序执行，先长后短，避免二次替换）：

    EchoMindJavaApplication -> PuddingApplication
    EchoMindJava            -> PuddingJava
    EchoMindController      -> PuddingController
    EchoMindProperties      -> PuddingProperties
    echomind-java           -> pudding-java
    echomind-frontend       -> pudding-frontend
    echomind                -> pudding        （Java 包名 com.echomind -> com.pudding）
    ECHOMIND                -> PUDDING        （环境变量）
    EchoMind                -> 布丁智服        （文档/文案）

脚本会重跑覆盖 pudding-* 三个目录，README.md / docs/ 不会被删除。
"""
from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEST_ROOT = os.path.join(ROOT, "布丁智服")

# 源目录 -> 目标目录
MODULES = [
    ("EchoMind", "pudding-python"),
    ("EchoMindJava", "pudding-java"),
    ("EchoMindFrontend", "pudding-frontend"),
]

# 不参与复制的目录（构建产物、依赖缓存、版本库、IDE）
IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "target", "dist", "build",
    ".idea", ".pytest_cache", "__pycache__", ".mvn", ".vscode",
}
IGNORE_FILES = {".DS_Store", "Thumbs.db"}

# 只处理这些文本后缀，其余按二进制原样复制
TEXT_EXT = {
    ".py", ".java", ".js", ".mjs", ".vue", ".html", ".css", ".md", ".txt",
    ".yml", ".yaml", ".xml", ".json", ".properties", ".sh", ".cmd", ".bat",
    ".env", ".example", ".gitignore", ".dockerignore", ".conf", ".sql", ".svg",
}

# 相对路径前缀命中则跳过（运行期生成的向量库，带着旧 collection 名，重建即可）
SKIP_REL_PREFIXES = ("data/chroma",)

# 内容替换规则（顺序敏感）
CONTENT_RULES = [
    ("EchoMindJavaApplication", "PuddingApplication"),
    ("EchoMindJava", "PuddingJava"),
    ("EchoMindController", "PuddingController"),
    ("EchoMindProperties", "PuddingProperties"),
    ("EchoMind学习文档", "布丁智服学习文档"),
    ("EchoMind定位与技术亮点", "布丁智服定位与技术亮点"),
    ("echomind-java", "pudding-java"),
    ("echomind-frontend", "pudding-frontend"),
    ("echomind.", "pudding."),
    ("echoMind", "pudding"),      # 驼峰方法名 echoMindOpenAPI -> puddingOpenAPI
    ("echomind", "pudding"),
    ("ECHOMIND", "PUDDING"),
    ("EchoMind", "布丁智服"),
]

# 路径（目录名/文件名）替换规则：文件名也必须是合法类名，先用英文
PATH_RULES = [
    ("EchoMindJavaApplication", "PuddingApplication"),
    ("EchoMindJava", "PuddingJava"),
    ("EchoMindController", "PuddingController"),
    ("EchoMindProperties", "PuddingProperties"),
    ("EchoMind学习文档", "布丁智服学习文档"),
    ("EchoMind定位与技术亮点", "布丁智服定位与技术亮点"),
    ("EchoMind", "布丁智服"),
    ("echomind", "pudding"),
]

# 前端主题：薰衣草紫 -> 焦糖布丁色
THEME_COLORS = {
    "#6f78f7": "#e8a33d",   # 主强调色
    "#7c84fb": "#f6c264",   # 品牌标识底色
    "rgba(111, 120, 247, 0.14)": "rgba(232, 163, 61, 0.16)",
    "#d9dcff": "#ffe6b8",
    "#d8dcff": "#ffe6b8",
    "#d9def0": "#ffeccd",
    "#e8eafe": "#fff3dd",
    "#dfe2ff": "#fff3dd",
    "#d8ddff": "#fff3dd",
    "#e0e3ff": "#fff3dd",
    "#cfd4ff": "#ffdfae",
    "#aab1ff": "#f0b95c",
    "#9ba3b8": "#b9a58f",
    "#80889f": "#b9a58f",
    "#7e8597": "#a2937f",
}

# 布丁服务人格（写入通用客服 Skill，两个技术栈各一份）
PUDDING_PERSONA = """
## 布丁服务人格

布丁的性格是“柔软、有弹性、有边界”，下面几条是布丁智服所有 Agent 的说话底线：

- 柔软：先接住情绪，再处理问题。用户着急、抱怨、情绪激烈时，不反驳、不甩规则、不说“这是规定”，先用一句话承认他的处境。
- 有弹性：同一件事按用户身份和场景换说法。老用户少讲流程直接给结论，新用户先解释概念；能自助的给出路径，不能自助的直接转人工。
- 有甜度但不过甜：保持礼貌和一点温度，但不用表情堆砌，不用“亲”“宝子”这类过度营销语气，也不要为了讨好而承诺做不到的事。
- 有边界：不承诺无法保证的结果，不猜测系统事实，涉及隐私、支付、权限时主动说明需要核验；宁可多说一句“需要人工确认”，也不假装已经办成。
- 有收口：每轮回复最后给一个明确的下一步（查什么、等多久、找谁），不让用户停留在“那我该怎么办”。

## 回复总原则
"""


def is_text(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name in {"dockerfile", "dockerfile.dev", "makefile", "mvnw", "mvnw.cmd"}:
        return True
    if os.path.splitext(name)[1] in TEXT_EXT:
        return True
    return name in {".env", ".env.example", ".gitignore", ".dockerignore"}


def decode(data: bytes):
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, None


def replace_content(text: str) -> str:
    for old, new in CONTENT_RULES:
        text = text.replace(old, new)
    # 只收拾同一行内的多余空格（不能跨行，否则会把段落合并）
    text = re.sub(r"布丁智服[ \t\u3000]+(?=[\u4e00-\u9fff（（])", "布丁智服", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff））])[ \t\u3000]+布丁智服", "布丁智服", text)
    # 抹掉真实密钥，避免新项目被推到 GitHub 时泄露
    text = re.sub(r"sk-[A-Za-z0-9]{16,}", "sk-your-own-deepseek-key", text)
    return text


def replace_path_name(name: str) -> str:
    for old, new in PATH_RULES:
        name = name.replace(old, new)
    return name


def copy_module(src: str, dst: str) -> int:
    """复制单个模块（跳过依赖/构建产物），返回复制的文件数。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        rel = os.path.relpath(dirpath, src)
        if rel.replace("\\", "/").startswith(SKIP_REL_PREFIXES):
            continue
        target_dir = dst if rel == "." else os.path.join(dst, replace_path_name(rel))
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            if fn in IGNORE_FILES:
                continue
            s = os.path.join(dirpath, fn)
            d = os.path.join(target_dir, replace_path_name(fn))
            if is_text(s):
                data = open(s, "rb").read()
                text, enc = decode(data)
                if text is None:
                    shutil.copy2(s, d)
                else:
                    new = replace_content(text)
                    bom = data.startswith(b"\xef\xbb\xbf")
                    out = new.encode(enc)
                    if bom and not out.startswith(b"\xef\xbb\xbf"):
                        out = b"\xef\xbb\xbf" + out
                    with open(d, "wb") as fh:
                        fh.write(out)
            else:
                shutil.copy2(s, d)
            count += 1
    return count


def patch(rel_path: str, pairs, count_label: str = "") -> int:
    """在目标文件里做定向替换，返回命中次数。"""
    path = os.path.join(DEST_ROOT, rel_path)
    if not os.path.exists(path):
        return 0
    text = open(path, "r", encoding="utf-8").read()
    hits = 0
    for old, new in pairs:
        if old in text:
            hits += text.count(old)
            text = text.replace(old, new)
    if hits:
        open(path, "w", encoding="utf-8", newline="\n").write(text)
    return hits


def apply_branding() -> None:
    # 1. 前端主题：焦糖布丁配色 + 品牌标识
    apply_theme()
    patch("pudding-frontend/src/styles.css", [(
        "  background: linear-gradient(180deg, rgba(111, 120, 247, 1) 0%, rgba(89, 96, 220, 1) 100%);\n"
        "  color: #fff;\n"
        "  font-size: 13px;\n"
        "  font-weight: 900;\n"
        "  box-shadow: 0 10px 24px rgba(111, 120, 247, 0.24);",
        "  background: linear-gradient(180deg, #f7c667 0%, #dd8b26 100%);\n"
        "  color: #fff;\n"
        "  font-size: 17px;\n"
        "  font-weight: 900;\n"
        "  box-shadow: 0 10px 24px rgba(232, 163, 61, 0.28);",
    )])
    patch("pudding-frontend/src/App.vue", [('<span class="brand-mark">E</span>', '<span class="brand-mark">🍮</span>')])
    patch("pudding-frontend/index.html", [("布丁智服客服调试工作台", "布丁智服 · 多 Agent 智能客服工作台")])

    # 2. Python 启动 banner：对齐 + 多 Agent 定位
    patch("pudding-python/api/main.py", [(
        "   ║   布丁智服  v2.0     ║\n   ║   智能客服 AI 系统    ║",
        "   ║   布丁智服  v2.0     ║\n   ║  多 Agent 智能客服   ║",
    )])

    # 3. 布丁服务人格：写进两个技术栈的通用客服 Skill
    for rel in ("pudding-python/skills/general_customer_service/SKILL.md",
                "pudding-java/skills/general_customer_service/SKILL.md"):
        patch(rel, [("\n## 回复总原则\n", PUDDING_PERSONA)])


def apply_theme() -> int:
    css = os.path.join(DEST_ROOT, "pudding-frontend", "src", "styles.css")
    if not os.path.exists(css):
        return 0
    text = open(css, "r", encoding="utf-8").read()
    hits = 0
    for old, new in THEME_COLORS.items():
        if old in text:
            hits += text.count(old)
            text = text.replace(old, new)
    # 半透明版本（hover 边框、focus 光晕、左侧强调条等）
    for old, new in (
        ("rgba(111, 120, 247", "rgba(232, 163, 61"),
        ("rgba(124, 132, 251", "rgba(246, 194, 100"),
    ):
        if old in text:
            hits += text.count(old)
            text = text.replace(old, new)
    open(css, "w", encoding="utf-8", newline="\n").write(text)
    return hits


def main() -> int:
    if not os.path.isdir(os.path.join(ROOT, "EchoMind")):
        print("找不到源目录 EchoMind，请把脚本放在项目根目录下执行", file=sys.stderr)
        return 1

    total = 0
    for src_name, dst_name in MODULES:
        src = os.path.join(ROOT, src_name)
        dst = os.path.join(DEST_ROOT, dst_name)
        if not os.path.isdir(src):
            print(f"跳过不存在的源目录：{src_name}")
            continue
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        n = copy_module(src, dst)
        total += n
        print(f"{src_name:18s} -> 布丁智服/{dst_name:16s} {n:4d} 个文件")

    apply_branding()
    print(f"合计复制 {total} 个文件，品牌/人设/主题已套用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
