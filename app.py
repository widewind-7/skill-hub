"""Skill Hub - Local skill management system for Claude Code agents."""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("skill-hub")

# Baidu Translate API
BAIDU_APPID = os.environ.get("BAIDU_APPID", "")
BAIDU_KEY = os.environ.get("BAIDU_KEY", "")

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Skill Hub")

HOME = Path(os.path.expanduser("~"))
CACHE_FILE = Path(__file__).parent / ".skill_cache.json"
ZH_MAP_FILE = Path(__file__).parent / "skill_zh_map.json"

# 加载中文翻译映射
_zh_map: dict[str, str] = {}
if ZH_MAP_FILE.exists():
    try:
        _zh_map = json.loads(ZH_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        _zh_map = {}

# ── 中央仓库 + Agent 目录 ─────────────────────────────────────────
REPO_DIR = HOME / ".cc-switch" / "skills"  # 中央仓库，symlink 源

AGENT_DIRS = {
    "claude": HOME / ".claude" / "skills",
    "agents": HOME / ".agents" / "skills",
    "codex": HOME / ".codex" / "skills",
    "openclaw": HOME / ".openclaw" / "skills",
    "reasonix": HOME / ".reasonix" / "skills",
    "trae-cn": HOME / ".trae-cn" / "skills",
    "trae": HOME / ".trae" / "skills",
    "workbuddy": HOME / ".workbuddy" / "skills",
}

CATEGORY_MAP = {
    "design-system": "创意设计", "creative-direction": "创意设计", "prototype": "创意设计",
    "video": "视频制作", "animation": "视频制作",
    "marketing-creative": "内容创作", "copywriting": "内容创作",
    "development": "开发工具", "devops": "开发工具",
    "data": "数据分析", "research": "学术研究",
    "productivity": "效率工具", "ai-ml": "AI/ML",
}

CAT_KEYWORDS = [
    (["video", "remotion", "animat", "lottie", "gsap", "motion", "hyperframe"], "视频制作"),
    (["design", "figma", "ui", "ux", "prototype", "css", "tailwind", "shadcn"], "创意设计"),
    (["write", "copy", "article", "content", "blog", "marketing"], "内容创作"),
    (["code", "dev", "git", "test", "debug", "api", "cli"], "开发工具"),
    (["data", "stock", "chart", "analytics", "research"], "数据分析"),
    (["ai", "llm", "model", "prompt"], "AI/ML"),
]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ── 数据模型 ────────────────────────────────────────────────────
class SkillMeta(BaseModel):
    name: str
    description: str = ""
    summary_zh: str = ""
    category: str = "其他"
    origin: str = ""
    version: str = ""
    homepage: str = ""
    triggers: list = []
    installed_in: dict[str, dict] = {}
    source_path: str = ""
    file_count: int = 0
    parent: str = ""  # 父 skill 名称，空表示顶层 skill
    children: list[str] = []  # 子 skill 名称列表
    od_type: str = ""  # od.type 元数据（如 hyperframes）
    od_mode: str = ""  # od.mode 元数据（如 template, prototype）


class SyncRequest(BaseModel):
    agent: str


class RepoInfo(BaseModel):
    owner: str
    name: str
    branch: str = "main"
    enabled: bool = True


class InstallRequest(BaseModel):
    owner: str
    name: str
    branch: str = "main"
    directory: str


# ── 仓库管理 + 发现/更新 ───────────────────────────────────────
REPOS_FILE = Path(__file__).parent / "skill_repos.json"
INSTALLED_FILE = Path(__file__).parent / "installed_skills.json"
DISCOVER_CACHE = Path(__file__).parent / ".discover_cache.json"
DISCOVER_TTL = 3600  # 1 hour

_repos: list[dict] = []
_installed: dict[str, dict] = {}  # skill_name -> {repo_owner, repo_name, repo_branch, directory, content_hash}


def _load_repos():
    global _repos
    if REPOS_FILE.exists():
        _repos = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    else:
        # Seed from cc-switch database
        _repos = _seed_from_ccs_db()
        _save_repos()


def _seed_from_ccs_db() -> list[dict]:
    db_path = HOME / ".cc-switch" / "cc-switch.db"
    if not db_path.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT owner, name, branch FROM skill_repos WHERE enabled=1")
        rows = cur.fetchall()
        conn.close()
        return [{"owner": r[0], "name": r[1], "branch": r[2], "enabled": True} for r in rows]
    except Exception:
        return []


def _save_repos():
    REPOS_FILE.write_text(json.dumps(_repos, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_installed():
    global _installed
    if INSTALLED_FILE.exists():
        _installed = json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
    else:
        _installed = {}


def _save_installed():
    INSTALLED_FILE.write_text(json.dumps(_installed, ensure_ascii=False, indent=2), encoding="utf-8")


def _gh_api(endpoint: str) -> dict | list:
    """Call GitHub API via gh CLI."""
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api error: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _download_dir(owner: str, repo: str, branch: str, path: str, dest: Path):
    """Recursively download a directory from GitHub."""
    items = _gh_api(f"repos/{owner}/{repo}/contents/{path}?ref={branch}")
    dest.mkdir(parents=True, exist_ok=True)
    for item in items:
        item_path = f"{path}/{item['name']}" if path else item['name']
        if item["type"] == "dir":
            _download_dir(owner, repo, branch, item_path, dest / item["name"])
        elif item["type"] == "file":
            # Download file content
            file_data = _gh_api(f"repos/{owner}/{repo}/contents/{item_path}?ref={branch}")
            import base64
            content = base64.b64decode(file_data["content"])
            (dest / item["name"]).write_bytes(content)


def _compute_hash(directory: Path) -> str:
    """Compute SHA256 hash of all files in a directory."""
    sha = hashlib.sha256()
    for f in sorted(directory.rglob("*")):
        if f.is_file() and ".git" not in str(f):
            sha.update(f.read_bytes())
    return sha.hexdigest()


def _get_remote_tree_sha(owner: str, repo: str, branch: str, path: str) -> str:
    """Get the tree SHA for a directory from GitHub's git tree API (one call per repo)."""
    tree = _gh_api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    for item in tree.get("tree", []):
        if item["path"] == path and item["type"] == "tree":
            return item["sha"]
    # Fallback: if path not found as tree, use blob sha of SKILL.md
    for item in tree.get("tree", []):
        if item["path"] == f"{path}/SKILL.md":
            return item["sha"]
    raise RuntimeError(f"Path '{path}' not found in tree")


def _discover_from_repo(owner: str, repo: str, branch: str) -> list[dict]:
    """Discover skills from a single repo using git tree API + concurrent raw fetches."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        tree = _gh_api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    except Exception:
        return []

    # Get repo star count once
    repo_stars = 0
    try:
        repo_info = _gh_api(f"repos/{owner}/{repo}")
        repo_stars = repo_info.get("stargazers_count", 0)
    except Exception:
        pass

    # Find all SKILL.md paths
    skill_dirs = []
    for item in tree.get("tree", []):
        if item["path"].endswith("/SKILL.md") or item["path"] == "SKILL.md":
            skill_dir = item["path"].rsplit("/SKILL.md", 1)[0]
            skill_dirs.append(skill_dir)

    def _fetch_one(skill_path: str) -> dict | None:
        name = skill_path.split("/")[-1] if "/" in skill_path else skill_path
        if not name or name.startswith("."):
            return None
        try:
            # Use raw.githubusercontent.com (no API rate limit)
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{skill_path}/SKILL.md"
            req = urllib.request.Request(url, headers={"User-Agent": "skill-hub"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                content = resp.read().decode("utf-8", errors="replace")[:4096]
            m = FRONTMATTER_RE.match(content)
            meta = {}
            if m:
                try:
                    meta = yaml.safe_load(m.group(1)) or {}
                except Exception:
                    pass
            return {
                "name": meta.get("name", name),
                "description": str(meta.get("description", ""))[:500],
                "directory": skill_path,
                "repo_owner": owner,
                "repo_name": repo,
                "repo_branch": branch,
                "stars": repo_stars,
            }
        except Exception:
            return {
                "name": name,
                "description": "",
                "directory": skill_path,
                "repo_owner": owner,
                "repo_name": repo,
                "repo_branch": branch,
                "stars": repo_stars,
            }

    # Fetch all SKILL.md files concurrently (20 workers for raw URLs)
    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_fetch_one, sp): sp for sp in skill_dirs}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    return results


# Initialize
_load_repos()
_load_installed()


# ── 解析 ───────────────────────────────────────────────────────
def parse_skill_md(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except Exception:
        return {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    raw = m.group(1)
    try:
        return yaml.safe_load(raw) or {}
    except Exception:
        pass
    # Regex fallback for SKILL.md with broken YAML (e.g. special chars in argument-hint)
    result = {}
    nm = re.search(r'^name:\s*(.+)$', raw, re.MULTILINE)
    if nm:
        result['name'] = nm.group(1).strip()
    dm = re.search(r'^description:\s*\|?\s*\n(.*?)(?=\n\w|\n---|\Z)', raw, re.DOTALL)
    if dm:
        result['description'] = dm.group(1).strip().split('\n')[0][:200]
    else:
        dm2 = re.search(r'^description:\s*(.+)$', raw, re.MULTILINE)
        if dm2:
            result['description'] = dm2.group(1).strip()[:200]
    return result


def classify_skill(meta: dict) -> str:
    od = meta.get("od", {})
    if isinstance(od, dict):
        cat = od.get("category", "")
        if cat in CATEGORY_MAP:
            return CATEGORY_MAP[cat]

    combined = f"{meta.get('name','') or ''} {meta.get('description','') or ''}".lower()
    for kws, label in CAT_KEYWORDS:
        if any(k in combined for k in kws):
            return label
    return "其他"


# ── 中文简要说明生成 ─────────────────────────────────────────────
_ZH_KEYWORDS: list[tuple[list[str], str]] = [
    (["image", "img", "photo", "picture", "screenshot", "picture"], "图像处理工具，生成和编辑图片"),
    (["video", "remotion", "hyperframe", "ffmpeg", "video-player"], "视频制作工具，用于生成和编辑视频内容"),
    (["animation", "animat", "lottie", "gsap", "motion"], "动画制作工具，创建动效和交互动画"),
    (["3d", "three", "spline", "threejs"], "3D 场景与模型制作工具"),
    (["design-system", "design-token", "figma"], "设计系统工具，管理设计规范和组件库"),
    (["prototype", "wireframe", "figma"], "原型设计工具，快速制作交互原型"),
    (["ui-design", "ui ", "tailwind", "shadcn", "radix"], "UI 界面开发工具，构建现代化界面组件"),
    (["web-design", "landing", "website"], "网页设计工具，创建精美网页"),
    (["creative-direction", "creative"], "创意设计工具，提供设计方向和视觉方案"),
    (["copywriting", "copy-"], "营销文案工具，撰写广告和推广文案"),
    (["content-creator", "content-"], "内容创作助手，辅助创作各类内容"),
    (["article", "blog", "writing", "write", "markdown"], "写作工具，辅助撰写文章和文档"),
    (["storytelling", "story", "narrative"], "叙事工具，辅助故事创作和内容讲述"),
    (["data-", "data_"], "数据处理工具，进行数据分析和可视化"),
    (["stock", "crypto", "finance", "trading"], "金融分析工具，分析市场数据和趋势"),
    (["chart", "diagram", "visualization", "visual"], "图表可视化工具，生成各类图表"),
    (["research", "academic", "paper", "scholar"], "学术研究工具，辅助论文和文献研究"),
    (["git", "github", "pr-", "code-review"], "代码管理工具，辅助 Git 和 GitHub 操作"),
    (["test", "testing", "spec"], "测试工具，辅助编写和运行测试"),
    (["api", "rest", "graphql", "endpoint"], "API 开发工具，设计和调试接口"),
    (["cli", "command-line", "terminal"], "命令行工具，增强终端操作体验"),
    (["debug", "inspect", "profiler"], "调试工具，辅助代码调试和性能分析"),
    (["ci/cd", "deploy", "docker", "k8s", "devops"], "DevOps 工具，辅助部署和运维"),
    (["claude", "anthropic", "sdk"], "Claude API 工具，辅助调用和管理 Claude 服务"),
    (["prompt", "llm", "ai-"], "AI/LLM 工具，优化 AI 交互和提示词"),
    (["agent", "agentic"], "AI Agent 工具，构建和管理智能代理"),
    (["mcp", "mcp-"], "MCP 服务器工具，扩展 Claude Code 能力"),
    (["image", "img", "photo", "picture", "screenshot"], "图像处理工具，生成和编辑图片"),
    (["icon", "emoji", "svg"], "图标素材工具，管理图标和矢量图"),
    (["audio", "sound", "music", "voice"], "音频处理工具，处理音频和语音"),
    (["pdf", "document", "doc"], "文档处理工具，生成和管理文档"),
    (["email", "mail"], "邮件工具，辅助邮件撰写和管理"),
    (["calendar", "schedule", "event"], "日程管理工具，管理时间和事件"),
    (["todo", "task", "project", "kanban"], "任务管理工具，组织和跟踪工作进度"),
    (["travel", "trip", "itinerary"], "旅行规划工具，生成行程和旅行方案"),
    (["weather", "forecast"], "天气工具，查询天气信息"),
    (["translate", "i18n", "localization"], "翻译/国际化工具，处理多语言"),
    (["seo", "search-engine"], "SEO 工具，优化搜索引擎排名"),
    (["social", "twitter", "linkedin", "instagram", "xiaohongshu"], "社交媒体工具，管理社交平台内容"),
    (["knowledge", "kb-", "rag", "retrieval"], "知识检索工具，从知识库中查找信息"),
    (["memory", "remember"], "记忆管理工具，管理上下文和知识"),
    (["workflow", "pipeline", "automat"], "流程自动化工具，编排和自动化工作流"),
    (["theme", "dark-mode", "color"], "主题样式工具，管理视觉主题和配色"),
    (["component", "library", "storybook"], "组件库工具，管理可复用 UI 组件"),
    (["dashboard", "admin", "panel"], "仪表盘工具，构建管理后台界面"),
    (["form", "validation", "schema"], "表单工具，处理表单验证和数据校验"),
    (["table", "grid", "list"], "数据表格工具，展示和操作表格数据"),
    (["map", "geo", "location"], "地图地理工具，处理地理位置信息"),
    (["chat", "messenger", "im"], "聊天通讯工具，构建即时通讯功能"),
    (["payment", "stripe", "billing"], "支付工具，处理支付和账单"),
    (["auth", "login", "oauth", "jwt"], "认证授权工具，处理用户登录和权限"),
    (["perf", "optim", "cache", "lazy"], "性能优化工具，提升应用运行效率"),
    (["a11y", "accessibility", "screen-reader"], "无障碍工具，提升产品可访问性"),
    (["scrap", "crawl", "fetch", "parse"], "数据抓取工具，从网页提取和解析数据"),
    (["beautiful-article", "maiguo", "pango"], "精美排版工具，生成专业排版的文章"),
    (["mindmap", "mind-map", "思维导图"], "思维导图工具，创建思维导图"),
    (["presentation", "slide", "ppt"], "演示文稿工具，制作幻灯片"),
    (["spreadsheet", "excel", "csv"], "表格数据工具，处理表格和数据文件"),
    (["notion", "obsidian", "笔记"], "笔记工具，管理知识和笔记"),
    (["cheat-on-content", "cheat-learn-from", "cheat-migrate", "cheat-persona",
      "cheat-predict", "cheat-publish", "cheat-recommend", "cheat-retro",
      "cheat-seed", "cheat-trends"], "内容创作/社交媒体运营辅助工具"),
    (["faq-page"], "FAQ 页面生成器，创建可折叠的常见问题页面"),
    (["gemini-web-extended-thinking", "gemini"], "Gemini AI 扩展思维工具，用于深度推理和分析"),
    (["premium-design"], "高端设计工具，创建精美的视觉设计方案"),
    (["ss-page", "styleseed"], "移动端页面快速搭建工具，基于 StyleSeed 布局模式"),
    (["ask-matt"], "Skill 路由器，根据场景推荐合适的 skill 或工作流"),
    (["teach"], "教学工具，辅助用户学习新技能或概念"),
]


def build_summary_zh(name: str, desc: str, category: str) -> str:
    """根据 skill 名称和描述生成中文简要说明"""
    # 1. 优先使用翻译映射表
    if name in _zh_map and _zh_map[name]:
        return _zh_map[name]

    # 2. 检查描述是否已有中文
    has_chinese = any('一' <= c <= '鿿' for c in desc)
    if has_chinese and len(desc) > 4:
        return desc[:200]

    # 3. 无中文翻译，返回空，由前端显示原文+翻译按钮
    return ""

    return ""


def resolve_source(entry: Path) -> Path:
    if entry.is_symlink():
        return Path(os.readlink(str(entry)))
    return entry


# ── 磁盘缓存 ──────────────────────────────────────────────────
_skills_cache: dict[str, SkillMeta] = {}
_cache_ts: float = 0
CACHE_TTL = 30


def load_disk_cache() -> dict[str, SkillMeta] | None:
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > 300:
            return None
        return {k: SkillMeta(**v) for k, v in data["skills"].items()}
    except Exception:
        return None


def save_disk_cache(skills: dict[str, SkillMeta]):
    try:
        CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "skills": {k: v.model_dump() for k, v in skills.items()}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _do_scan() -> dict[str, SkillMeta]:
    skills: dict[str, SkillMeta] = {}
    parsed_cache: dict[str, dict] = {}  # source_path -> parsed meta

    # 扫描目录：中央仓库 + 所有 agent
    scan_dirs = {"cc-switch": REPO_DIR, **AGENT_DIRS}

    for agent_name, agent_dir in scan_dirs.items():
        if not agent_dir.exists():
            continue
        for entry in agent_dir.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_file():
                continue

            skill_name = entry.name
            source = resolve_source(entry)
            source_str = str(source)

            if skill_name not in skills:
                if source_str not in parsed_cache:
                    skill_md = source / "SKILL.md"
                    meta_dict = parse_skill_md(skill_md) if skill_md.exists() else {}
                    parsed_cache[source_str] = meta_dict
                else:
                    meta_dict = parsed_cache[source_str]

                desc = meta_dict.get("description", "")
                desc = desc.strip() if isinstance(desc, str) else str(desc)

                # 统计文件数（只对源目录，用缓存避免重复计数）
                file_count = 0
                if source.is_dir():
                    try:
                        file_count = sum(1 for _ in source.iterdir() if _.is_file())
                    except Exception:
                        file_count = 0

                category = classify_skill(meta_dict)

                # 提取 homepage：od.upstream > homepage > url
                od = meta_dict.get("od", {})
                homepage = ""
                if isinstance(od, dict):
                    homepage = od.get("upstream", "") or od.get("homepage", "")
                if not homepage:
                    homepage = meta_dict.get("homepage", "") or meta_dict.get("url", "")

                skills[skill_name] = SkillMeta(
                    name=skill_name,
                    description=desc[:300],
                    summary_zh=build_summary_zh(skill_name, desc, category),
                    category=category,
                    origin=meta_dict.get("origin", ""),
                    version=str(meta_dict.get("version", "")),
                    homepage=homepage,
                    triggers=meta_dict.get("triggers", []) or [],
                    source_path=source_str,
                    file_count=file_count,
                    od_type=od.get("type", "") if isinstance(od, dict) else "",
                    od_mode=od.get("mode", "") if isinstance(od, dict) else "",
                )

            is_sym = entry.is_symlink()
            skills[skill_name].installed_in[agent_name] = {
                "path": str(entry),
                "is_symlink": is_sym,
                "target": os.readlink(str(entry)) if is_sym else "",
            }

    # ── 子 skill 分组 ────────────────────────────────────────────
    # 第一轮：按 od.type 元数据分组（如 hyperframes 模板归到 hyperframes）
    type_groups: dict[str, list[str]] = {}
    for name, s in skills.items():
        if s.od_type and s.od_type != name:
            type_groups.setdefault(s.od_type, []).append(name)

    for parent_type, members in type_groups.items():
        if parent_type in skills:
            for m in members:
                if skills[m].parent:
                    continue
                skills[m].parent = parent_type
                skills[parent_type].children.append(m)

    # 第二轮：按名称前缀分组（阈值 5+，捕获 cheat-*、video-*、gsap-* 等大家族）
    prefix_groups: dict[str, list[str]] = {}
    for name in skills:
        if skills[name].parent:
            continue
        parts = name.split("-")
        if len(parts) >= 2:
            prefix = parts[0]
            prefix_groups.setdefault(prefix, []).append(name)

    for prefix, members in prefix_groups.items():
        if len(members) < 4:
            continue
        natural_parent = prefix if prefix in members else None
        if not natural_parent:
            for candidate in [f"{prefix}-on-content", f"{prefix}-core", f"{prefix}-replication", f"{prefix}-consultation"]:
                if candidate in members:
                    natural_parent = candidate
                    break
        if not natural_parent:
            natural_parent = min(members, key=len)
        for m in members:
            if m == natural_parent:
                continue
            if skills[m].parent:
                continue
            skills[m].parent = natural_parent
            skills[natural_parent].children.append(m)

    # 第三轮：路由器 skill 检测（通过 SKILL.md 反引号引用，按候选数降序处理）
    scan_dirs = [REPO_DIR] + list(AGENT_DIRS.values())
    router_candidates = []
    for name, s in skills.items():
        if s.parent:
            continue
        for base_dir in scan_dirs:
            skill_md = base_dir / name / "SKILL.md"
            if skill_md.exists():
                try:
                    content = skill_md.read_text(encoding="utf-8", errors="replace")[:8192]
                    refs = set(re.findall(r'`([a-z][a-z0-9-]+)`', content))
                    refs.discard(name)
                    candidates = [r for r in refs if r in skills and r != name and not skills[r].parent]
                    if len(candidates) >= 2:
                        router_candidates.append((name, candidates))
                except Exception:
                    pass
                break

    router_candidates.sort(key=lambda x: -len(x[1]))
    for name, candidates in router_candidates:
        s = skills[name]
        parent_prefix = name.split("-")[0] if "-" in name else name
        # 只保留与 parent 有共同前缀的候选
        domain_children = [c for c in candidates if c.startswith(parent_prefix + "-") or c.startswith(parent_prefix)]
        if len(domain_children) >= 2:
            for child in domain_children:
                if not skills[child].parent:
                    skills[child].parent = name
                    s.children.append(child)
        elif len(candidates) >= 2:
            # 没有共同前缀时，候选按前缀分组，每组选最长名作为 parent
            prefix_groups_map: dict[str, list[str]] = {}
            for c in candidates:
                cp = c.split("-")[0] if "-" in c else c
                prefix_groups_map.setdefault(cp, []).append(c)
            for cp, group in prefix_groups_map.items():
                if len(group) >= 2:
                    # 优先选名字含 replication/router/workflow 的，否则选最长名
                    def _group_score(n):
                        bonus = 100 if any(k in n for k in ('replication', 'router', 'workflow', 'orchestrat')) else 0
                        return (bonus, len(n))
                    group_parent = max(group, key=_group_score)
                    for child in group:
                        if child != group_parent and not skills[child].parent:
                            skills[child].parent = group_parent
                            skills[group_parent].children.append(child)

    return skills


def scan_all_skills() -> dict[str, SkillMeta]:
    global _skills_cache, _cache_ts

    if _skills_cache and time.time() - _cache_ts < CACHE_TTL:
        return _skills_cache

    if not _skills_cache:
        disk = load_disk_cache()
        if disk:
            _skills_cache = disk
            _cache_ts = time.time()
            # 后台刷新
            threading.Thread(target=_refresh_cache, daemon=True).start()
            return _skills_cache

    _skills_cache = _do_scan()
    _cache_ts = time.time()
    save_disk_cache(_skills_cache)
    return _skills_cache


def _refresh_cache():
    global _skills_cache, _cache_ts
    try:
        fresh = _do_scan()
        _skills_cache = fresh
        _cache_ts = time.time()
        save_disk_cache(fresh)
    except Exception:
        pass


def cache_clear():
    global _skills_cache, _cache_ts
    _skills_cache = {}
    _cache_ts = 0
    try:
        CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── API 路由 ────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/skills")
async def list_skills(q: str = "", category: str = "", show_all: bool = False):
    skills = scan_all_skills()
    result = list(skills.values())

    # 默认隐藏子 skill，但搜索时显示匹配的子 skill
    if not show_all:
        if q:
            q_lower = q.lower()
            # 搜索时：只返回名称或描述匹配的（含子 skill）
            result = [s for s in result if q_lower in s.name.lower() or q_lower in s.description.lower()]
        else:
            result = [s for s in result if not s.parent]

    if category and category != "全部":
        result = [s for s in result if s.category == category]

    result.sort(key=lambda s: s.name.lower())
    return result


@app.get("/api/skills/{name}")
async def get_skill(name: str):
    skills = scan_all_skills()
    if name not in skills:
        raise HTTPException(404, f"Skill '{name}' not found")

    skill = skills[name]
    source = Path(skill.source_path)
    skill_md = source / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""

    files = []
    if source.is_dir():
        files = [str(f.relative_to(source)) for f in sorted(source.rglob("*"))
                 if f.is_file() and ".git" not in str(f)]

    # 子 skill 详情
    children_info = []
    for child_name in skill.children:
        if child_name in skills:
            cs = skills[child_name]
            children_info.append({
                "name": cs.name,
                "summary_zh": cs.summary_zh,
                "description": cs.description,
                "category": cs.category,
            })

    return {**skill.model_dump(), "content": content, "files": files, "children_info": children_info}


@app.delete("/api/skills/{name}")
async def delete_skill(name: str, agent: str = ""):
    skills = scan_all_skills()
    if name not in skills:
        raise HTTPException(404, f"Skill '{name}' not found")

    skill = skills[name]
    if agent:
        if agent not in skill.installed_in:
            raise HTTPException(404, f"Not installed in {agent}")
        info = skill.installed_in[agent]
        if not info["is_symlink"]:
            raise HTTPException(400, f"Skill in {agent} is not a symlink")
        Path(info["path"]).unlink()
        cache_clear()
        return {"ok": True, "message": f"Removed from {agent}"}
    else:
        removed = [ag for ag, i in skill.installed_in.items() if i["is_symlink"]]
        for ag in removed:
            Path(skill.installed_in[ag]["path"]).unlink()
        cache_clear()
        return {"ok": True, "message": f"Removed symlinks from: {', '.join(removed)}"}


@app.post("/api/skills/{name}/sync")
async def sync_skill(name: str, req: SyncRequest):
    skills = scan_all_skills()
    if name not in skills:
        raise HTTPException(404, f"Skill '{name}' not found")

    agent = req.agent
    if agent not in AGENT_DIRS:
        raise HTTPException(400, f"Unknown agent: {agent}")

    agent_dir = AGENT_DIRS[agent]
    agent_dir.mkdir(parents=True, exist_ok=True)

    target_path = agent_dir / name
    if target_path.exists():
        return {"ok": True, "message": f"Already exists in {agent}"}

    source = Path(skills[name].source_path)
    try:
        if os.name == "nt":
            try:
                os.symlink(str(source), str(target_path), target_is_directory=True)
            except OSError:
                subprocess.run(["mklink", "/J", str(target_path), str(source)],
                               shell=True, check=True, capture_output=True)
        else:
            os.symlink(str(source), str(target_path))
        cache_clear()
        return {"ok": True, "message": f"Synced to {agent}"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/skills/{name}/unsync")
async def unsync_skill(name: str, req: SyncRequest):
    skills = scan_all_skills()
    if name not in skills:
        raise HTTPException(404, f"Skill '{name}' not found")

    skill = skills[name]
    agent = req.agent
    if agent not in skill.installed_in:
        raise HTTPException(404, f"Not installed in {agent}")

    info = skill.installed_in[agent]
    if not info["is_symlink"]:
        raise HTTPException(400, f"Skill in {agent} is not a symlink")

    Path(info["path"]).unlink()
    cache_clear()
    return {"ok": True, "message": f"Removed from {agent}"}


@app.get("/api/agents")
async def list_agents():
    result = []
    for name, path in AGENT_DIRS.items():
        count = 0
        if path.exists():
            count = sum(1 for e in path.iterdir() if e.is_dir() and not e.name.startswith("."))
        result.append({"name": name, "path": str(path), "skill_count": count})
    return result


@app.get("/api/agents/{agent_name}/skills")
async def agent_skills(agent_name: str):
    if agent_name not in AGENT_DIRS:
        raise HTTPException(404, f"Unknown agent: {agent_name}")
    skills = scan_all_skills()
    result = []
    for s in skills.values():
        if agent_name in s.installed_in:
            result.append({
                "name": s.name,
                "category": s.category,
                "description": s.description,
                "summary_zh": s.summary_zh,
                "homepage": s.homepage,
                "is_symlink": s.installed_in[agent_name]["is_symlink"],
            })
    result.sort(key=lambda x: x["name"].lower())
    return result


@app.get("/api/categories")
async def list_categories():
    skills = scan_all_skills()
    cats: dict[str, int] = {}
    for s in skills.values():
        if s.parent:
            continue
        cats[s.category] = cats.get(s.category, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])]


@app.post("/api/open-path")
async def open_path(body: dict):
    """Open a local folder in Explorer/Finder."""
    path = body.get("path", "")
    if not path:
        raise HTTPException(400, "Path is empty")
    try:
        if os.name == "nt":
            real = os.path.realpath(path)
            logger.info(f"Opening path: input={path!r} real={real!r}")
            # Use os.startfile in a thread to avoid async context issues
            def _open():
                try:
                    os.startfile(real)
                    logger.info(f"startfile success: {real}")
                except Exception as e:
                    logger.error(f"os.startfile failed: {e}, trying explorer")
                    try:
                        os.startfile(os.path.dirname(real))
                    except Exception as e2:
                        logger.error(f"explorer fallback also failed: {e2}")
            threading.Thread(target=_open, daemon=True).start()
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True}
    except Exception as e:
        logger.exception(f"open-path failed for {path}")
        raise HTTPException(500, str(e))


@app.post("/api/translate")
async def translate_skill(body: dict):
    """Use Baidu Translate API to translate a skill description to Chinese."""
    import hashlib
    name = body.get("name", "")
    desc = body.get("desc", "")
    if not name or not desc:
        raise HTTPException(400, "name and desc are required")

    # Cache key = name + description (different repos may have same name, different desc)
    cache_key = hashlib.md5((name + desc[:200]).encode()).hexdigest()

    # Check in-memory translate cache
    if hasattr(app, '_translate_cache') and cache_key in app._translate_cache:
        return {"zh": app._translate_cache[cache_key]}

    salt = str(int(time.time() * 1000))
    sign_str = BAIDU_APPID + desc[:1000] + salt + BAIDU_KEY
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    params = urllib.parse.urlencode({
        "q": desc[:1000],
        "from": "en",
        "to": "zh",
        "appid": BAIDU_APPID,
        "salt": salt,
        "sign": sign,
    })
    url = f"https://fanyi-api.baidu.com/api/trans/vip/translate?{params}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if "trans_result" in data:
            zh = "".join(item["dst"] for item in data["trans_result"])
        elif "error_code" in data:
            raise HTTPException(400, f"Baidu API error {data['error_code']}: {data.get('error_msg', '')}")
        else:
            raise HTTPException(400, "Unexpected response from Baidu API")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Network error: {e}")

    # Save to memory cache
    if not hasattr(app, '_translate_cache'):
        app._translate_cache = {}
    app._translate_cache[cache_key] = zh

    return {"zh": zh}


# ── 仓库管理 API ────────────────────────────────────────────────

@app.get("/api/repos")
async def list_repos():
    return _repos


@app.post("/api/repos")
async def add_repo(repo: RepoInfo):
    for r in _repos:
        if r["owner"] == repo.owner and r["name"] == repo.name:
            return {"ok": True, "message": "Already exists"}
    _repos.append(repo.model_dump())
    _save_repos()
    # Clear discover cache
    DISCOVER_CACHE.unlink(missing_ok=True)
    return {"ok": True}


@app.delete("/api/repos/{owner}/{name}")
async def remove_repo(owner: str, name: str):
    global _repos
    _repos = [r for r in _repos if not (r["owner"] == owner and r["name"] == name)]
    _save_repos()
    DISCOVER_CACHE.unlink(missing_ok=True)
    return {"ok": True}


@app.put("/api/repos/{owner}/{name}/toggle")
async def toggle_repo(owner: str, name: str):
    for r in _repos:
        if r["owner"] == owner and r["name"] == name:
            r["enabled"] = not r.get("enabled", True)
            _save_repos()
            DISCOVER_CACHE.unlink(missing_ok=True)
            return {"ok": True, "enabled": r["enabled"]}
    raise HTTPException(404, "Repo not found")


# ── 发现 API ───────────────────────────────────────────────────

@app.get("/api/discover")
async def discover_skills():
    # Check cache
    if DISCOVER_CACHE.exists():
        try:
            cache = json.loads(DISCOVER_CACHE.read_text(encoding="utf-8"))
            if time.time() - cache.get("ts", 0) < DISCOVER_TTL:
                return cache["data"]
        except Exception:
            pass

    # Scan all enabled repos concurrently
    from concurrent.futures import ThreadPoolExecutor, as_completed
    enabled_repos = [r for r in _repos if r.get("enabled", True)]
    all_skills = []
    with ThreadPoolExecutor(max_workers=len(enabled_repos) or 1) as pool:
        futures = {pool.submit(_discover_from_repo, r["owner"], r["name"], r["branch"]): r for r in enabled_repos}
        for f in as_completed(futures):
            try:
                all_skills.extend(f.result())
            except Exception:
                continue

    # Mark installed
    local_skills = scan_all_skills()
    for s in all_skills:
        s["installed"] = s["name"] in local_skills or s["name"] in _installed

    # Save cache
    DISCOVER_CACHE.write_text(json.dumps({"ts": time.time(), "data": all_skills}, ensure_ascii=False), encoding="utf-8")

    return all_skills


@app.get("/api/discover/skill")
async def discover_skill_detail(owner: str, repo: str, branch: str = "main", directory: str = ""):
    """Fetch full SKILL.md content and file listing for a discover skill."""
    try:
        # Fetch SKILL.md content
        skill_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{directory}/SKILL.md"
        req = urllib.request.Request(skill_url, headers={"User-Agent": "skill-hub"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")

        # Parse frontmatter
        meta = {}
        body = content
        m = FRONTMATTER_RE.match(content)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                pass
            body = content[m.end():]

        # Fetch file listing via git tree
        tree = _gh_api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
        files = []
        prefix = directory + "/" if directory else ""
        for item in tree.get("tree", []):
            if item["path"].startswith(prefix) and item["path"] != directory:
                rel = item["path"][len(prefix):]
                if "/" not in rel:  # immediate children only
                    files.append(rel)

        return {
            "name": meta.get("name", directory.split("/")[-1] if directory else ""),
            "description": str(meta.get("description", "")),
            "body": body.strip(),
            "meta": {k: str(v)[:200] for k, v in meta.items() if k not in ("name", "description")},
            "files": sorted(files),
            "github_url": f"https://github.com/{owner}/{repo}/tree/{branch}/{directory}",
        }
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch skill: {e}")


# ── Trending API ─────────────────────────────────────────────────

@app.get("/api/trending")
async def github_trending(language: str = "", since: str = "daily"):
    """Fetch trending repositories from GitHub."""
    trending_cache = Path(__file__).parent / ".trending_cache.json"
    cache_key = f"{language}:{since}"

    # Check cache (30 min)
    if trending_cache.exists():
        try:
            cache = json.loads(trending_cache.read_text(encoding="utf-8"))
            if cache.get("key") == cache_key and time.time() - cache.get("ts", 0) < 1800:
                return cache["data"]
        except Exception:
            pass

    try:
        # Use GitHub search API: repos created in last N days, sorted by stars
        from datetime import datetime, timedelta
        days = {"daily": 7, "weekly": 30, "monthly": 90}.get(since, 7)
        date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        q_parts = [f"created:>{date}"]
        if language:
            q_parts.append(f"language:{language}")
        q = " ".join(q_parts)
        api_url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page=30"

        # Get token from gh CLI
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
        headers = {"User-Agent": "skill-hub", "Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        repos = []
        for item in data.get("items", [])[:30]:
            repos.append({
                "owner": item["owner"]["login"],
                "name": item["name"],
                "full_name": item["full_name"],
                "description": (item.get("description") or "")[:300],
                "language": item.get("language") or "",
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "url": item["html_url"],
                "topics": item.get("topics", [])[:5],
            })

        # Save cache
        trending_cache.write_text(json.dumps({"key": cache_key, "ts": time.time(), "data": repos}, ensure_ascii=False), encoding="utf-8")
        return repos
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch trending: {e}")


# ── 安装 API ────────────────────────────────────────────────────

@app.post("/api/skills/install")
async def install_skill(req: InstallRequest):
    """Download a skill from GitHub to the central repo."""
    dest = REPO_DIR / req.directory
    if dest.exists():
        return {"ok": True, "message": "Already installed locally"}

    try:
        _download_dir(req.owner, req.name, req.branch, req.directory, dest)
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    try:
        tree_sha = _get_remote_tree_sha(req.owner, req.name, req.branch, req.directory)
    except Exception:
        tree_sha = ""
    _installed[req.directory] = {
        "repo_owner": req.owner,
        "repo_name": req.name,
        "repo_branch": req.branch,
        "directory": req.directory,
        "tree_sha": tree_sha,
        "installed_at": int(time.time()),
    }
    _save_installed()
    cache_clear()
    return {"ok": True, "message": f"Installed {req.directory}"}


# ── 更新 API ────────────────────────────────────────────────────

@app.get("/api/updates")
async def check_updates():
    """Check for updates on installed skills from repos."""
    # Group installed skills by repo to minimize API calls
    repo_skills: dict[tuple, list] = {}  # (owner, repo, branch) -> [skill_info]
    for name, info in _installed.items():
        if not info.get("repo_owner"):
            continue
        key = (info["repo_owner"], info["repo_name"], info.get("repo_branch", "main"))
        repo_skills.setdefault(key, []).append((name, info))

    # Fetch tree once per repo
    repo_trees: dict[tuple, dict] = {}
    for (owner, repo, branch) in repo_skills:
        try:
            tree = _gh_api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
            repo_trees[(owner, repo, branch)] = {item["path"]: item for item in tree.get("tree", [])}
        except Exception:
            continue

    updates = []
    for (owner, repo, branch), skills in repo_skills.items():
        tree_map = repo_trees.get((owner, repo, branch))
        if not tree_map:
            continue
        for name, info in skills:
            directory = info["directory"]
            tree_entry = tree_map.get(directory)
            if not tree_entry:
                continue
            remote_sha = tree_entry.get("sha", "")
            local_sha = info.get("tree_sha", info.get("content_hash", ""))
            if remote_sha and remote_sha != local_sha:
                updates.append({
                    "name": name,
                    "repo": f"{owner}/{repo}",
                    "directory": directory,
                    "current_sha": local_sha[:12],
                    "remote_sha": remote_sha[:12],
                })
    return updates


@app.post("/api/skills/{name}/update")
async def update_skill(name: str):
    """Update a single skill from its repo."""
    if name not in _installed:
        raise HTTPException(404, f"Skill '{name}' not tracked as installed from repo")

    info = _installed[name]
    dest = REPO_DIR / info["directory"]

    # Remove old files
    if dest.exists():
        import shutil
        shutil.rmtree(dest)

    try:
        _download_dir(
            info["repo_owner"], info["repo_name"],
            info.get("repo_branch", "main"), info["directory"], dest
        )
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    try:
        tree_sha = _get_remote_tree_sha(info["repo_owner"], info["repo_name"], info.get("repo_branch", "main"), info["directory"])
    except Exception:
        tree_sha = info.get("tree_sha", "")
    info["tree_sha"] = tree_sha
    info["updated_at"] = int(time.time())
    _installed[name] = info
    _save_installed()
    cache_clear()
    return {"ok": True, "message": f"Updated {name}"}


@app.post("/api/updates/all")
async def update_all():
    """Update all skills that have updates."""
    updates = await check_updates()
    results = []
    for u in updates:
        try:
            await update_skill(u["name"])
            results.append({"name": u["name"], "ok": True})
        except Exception as e:
            results.append({"name": u["name"], "ok": False, "error": str(e)})
    return {"updated": len([r for r in results if r["ok"]]), "results": results}


if __name__ == "__main__":
    import uvicorn
    print("Skill Hub starting... http://localhost:8765")
    uvicorn.run(app, host="0.0.0.0", port=8765)
